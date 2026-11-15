#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Print what the lead and the lanes did as one timeline, read from the records they already append.

Each line is one event: its time (JST), who acted (a workspace, 指揮 for the lead's decision log, or the
person the adoption ledger names), to whom, what happened, a summary, and the record it came from
(`[source id]`; `--json` gives the whole text). The records:

- inbox: the lanes' `triage-notify` reports (質問・完了・経過が届いた), `TRIAGE_INBOX`;
- answer: answers sent or recorded for those questions, and withdrawn questions, `TRIAGE_ASK_ANSWERS`;
- decision: the lead's decision log (指揮が決めた), `TRIAGE_LEAD_DECISIONS`;
- board: the board's event log, the only record of what `send` delivered (送った), the lanes `spawn-lane`
  started (起動した), and the Backlog comments `backlog-post` posted (投稿した), `ORCA_BOARD_EVENTS`;
- hold: when each hold was set and released, `holds.sqlite` under `ORCA_BRIEFS_ROOT`;
- undelivered: sends kept for a resend because the lane's screen was unreadable, under `ORCA_BRIEFS_ROOT`;
- adoption: the adoption ledger's proposals, decisions, hand-offs and applications,
  `ADOPTION_TRIAGE_EVENTS` and the other Macs' copies in `ADOPTION_TRIAGE_IMPORTS`;
- claude, codex: this Mac's agent transcripts, for calls that finished writing outside the workspace: a
  `gh` command that merges, comments, reviews, creates, edits or sends a write to the API, and an MCP tool
  not marked read-only (Codex) or named for a write (Claude). `gh pr merge --auto` reads as a scheduled
  merge. Text in a heredoc or quotes is not a command, and a call that failed, ran in the background, or
  follows `||` is left out. Its time is when it finished, so it reads against the sends around it.

A send's time is when `send` finished checking the delivery, some seconds after the lane got the text.
The session host (Orca today) names the workspace behind a tab's handle or a directory and lists the
transcripts; a host the lanes move to implements `SessionHost`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import BinaryIO, Literal, Protocol

JST = timezone(timedelta(hours=9))
SUMMARY = 120
# Transcripts only grow, so reading starts this far before --since: a call may finish well after it began, and
# Claude Code lets a foreground Bash call run for up to two hours.
CALL_SLACK = timedelta(hours=2)
CHUNK = 4 << 20
STAMP = re.compile(rb'"timestamp":\s*"([^"]+)"')
REPORTED = {"ask": "質問が届いた", "done": "完了が届いた", "info": "経過が届いた"}
ADOPTION_DECIDED = {"adopt": "案を採用した", "reject": "案を却下した", "defer": "案を保留した",
                    "revoke": "案の採用を取り消した"}
ADOPTION_DONE = {"handed_off": "案を渡した", "applied": "案を入れた"}
WRITE_NAME = re.compile(r"(?:^|[._])(?:add|create|update|edit|merge|delete|remove|post|comment|submit|send|"
                        r"reply|approve)(?:$|[._])")
SHELL_BREAK = re.compile(r"([;\n()]|&&|\|\|?|(?<![<>])&|\bthen\b|\bdo\b)")
HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)(\w+)\1")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", re.DOTALL)
HELP = re.compile(r"(?:^|\s)(?:--help|-h)(?=\s|$)")
GH_CALL = re.compile(r"\s*(?:\w+=\S*\s+)*gh\s+(\S+)\s*(\S*)(.*)", re.DOTALL)
GH_POSTS = {("pr", "comment"), ("pr", "create"), ("pr", "review"), ("issue", "comment"), ("issue", "create")}
GH_EDITS = {"edit", "close", "reopen", "ready", "delete", "lock", "transfer"}


@dataclass(frozen=True)
class Event:
    at: datetime
    lane: str
    kind: str
    text: str
    source: str
    ref: str
    to: str | None = None

    def line(self) -> str:
        who = f"{self.lane} → {self.to}" if self.to else self.lane
        summary = " ".join(self.text.split())
        summary = summary if len(summary) <= SUMMARY else summary[:SUMMARY] + "…"
        return f"{self.at:%m/%d %H:%M:%S}  {who}  {self.kind}  {summary}  [{self.source} {self.ref[:12]}]"

    def row(self) -> dict[str, str | None]:
        return {**asdict(self), "at": self.at.isoformat(timespec="seconds")}


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime | None

    def at(self, value: object) -> datetime | None:
        """The time a record gives, when it falls inside the window."""
        at = when(value)
        return at if at is not None and self.start <= at and (self.end is None or at <= self.end) else None


@dataclass(frozen=True)
class Transcript:
    agent: Literal["claude", "codex"]
    path: Path


class SessionHost(Protocol):
    """The manager the lanes' agent sessions run in."""

    def tab_workspaces(self) -> dict[str, str]:
        """Each live tab's handle and the name of the workspace it works in."""
        ...

    def workspace_name(self, path: Path) -> str:
        """The name of the workspace a directory belongs to."""
        ...

    def transcripts(self, since: datetime) -> list[Transcript]:
        """This Mac's agent transcripts written to since `since`."""
        ...


@dataclass(frozen=True)
class OrcaHost:
    orca: str
    workspaces: Path
    support: Path

    @classmethod
    def from_environment(cls, orca: str) -> OrcaHost:
        return cls(orca, Path(os.environ.get("ORCA_WORKSPACES") or Path.home() / "orca/workspaces"),
                   Path.home() / "Library/Application Support/orca")

    def tab_workspaces(self) -> dict[str, str]:
        try:
            done = subprocess.run([self.orca, "terminal", "list", "--json"], capture_output=True, text=True, timeout=30)
            payload = json.loads(done.stdout)
            terminals = payload.get("result", payload)["terminals"]
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as error:
            print(f"timeline: Orca の端末の一覧を読めないため、記録にある名前だけを使います: {error}", file=sys.stderr)
            return {}
        return {str(terminal["handle"]): self.workspace_name(Path(terminal["worktreePath"]))
                for terminal in terminals if terminal.get("handle") and terminal.get("worktreePath")}

    def workspace_name(self, path: Path) -> str:
        # Orca puts each workspace at <workspaces>/<repository>/<name>; an agent may stand in a directory under it.
        parts = path.relative_to(self.workspaces).parts if path.is_relative_to(self.workspaces) else ()
        return parts[1] if len(parts) >= 2 else path.name

    def transcripts(self, since: datetime) -> list[Transcript]:
        home = Path.home()
        claude = [home / ".claude", *self.support.glob("claude-profiles/*/home")]
        codex = [home / ".codex", *self.support.glob("codex-accounts/*/home"), self.support / "codex-runtime-home/home"]
        found = [*(Transcript("claude", path) for root in claude for path in root.glob("projects/*/*.jsonl")),
                 *(Transcript("codex", path) for root in codex for path in root.glob("sessions/*/*/*/rollout-*.jsonl"))]
        # One session's transcript shows under several homes, linked or copied; its file name carries the session id.
        newest = sorted(found, key=lambda transcript: modified(transcript.path))
        unique = {(transcript.agent, transcript.path.name): transcript for transcript in newest}
        return [transcript for transcript in unique.values() if modified(transcript.path) >= since.timestamp()]


def modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@dataclass(frozen=True)
class Records:
    inbox: Path
    answers: Path
    decisions: Path
    board: Path
    briefs: Path
    adoption: Path
    adoption_imports: Path

    @classmethod
    def from_environment(cls) -> Records:
        home = Path.home()
        briefs = Path(os.environ.get("ORCA_BRIEFS_ROOT") or home / ".cache/orca-briefs")

        def path(variable: str, default: Path) -> Path:
            return Path(os.environ.get(variable) or default).expanduser()

        return cls(inbox=path("TRIAGE_INBOX", briefs / "triage-inbox.jsonl"),
                   answers=path("TRIAGE_ASK_ANSWERS", briefs / "ask-answers.jsonl"),
                   decisions=path("TRIAGE_LEAD_DECISIONS", home / ".cache/orca-session-triage/lead-decisions.jsonl"),
                   board=path("ORCA_BOARD_EVENTS", home / ".local/share/orca-board/events.jsonl"),
                   briefs=briefs.expanduser(),
                   adoption=path("ADOPTION_TRIAGE_EVENTS", home / ".local/share/adoption-triage/events.jsonl"),
                   adoption_imports=path("ADOPTION_TRIAGE_IMPORTS", home / ".local/share/adoption-triage/imports"))


def entries(path: Path) -> list[dict]:
    """A JSON Lines record's objects; a missing record is empty and a damaged line is skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [item for item in map(parse, text.splitlines()) if item is not None]


def parse(line: str | bytes) -> dict | None:
    try:
        item = json.loads(line)
    except ValueError:
        return None
    return item if isinstance(item, dict) else None


def when(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, JST)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)).astimezone(JST)
    return None


@dataclass(frozen=True)
class Names:
    handles: dict[str, str]

    def of(self, handle: object) -> str:
        return self.handles.get(str(handle)) or str(handle or "?")[:13]


def names(inbox: list[dict], board: list[dict], host: SessionHost) -> Names:
    recorded = {**{str(item["handle"]): str(item["worktree"]) for item in inbox
                   if item.get("handle") and item.get("worktree")},
                **{str(item["to"]): Path(str(item["worktree"])).name for item in board
                   if item.get("type") == "start" and item.get("to") and item.get("worktree")}}
    return Names({**recorded, **host.tab_workspaces()})


def reports(inbox: list[dict], window: Window) -> Iterator[Event]:
    for item in inbox:
        if (at := window.at(item.get("ts"))):
            text = item.get("text") or "; ".join(str(question.get("question")) for question in item.get("questions") or [])
            kind = REPORTED.get(str(item.get("kind")), f"{item.get('kind')}が届いた")
            yield Event(at, str(item.get("worktree") or "?"), kind, text, "inbox", str(item.get("id")))


def answers(path: Path, inbox: list[dict], window: Window, host: SessionHost) -> Iterator[Event]:
    asked = {str(item["id"]): str(item.get("worktree") or "?") for item in inbox if item.get("id")}
    for item in entries(path):
        at = window.at(item.get("ts"))
        if at is None:
            continue
        ask = str(item.get("id"))
        sender = (item.get("sender") or {}).get("worktree")
        lane = host.workspace_name(Path(sender)) if sender else "指揮"
        how = str(item.get("how"))
        note = f"ask {ask[:8]}: {item.get('note') or ''}".rstrip(": ")
        if how.endswith("--withdraws"):
            yield Event(at, lane, "質問を取り下げた", note, "answer", ask)
        else:
            kind = "答えた" if how.startswith("send") else "答えを記録した"
            yield Event(at, lane, kind, note, "answer", ask, asked.get(ask.split(":")[0]))


def decisions(path: Path, window: Window) -> Iterator[Event]:
    for item in entries(path):
        if (at := window.at(item.get("decided_at"))):
            key = str(item.get("decision"))
            named = re.search(r"::(/[^:]+)", key)
            yield Event(at, "指揮", "指揮が決めた", f"{key}: {item.get('answer')}", "decision", str(item.get("id")),
                        Path(named[1]).name if named else None)


def board_events(board: list[dict], window: Window, lanes: Names) -> Iterator[Event]:
    for item in board:
        at = window.at(item.get("ts"))
        if at is None:
            continue
        ref = str(item.get("id"))
        if item.get("type") == "send" and item.get("source") == "send":
            yield Event(at, lanes.of(item.get("from")), "送った", str(item.get("text") or ""), "board", ref,
                        lanes.of(item.get("to")))
        elif item.get("type") == "start":
            yield Event(at, lanes.of(item.get("from")), "起動した", f"{item.get('task')}（{item.get('brief')}）", "board",
                        ref, Path(str(item.get("worktree"))).name)
        elif item.get("type") == "post" and item.get("source") == "backlog-post":
            yield Event(at, str(item.get("worktree") or lanes.of(item.get("from"))), "投稿した",
                        f"{item.get('to')} {item.get('text') or ''}", "board", ref)


def holds(path: Path, window: Window, lanes: Names) -> Iterator[Event]:
    if not path.exists():
        return
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM holds").fetchall()
    for row in rows:
        kind, value = row["condition_kind"], row["condition_value"]
        until = {"pr-merged": f"{value} の merge まで", "time": f"{value} まで"}.get(kind, f"{lanes.of(value)} の解除の連絡まで")
        lane = lanes.of(row["handle"])
        if (at := window.at(row["created_at"])):
            yield Event(at, lane, "保留を置いた", f"{until}: {row['instruction']}", "hold", row["id"])
        if (at := window.at(row["released_at"])):
            signalled = when(row["signalled_at"])
            note = f"（解除の連絡 {signalled:%H:%M:%S}）" if signalled else ""
            yield Event(at, lane, "保留を解いた", f"{until}{note}", "hold", row["id"])


def undelivered(directory: Path, window: Window, lanes: Names) -> Iterator[Event]:
    for path in sorted(directory.glob("*.json")):
        # watch-inbox removes a record once its resend lands, possibly between the glob and these reads.
        item = parse(read_or_empty(path)) or {}
        if (at := window.at(item.get("at"))):
            text = read_or_empty(path.with_suffix(".txt")) or str(item.get("target"))
            yield Event(at, lanes.of(item.get("handle")), "未配信", text, "undelivered", path.stem)


def read_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def adoptions(events: Path, imports: Path, window: Window) -> Iterator[Event]:
    ledger = {str(item.get("id")): item for path in [events, *sorted(imports.glob("*.jsonl"))] for item in entries(path)}
    proposals = {item.get("proposal_id"): str(item.get("proposal") or "") for item in ledger.values()
                 if item.get("type") == "proposal"}
    for ref, item in ledger.items():
        at = window.at(item.get("ts"))
        if at is None:
            continue
        proposal = proposals.get(item.get("proposal_id"), str(item.get("proposal_id")))
        if item.get("type") == "proposal":
            source = str((item.get("source") or {}).get("worktree") or "?")
            yield Event(at, Path(source).name, "案が出た", proposal, "adoption", ref)
        elif item.get("type") == "decision":
            kind = ADOPTION_DECIDED.get(str(item.get("decision")), f"案を{item.get('decision')}")
            yield Event(at, str(item.get("actor") or "?"), kind, f"{proposal}（{item.get('reason') or ''}）", "adoption", ref)
        elif item.get("type") == "execution":
            kind = ADOPTION_DONE.get(str(item.get("outcome")), f"案の実施: {item.get('outcome')}")
            yield Event(at, str(item.get("actor") or "?"), kind, f"{proposal} {item.get('result') or ''}", "adoption", ref)


def gh_writes(command: str) -> list[tuple[str, str]]:
    """What each `gh` call of a shell command wrote, with the call (its quoted text emptied)."""
    parts = SHELL_BREAK.split(executed(command))
    # A call after || runs only when the one before it failed, which a successful command cannot show.
    segments = [segment for index, segment in enumerate(parts[::2]) if index == 0 or parts[2 * index - 1] != "||"]
    # A GraphQL query is quoted, so whether it mutates is read from the command as written.
    mutation = "mutation" in command
    return [wrote for segment in segments if (wrote := gh_write(segment, mutation))]


def executed(command: str) -> str:
    """The command without its heredoc bodies and quoted text, which the shell passes on instead of running."""
    kept: list[str] = []
    ends: list[str] = []
    for line in command.split("\n"):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            continue
        ends.extend(found[2] for found in HEREDOC.finditer(line))
        kept.append(line)
    return QUOTED.sub(lambda found: found[0][0] * 2, "\n".join(kept))


def gh_write(segment: str, mutation: bool) -> tuple[str, str] | None:
    found = GH_CALL.match(segment)
    if found is None:
        return None
    group, verb, rest = found.groups()
    call = segment.strip()
    if HELP.search(f"{verb} {rest}"):
        return None
    if group == "pr" and verb == "merge":
        auto = re.search(r"(?:^|\s)--(disable-auto|auto)\b", rest)
        return {"auto": "自動 merge を予約した", "disable-auto": "自動 merge を取り消した"}.get(
            auto[1] if auto else "", "merge した"), call
    if (group, verb) in GH_POSTS:
        return "投稿した", call
    if group in ("pr", "issue") and verb in GH_EDITS:
        return "書いた", call
    if group == "api":
        arguments = f"{verb}{rest}"
        method = re.search(r"(?:-X|--method)[ =]?(\w+)", arguments)
        fields = re.search(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)\b", arguments)
        query = verb == "graphql" and not mutation
        if method and method[1].upper() != "GET" or not method and fields and not query:
            return "書いた", call
    return None


def tool_write(tool: str) -> str:
    return "merge した" if "merge" in tool else "投稿した" if re.search(r"comment|reply|post|(?:create|submit)_\w*review", tool) else "書いた"


def target(arguments: dict) -> str:
    repo = next((arguments[key] for key in ("repo_full_name", "repository_full_name", "repo", "repository")
                 if arguments.get(key)), None)
    number = next((arguments[key] for key in ("pr_number", "issue_number", "pull_number", "number")
                   if arguments.get(key)), None)
    return f"{repo or ''}#{number}" if number else str(repo or "")


def window_lines(path: Path, start: datetime) -> Iterator[bytes]:
    """The transcript's lines from about `start` on, found by reading back from its end."""
    try:
        with path.open("rb") as stream:
            stream.seek(window_offset(stream, start))
            if stream.tell():
                stream.readline()
            yield from stream
    except OSError as error:
        print(f"timeline: {path} を読めません: {error}", file=sys.stderr)


def window_offset(stream: BinaryIO, start: datetime) -> int:
    begin = stream.seek(0, os.SEEK_END)
    while begin > 0:
        begin = max(0, begin - CHUNK)
        stream.seek(begin)
        chunk = stream.read(CHUNK)
        found = STAMP.search(chunk, chunk.find(b"\n") + 1 if begin else 0)
        if found and (at := when(found[1].decode(errors="replace"))) and at < start:
            return begin
    return 0


def codex_writes(transcript: Path, window: Window, host: SessionHost) -> Iterator[Event]:
    try:
        with transcript.open("rb") as stream:
            meta = parse(stream.readline()) or {}
    except OSError:
        return
    lane = host.workspace_name(Path(str((meta.get("payload") or {}).get("cwd") or "?")))
    for raw in window_lines(transcript, window.start - CALL_SLACK):
        if b'"item_completed"' not in raw or (b"McpToolCall" not in raw and b"CommandExecution" not in raw):
            continue
        line = parse(raw) or {}
        item = (line.get("payload") or {}).get("item") or {}
        at = window.at(line.get("timestamp"))
        if at is None or item.get("status") != "completed":
            continue
        if item.get("type") == "McpToolCall":
            tool = str(item.get("tool") or "")
            hint = item.get("readOnlyHint")
            if "'isError': True" in str(item.get("result")) or hint is True or hint is None and not WRITE_NAME.search(tool):
                continue
            summary = f"{item.get('appName') or item.get('server')} {tool.split('.')[-1]} {target(item.get('arguments') or {})}"
            yield Event(at, lane, tool_write(tool), summary, "codex", str(item.get("id")))
        elif item.get("exit_code") == 0:
            writes = gh_writes(" ".join(map(str, (item.get("command") or [])[-1:])))
            for number, (kind, call) in enumerate(writes, 1):
                yield Event(at, lane, kind, call, "codex", reference(str(item.get("id")), number, len(writes)))


def claude_writes(transcript: Path, window: Window, host: SessionHost) -> Iterator[Event]:
    pending: dict[str, tuple[str, list[tuple[str, str]]]] = {}  # tool_use id -> (lane, [(kind, call)])
    for raw in window_lines(transcript, window.start - CALL_SLACK):
        if b'"tool_use"' in raw and (b"gh " in raw or b"mcp__" in raw):
            line = parse(raw) or {}
            lane = host.workspace_name(Path(str(line.get("cwd") or "?")))
            for part in content(line):
                if part.get("type") == "tool_use" and (writes := claude_calls(part)):
                    pending[str(part.get("id"))] = (lane, writes)
        elif pending and b'"tool_result"' in raw and any(use.encode() in raw for use in pending):
            line = parse(raw) or {}
            for part in content(line):
                use = str(part.get("tool_use_id"))
                if part.get("type") == "tool_result" and use in pending:
                    lane, writes = pending.pop(use)
                    if not part.get("is_error") and (at := window.at(line.get("timestamp"))):
                        for number, (kind, call) in enumerate(writes, 1):
                            yield Event(at, lane, kind, call, "claude", reference(use, number, len(writes)))


def content(line: dict) -> list[dict]:
    parts = (line.get("message") or {}).get("content")
    return [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []


def claude_calls(part: dict) -> list[tuple[str, str]]:
    name = str(part.get("name") or "")
    arguments = part.get("input") or {}
    if name == "Bash":
        # A background call's result only says it started; whether it wrote comes later, if ever.
        return [] if arguments.get("run_in_background") else gh_writes(str(arguments.get("command") or ""))
    if name.startswith("mcp__") and WRITE_NAME.search(tool := name.split("__")[-1]):
        return [(tool_write(tool), f"{name.split('__')[1]} {tool} {target(arguments)}")]
    return []


def reference(call: str, number: int, writes: int) -> str:
    return call if writes == 1 else f"{call}:{number}"


def transcript_writes(host: SessionHost, window: Window) -> Iterator[Event]:
    readers: dict[str, Callable[[Path, Window, SessionHost], Iterator[Event]]] = {
        "claude": claude_writes, "codex": codex_writes}
    for transcript in host.transcripts(window.start - CALL_SLACK):
        yield from readers[transcript.agent](transcript.path, window, host)


def timeline(records: Records, host: SessionHost, window: Window, lane: str | None = None) -> list[Event]:
    """The window's events in time order; with `lane` (a workspace's name, path, or a tab's handle), its own."""
    inbox = entries(records.inbox)
    board = entries(records.board)
    lanes = names(inbox, board, host)
    found: Iterable[Event] = [
        *reports(inbox, window), *answers(records.answers, inbox, window, host), *decisions(records.decisions, window),
        *board_events(board, window, lanes), *holds(records.briefs / "holds.sqlite", window, lanes),
        *undelivered(records.briefs / "undelivered", window, lanes),
        *adoptions(records.adoption, records.adoption_imports, window), *transcript_writes(host, window)]
    wanted = lanes.of(lane) if lane and lane.startswith("term_") else Path(lane).name if lane else None
    return sorted((event for event in found if wanted is None or wanted in (event.lane, event.to)),
                  key=lambda event: event.at)


def moment(value: str) -> datetime:
    now = datetime.now(JST)
    if relative := re.fullmatch(r"(\d+)([mhd])", value):
        unit = {"m": "minutes", "h": "hours", "d": "days"}[relative[2]]
        return now - timedelta(**{unit: int(relative[1])})
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        return datetime.combine(now.date(), time.fromisoformat(value.zfill(5)), JST)
    parsed = when(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"時刻が読めません: {value}（例: 16:30、2026-11-15T16:30、3h）")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", required=True, type=moment,
                        help="この時刻から（16:30 は今日、2026-11-15T16:30、3h・90m・2d は今から遡る。時差の無い時刻は JST）")
    parser.add_argument("--until", type=moment, help="この時刻まで（--since と同じ書き方。省くと今まで）")
    parser.add_argument("--lane", help="この作業場が出した・受けた出来事だけ（名前、<repo>/<名前>、パス、端末の handle）")
    parser.add_argument("--match", help="本文にこの文字列を含む出来事だけ（大文字と小文字を区別しない）")
    parser.add_argument("--json", action="store_true", help="1 件 1 オブジェクトの JSON の配列で、本文を全文出す")
    parser.add_argument("--orca", default="orca", help="Orca の CLI（既定: orca）")
    args = parser.parse_args(argv)

    host = OrcaHost.from_environment(args.orca)
    events = timeline(Records.from_environment(), host, Window(args.since, args.until), args.lane)
    if args.match:
        events = [event for event in events if args.match.casefold() in event.text.casefold()]
    if args.json:
        print(json.dumps([event.row() for event in events], ensure_ascii=False, indent=2))
    else:
        print("\n".join(event.line() for event in events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
