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


class TabHost(Protocol):
    def agent_changes(self, environment: str | None, path: str) -> list[datetime]:
        """When each agent of the worktree at `path` last changed state (done, working, waiting)."""
        ...

    def close(self, environment: str | None, handle: str) -> None:
        """Close one tab; raise RuntimeError when the host refuses."""
        ...


class OrcaTabs:
    def __init__(self, orca: str) -> None:
        self.orca = orca
        self.worktrees: dict[str | None, list[dict]] = {}

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

    def agent_changes(self, environment: str | None, path: str) -> list[datetime]:
        # Orca moves updatedAt without any work (a working agent's own polling), so only state changes count.
        if environment not in self.worktrees:
            self.worktrees[environment] = self.run(environment, "worktree", "ps")["worktrees"]
        agents = [agent for worktree in self.worktrees[environment] if worktree.get("path") == path
                  for agent in worktree.get("agents") or []]
        started = [stamp for agent in agents
                   for stamp in (agent.get("stateStartedAt"), (agent.get("mainAgent") or {}).get("stateStartedAt"))]
        return [datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
                for stamp in started if isinstance(stamp, (int, float))]

    def close(self, environment: str | None, handle: str) -> None:
        self.run(environment, "terminal", "close", "--terminal", handle)


@dataclass(frozen=True)
class Activity:
    at: datetime
    what: str


def transcripts(row: dict, projects: Path) -> Iterator[Path]:
    """The local transcripts of the row's worktree: its Claude project directory and the rollouts the scan read."""
    if row.get("environment"):
        return
    yield from scan_sessions.find_claude_dir(projects, row["path"]).glob("*.jsonl")
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
    """The newest timestamped entry among the transcript's last lines; untimed bookkeeping lines do not count."""
    try:
        lines = path.read_bytes().splitlines()[-TAIL_LINES:]
    except OSError:
        return None
    return max((at for item in map(report, lines) if item
                if (at := scan_sessions.parse_time(item.get("timestamp"))) is not None), default=None)


def transcript_activity(paths: Iterable[Path], since: datetime) -> Iterator[Activity]:
    """Each transcript written since `since`, at its newest entry. Claude Code also rewrites an idle session's
    transcript with untimed lines (bridge-session, cost-state), so the file time alone only picks what to read."""
    touched = (path for path in paths if (at := modified(path)) is not None and at > since)
    yield from (Activity(at, f"会話記録 {path.name}") for path in touched if (at := last_entry_at(path)) is not None)


def inbox_activity(row: dict, inbox: Path) -> Iterator[Activity]:
    """Every report the row's worktree sent to the triage inbox."""
    try:
        lines = inbox.read_bytes().splitlines()
    except OSError:
        return
    ours = (item for item in map(report, lines) if item and item.get("worktree") == Path(row["path"]).name
            and item.get("repo") in (None, "", row.get("repo")))
    yield from (Activity(at, f"受信箱 {item.get('kind') or '-'}") for item in ours
                if (at := scan_sessions.parse_time(item.get("ts"))) is not None)


def moved_after_scan(row: dict, host: TabHost, projects: Path, inbox: Path) -> list[Activity]:
    started = scan_sessions.parse_time(row.get("scanned_at"))
    if started is None:
        raise ValueError("the scan has no scanned_at (saved before close_tabs.py existed): scan again")
    seen = [*transcript_activity(transcripts(row, projects), started), *inbox_activity(row, inbox),
            *(Activity(at, "Orca の agent の状態") for at in host.agent_changes(row.get("environment"), row["path"]))]
    return sorted((activity for activity in seen if activity.at > started), key=lambda activity: activity.at)


def row_of(rows: Sequence[dict], handle: str) -> dict | None:
    return next((row for row in rows if any(tab.get("handle") == handle for tab in row.get("terminals") or [])),
                None)


def close_one(rows: Sequence[dict], handle: str, host: TabHost, projects: Path, inbox: Path) -> tuple[bool, str]:
    row = row_of(rows, handle)
    if row is None:
        return False, f"{handle}: KEPT -> not a tab of any row in the scan"
    try:
        moved = moved_after_scan(row, host, projects, inbox)
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
    results = [close_one(rows, handle, host, args.claude_projects.expanduser(), args.inbox.expanduser())
               for handle in dict.fromkeys(args.handles)]
    print(*(line for _, line in results), sep="\n")
    return 0 if all(closed for closed, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
