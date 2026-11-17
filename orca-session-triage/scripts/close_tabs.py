#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Close the tabs a saved scan listed, unless their worktree moved after the scan.

usage: close_tabs.py --from SCAN_JSON [HANDLE ...] [--row NAME ...]

Just before closing each tab it reads again what moves while a lane works: the
newest entry of each of the worktree's Claude transcripts and of the Codex
rollouts the scan read, every report the worktree sent to the triage inbox
(any kind), and when the worktree's agents last changed state in Orca. Anything newer
than the scan's start (`scanned_at` on each row) keeps the tab open and prints
`HANDLE: KEPT -> 走査の後に動いた（time what）`, naming a transcript's line by its kind
(assistant の行, 人の入力, ツールの結果, …): scan again and read the row
before closing it (2026-10-28: an hourly run closed a lane that had deployed
after the scan and was waiting for a check). A remote row's transcripts are
on its own Mac; Orca's agent states and the inbox stand in for them. A tab
whose host cannot be read stays too. A transcript whose new lines are all
Claude Code compacting the idle session on its own ("Compacted while idle")
does not count, nor, when the worktree has one agent besides the caller, do
its state changes within that compaction; a line after it, from the agent or a
person, counts as before.

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
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Protocol

import scan_sessions


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


@dataclass(frozen=True)
class Span:
    start: datetime
    end: datetime

    def holds(self, at: datetime) -> bool:
        return self.start - COMPACTION_SLACK <= at <= self.end + COMPACTION_SLACK


# Compacting an idle session on its own, Claude Code writes the boundary, the summary as a user line, the files and
# listings it attaches again, a notice, and its links again; Orca's agent states move with it.
IDLE_COMPACTION_NOTICE = "Compacted while idle"
COMPACTION_COMPANIONS = {"attachment", "pr-link", "frame-link"}
COMPACTION_SLACK = timedelta(seconds=5)


def timed_entries(path: Path) -> list[tuple[datetime, dict]]:
    """The timestamped entries among a transcript's last lines (scan_sessions.TAIL_LINES)."""
    found = []
    for line in path.read_bytes().splitlines()[-scan_sessions.TAIL_LINES:]:
        entry = report(line)
        if entry is not None and (at := scan_sessions.parse_time(entry.get("timestamp"))) is not None:
            found.append((at, entry))
    return found


def is_idle_notice(entry: dict) -> bool:
    return (entry.get("type") == "system" and entry.get("subtype") == "informational"
            and str(entry.get("content") or "").startswith(IDLE_COMPACTION_NOTICE))


def is_compaction_line(entry: dict) -> bool:
    return (entry.get("type") in COMPACTION_COMPANIONS or is_idle_notice(entry) or bool(entry.get("isCompactSummary"))
            or (entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"))


def idle_compaction(entries: list[tuple[datetime, dict]], since: datetime) -> Span | None:
    """When every entry after `since` is an idle compaction's, its span: from the summary's start (the boundary's
    time less its durationMs) to its last line."""
    new = [(at, entry) for at, entry in entries if at > since]
    if not new or not all(is_compaction_line(entry) for _, entry in new) or not any(
            is_idle_notice(entry) for _, entry in new):
        return None
    boundaries = [(at, entry) for at, entry in entries if entry.get("subtype") == "compact_boundary"]
    start = min(at for at, _ in new)
    if boundaries:
        at, boundary = boundaries[-1]
        duration = (boundary.get("compactMetadata") or {}).get("durationMs")
        start = min(start, at - timedelta(milliseconds=duration) if isinstance(duration, (int, float)) else at)
    return Span(start, max(at for at, _ in new))


def line_kind(entry: dict) -> str:
    kind = entry.get("type")
    if kind == "assistant":
        return "assistant の行"
    if kind == "user":
        if scan_sessions.is_real_user_message(entry):
            return "人の入力"
        content = scan_sessions.message_of(entry).get("content")
        tool_result = isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content)
        return "ツールの結果" if tool_result else "user の行"
    detail = entry.get("subtype") or (entry.get("attachment") or {}).get("type")
    return f"{kind} の行" + (f"（{detail}）" if detail else "")


def transcript_activity(paths: Iterable[Path], since: datetime, compactions: list[Span]) -> Iterator[Activity]:
    """The newest entry, and its kind, of each transcript written since `since`: the file time alone only picks what
    to read. A transcript whose new entries are all an idle compaction's adds its span to `compactions` instead. One
    written after the scan that cannot be read raises OSError, keeping the tab."""
    for path in paths:
        if (at := modified(path)) is None or at <= since:
            continue
        entries = timed_entries(path)
        if (span := idle_compaction(entries, since)) is not None:
            compactions.append(span)
            continue
        if entries:
            at, entry = max(entries, key=lambda item: item[0])
            kind = line_kind(entry)
            yield Activity(at, f"会話記録 {path.name} の{' ' if kind[:1].isascii() else ''}{kind}")


def inbox_activity(row: dict, inbox: Path, caller: Caller) -> Iterator[Activity]:
    """Every report the row's worktree sent to the triage inbox from another terminal than the caller's; an inbox
    that exists but cannot be read raises OSError, which keeps the tab."""
    try:
        lines = inbox.read_bytes().splitlines()
    except FileNotFoundError:
        return
    ours = (item for item in map(report, lines) if item and item.get("worktree") == Path(row["path"]).name
            and (item.get("repo") or "") in scan_sessions.row_repos(row)
            and not (caller.handle and item.get("handle") == caller.handle))
    yield from (Activity(at, f"受信箱 {item.get('kind') or '-'}") for item in ours
                if (at := scan_sessions.parse_time(item.get("ts"))) is not None)


def moved_after_scan(row: dict, host: TabHost, projects: Path, inbox: Path, caller: Caller) -> list[Activity]:
    started = scan_sessions.parse_time(row.get("scanned_at"))
    if started is None:
        raise ValueError("the scan has no scanned_at (saved before close_tabs.py existed): scan again")
    compactions: list[Span] = []
    written = [*transcript_activity(transcripts(row, projects, caller), started, compactions)]
    changes = [change for change in host.agent_changes(row.get("environment"), row["path"])
               if not (caller.pane and change.pane == caller.pane)]
    panes = {change.pane for change in changes}
    compacting_agent_is_the_only_one = len(panes) == 1 and None not in panes
    seen = [*written, *inbox_activity(row, inbox, caller),
            *(Activity(change.at, "Orca の agent の状態") for change in changes
              if not (compacting_agent_is_the_only_one and any(span.holds(change.at) for span in compactions)))]
    return sorted((activity for activity in seen if activity.at > started), key=lambda activity: activity.at)


def row_handles(row: dict) -> list[str]:
    """The handles the row lists for cleanup, once each; sleeping cards have no handle."""
    return list(dict.fromkeys(handle for handle in [*(row.get("closable") or []), *(row.get("meaningful_tabs") or []),
                                                    *(card.get("handle") for card in row.get("done_cards") or [])]
                              if handle and not handle.startswith("(no handle)")))


def row_of(rows: Sequence[dict], handle: str) -> dict | None:
    return next((row for row in rows if handle in row_handles(row)
                 or any(tab.get("handle") == handle for tab in row.get("terminals") or [])), None)


def close_one(rows: Sequence[dict], handle: str, host: TabHost, projects: Path, inbox: Path,
              caller: Caller, state_file: Path = scan_sessions.HOURLY_STATE) -> tuple[bool, str]:
    row = row_of(rows, handle)
    if row is None:
        return False, f"{handle}: KEPT -> not a tab of any row in the scan"
    try:
        if scan_sessions.pending_handoffs(row, state_file.expanduser()):
            return False, f"{handle}: KEPT -> 未配信の約束の送り先"
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
    parser.add_argument("handles", nargs="*", metavar="HANDLE")
    parser.add_argument("--row", action="append", default=[], metavar="NAME",
                        help="close this row's closable, meaningful_tabs, and done_cards handles; repeatable; "
                             "NAME accepts the same repo/name, name, path, or identity as scan_sessions.py --row")
    parser.add_argument("--claude-projects", type=Path, default=Path("~/.claude/projects"),
                        help="Claude Code transcript root (default: %(default)s)")
    parser.add_argument("--inbox", type=Path,
                        default=Path(os.environ.get("TRIAGE_INBOX") or scan_sessions.INBOX_FILE),
                        help="triage inbox (default: $TRIAGE_INBOX or %(default)s)")
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    parser.add_argument("--state-file", type=Path, default=scan_sessions.HOURLY_STATE,
                        help="shared hourly state; reread undelivered destinations before closing")
    args = parser.parse_args(argv)
    if not args.handles and not args.row:
        parser.error("pass HANDLE or --row NAME")
    return args


def main(argv: Sequence[str] | None = None, host: TabHost | None = None) -> int:
    args = parse_args(argv)
    try:
        rows = scan_sessions.saved_rows(args.from_file)
    except (OSError, ValueError) as error:
        print(f"--from: {error}", file=sys.stderr)
        return 2
    try:
        selected = [scan_sessions.select_row(rows, name) for name in args.row]
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    targets = [(row_of(rows, handle), handle) for value in args.handles for handle in value.split()]
    targets.extend((row, handle) for row in selected for handle in row_handles(row))
    unique = {(row.get("environment") if row else None, handle): (row, handle) for row, handle in targets}
    host = host or OrcaTabs(args.orca)
    caller = Caller.from_environment()
    results = [close_one([row] if row else rows, handle, host, args.claude_projects.expanduser(),
                         args.inbox.expanduser(), caller, args.state_file) for row, handle in unique.values()]
    print(*(line for _, line in results), sep="\n")
    return 0 if all(closed for closed, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
