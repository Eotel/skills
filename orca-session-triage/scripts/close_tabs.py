#!/usr/bin/env python3
"""Close the tabs a saved scan listed, unless their worktree moved after the scan.

usage: close_tabs.py --from SCAN_JSON HANDLE [HANDLE ...]

Just before closing each tab it reads again what moves while a lane works: the
newest entry of each of the worktree's Claude transcripts and of the Codex
rollouts the scan read, every report the worktree sent to the triage inbox
(any kind), and when the worktree's agents last changed state in Orca. Anything newer
than the scan's start (`scanned_at` on each row) keeps the tab open and prints
`HANDLE: KEPT -> 走査の後に動いた（time what）`: scan again and read the row
before closing it (2026-10-28: an hourly run closed a lane that had deployed
after the scan and was waiting for a check). A remote row's transcripts are
on its own Mac; Orca's agent states and the inbox stand in for them. A tab
whose host cannot be read stays too.

The caller's own activity is left out: its Claude transcript ($CLAUDE_CODE_SESSION_ID), the agent of its
Orca pane ($ORCA_PANE_KEY), and the reports its terminal sent ($ORCA_TERMINAL_HANDLE).
An hourly run that closes the previous hourly tab of its own workspace otherwise keeps it for the run's
own work (2026-10-29).

Prints one line per handle (CLOSED, KEPT, or NOT CLOSED); exits 0 only when
every tab closed. Reading the agents and closing a tab sit behind TabHost;
OrcaTabs implements it with the Orca CLI, so another host needs one more TabHost.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Protocol

import scan_sessions


# Lines read from the end of a transcript for its newest entry.
TAIL_LINES = 400


@dataclass(frozen=True)
class AgentChange:
    pane: str | None  # Orca's paneKey (tabId:leafId) of the agent's tab
    at: datetime


class TabHost(Protocol):
    def agent_changes(self, environment: str | None, path: str) -> list[AgentChange]:
        """When each agent of the worktree at `path` last changed state (done, working, waiting)."""
        ...

    def close(self, environment: str | None, handle: str) -> None:
        """Close one tab; raise RuntimeError when the host refuses."""
        ...


class OrcaTabs:
    def __init__(self, orca: str) -> None:
        self.orca = orca

    def run(self, environment: str | None, *args: str) -> dict:
        where = ["--environment", environment] if environment else []
        done = subprocess.run([self.orca, *args, *where, "--json"], capture_output=True, text=True,
                              timeout=scan_sessions.ORCA_TIMEOUT)
        try:
            payload = json.loads(done.stdout)
        except ValueError:
            payload = {}
        if done.returncode != 0 or not payload.get("ok", True):
            raise RuntimeError((done.stderr or done.stdout).strip()[-300:] or f"orca exited {done.returncode}")
        return payload.get("result", payload)

    def agent_changes(self, environment: str | None, path: str) -> list[AgentChange]:
        # Read for each tab: a worktree may start working while the tabs before it close. Orca moves
        # updatedAt without any work (a working agent's own polling), so only state changes count.
        agents = [agent for worktree in self.run(environment, "worktree", "ps")["worktrees"]
                  if worktree.get("path") == path for agent in worktree.get("agents") or []]
        return [AgentChange(agent.get("paneKey"), datetime.fromtimestamp(stamp / 1000, tz=timezone.utc))
                for agent in agents
                for stamp in (agent.get("stateStartedAt"), (agent.get("mainAgent") or {}).get("stateStartedAt"))
                if isinstance(stamp, (int, float))]

    def close(self, environment: str | None, handle: str) -> None:
        self.run(environment, "terminal", "close", "--terminal", handle)


@dataclass(frozen=True)
class Activity:
    at: datetime
    what: str


@dataclass(frozen=True)
class Caller:
    """The session running close_tabs.py, whose own work in the worktree is not the tab's."""
    session: str | None
    pane: str | None
    handle: str | None

    @classmethod
    def from_environment(cls) -> Caller:
        return cls(session=os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
                   pane=os.environ.get("ORCA_PANE_KEY") or None,
                   handle=os.environ.get("ORCA_TERMINAL_HANDLE") or None)


def transcripts(row: dict, projects: Path, caller: Caller) -> Iterator[Path]:
    """The local transcripts of the row's worktree but the caller's: its Claude project directory under `projects`
    and the one holding the transcript the scan read (the scan may have had another root), and the rollouts the
    scan read."""
    if row.get("environment"):
        return
    scanned = (row.get("claude") or {}).get("transcript")
    directories = {scan_sessions.find_claude_dir(projects, row["path"]), *([Path(scanned).parent] if scanned else [])}
    yield from (path for directory in directories for path in directory.glob("*.jsonl") if path.stem != caller.session)
    yield from (Path(rollout["rollout"]) for rollout in row.get("codex") or [] if rollout.get("rollout"))


def report(raw: bytes) -> dict | None:
    try:
        item = json.loads(raw)
    except ValueError:
        return None
    return item if isinstance(item, dict) else None


def modified(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def last_entry_at(path: Path) -> datetime | None:
    """The newest timestamped entry among the transcript's last lines; untimed bookkeeping lines do not count.

    A transcript written after the scan that cannot be read raises OSError, which keeps the tab.
    """
    lines = path.read_bytes().splitlines()[-TAIL_LINES:]
    return max((at for item in map(report, lines) if item
                if (at := scan_sessions.parse_time(item.get("timestamp"))) is not None), default=None)


def transcript_activity(paths: Iterable[Path], since: datetime) -> Iterator[Activity]:
    """Each transcript written since `since`, at its newest entry. Claude Code also rewrites an idle session's
    transcript with untimed lines (bridge-session, cost-state), so the file time alone only picks what to read."""
    touched = (path for path in paths if (at := modified(path)) is not None and at > since)
    yield from (Activity(at, f"会話記録 {path.name}") for path in touched if (at := last_entry_at(path)) is not None)


def inbox_activity(row: dict, inbox: Path, caller: Caller) -> Iterator[Activity]:
    """Every report the row's worktree sent to the triage inbox from another terminal than the caller's; an inbox
    that exists but cannot be read raises OSError, which keeps the tab."""
    try:
        lines = inbox.read_bytes().splitlines()
    except FileNotFoundError:
        return
    ours = (item for item in map(report, lines) if item and item.get("worktree") == Path(row["path"]).name
            and item.get("repo") in (None, "", row.get("repo"))
            and not (caller.handle and item.get("handle") == caller.handle))
    yield from (Activity(at, f"受信箱 {item.get('kind') or '-'}") for item in ours
                if (at := scan_sessions.parse_time(item.get("ts"))) is not None)


def moved_after_scan(row: dict, host: TabHost, projects: Path, inbox: Path, caller: Caller) -> list[Activity]:
    started = scan_sessions.parse_time(row.get("scanned_at"))
    if started is None:
        raise ValueError("the scan has no scanned_at (saved before close_tabs.py existed): scan again")
    seen = [*transcript_activity(transcripts(row, projects, caller), started), *inbox_activity(row, inbox, caller),
            *(Activity(change.at, "Orca の agent の状態")
              for change in host.agent_changes(row.get("environment"), row["path"])
              if not (caller.pane and change.pane == caller.pane))]
    return sorted((activity for activity in seen if activity.at > started), key=lambda activity: activity.at)


def row_of(rows: Sequence[dict], handle: str) -> dict | None:
    return next((row for row in rows if any(tab.get("handle") == handle for tab in row.get("terminals") or [])),
                None)


def close_one(rows: Sequence[dict], handle: str, host: TabHost, projects: Path, inbox: Path,
              caller: Caller) -> tuple[bool, str]:
    row = row_of(rows, handle)
    if row is None:
        return False, f"{handle}: KEPT -> not a tab of any row in the scan"
    try:
        moved = moved_after_scan(row, host, projects, inbox, caller)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError) as error:
        return False, f"{handle}: KEPT -> {error}"
    if moved:
        last = moved[-1]
        return False, (f"{handle}: KEPT -> 走査の後に動いた（{last.at.astimezone().strftime('%H:%M:%S')} {last.what}"
                       + (f" ほか {len(moved) - 1} 件" if len(moved) > 1 else "") + "）")
    try:
        host.close(row.get("environment"), handle)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError) as error:
        return False, f"{handle}: NOT CLOSED -> {error}"
    return True, f"{handle}: CLOSED"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_file", required=True, metavar="SCAN_JSON",
                        help="the saved scan whose rows list these tabs")
    parser.add_argument("handles", nargs="+", metavar="HANDLE")
    parser.add_argument("--claude-projects", type=Path, default=Path("~/.claude/projects"),
                        help="Claude Code transcript root (default: %(default)s)")
    parser.add_argument("--inbox", type=Path,
                        default=Path(os.environ.get("TRIAGE_INBOX") or scan_sessions.INBOX_FILE),
                        help="triage inbox (default: $TRIAGE_INBOX or %(default)s)")
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, host: TabHost | None = None) -> int:
    args = parse_args(argv)
    try:
        rows = scan_sessions.saved_rows(args.from_file)
    except (OSError, ValueError) as error:
        print(f"--from: {error}", file=sys.stderr)
        return 2
    host = host or OrcaTabs(args.orca)
    caller = Caller.from_environment()
    results = [close_one(rows, handle, host, args.claude_projects.expanduser(), args.inbox.expanduser(), caller)
               for handle in dict.fromkeys(args.handles)]
    print(*(line for _, line in results), sep="\n")
    return 0 if all(closed for closed, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
