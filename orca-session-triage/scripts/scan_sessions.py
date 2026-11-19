#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
"""Inventory Orca agent sessions from Orca state and agent transcripts.

GitHub calls per run, for the rows that get PR facts: one GraphQL query per
repository (25 PRs a query), plus three REST reads (reviews, conversation
comments, line comments) for each PR whose updatedAt changed since the copy
kept in --pr-cache.

A live scan whose every GitHub query, or every Jev call, failed to connect
prints only a line starting with ネットワーク断 (also on stderr), writes no
--review-out, and exits 3: scan again once the connection is back.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import cache, partial
from itertools import dropwhile, takewhile
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Literal, NamedTuple, NotRequired, TypedDict
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hourly_runtime
import inbox_read
from commitment_store import read_document, read_lines
import retro_ledger
import rm_check
from worktree_notes import keep_note

DEFAULT_CODEX_SESSIONS = [
    "~/Library/Application Support/orca/codex-accounts/*/home/sessions",
    "~/.codex/sessions",
]


CLAUDE_DIR_LIMIT = 200

# A line holding only one of these is a shell waiting for input: starship's
# default ❯, the ➜ and ✗ (success, error) another Mac's starship sets, or a plain
# $ or %. Prompts that carry text, such as `app=#` or `42%`, may belong to a
# running program, so they never count.
BARE_PROMPTS = ("❯", "➜", "✗", "$", "%")
Screens = dict[tuple[str | None, str], dict]
HOURLY_STATE = Path("~/.cache/orca-session-triage/hourly-state.json")
LEAD_DECISIONS = Path("~/.cache/orca-session-triage/lead-decisions.jsonl")


class PromptWaiting(TypedDict):
    tab: str
    question: str
    choices: list[str]
    target: NotRequired[str]  # what a permission prompt asks about (prompt_target)
    asked_at: NotRequired[str]


class DoneCard(TypedDict):
    agent: str | None
    handle: str | None
    done_at: str | None
    interrupted: bool
    prompt: str
    last_message: str
    screen_tail: list[str]
    draft: bool | None
    draft_text: str | None
    composer_empty: bool | None


def claude_project_dir(projects_root: str | Path, worktree_path: str) -> Path:
    """Claude Code stores a cwd's transcripts under the path with non-alphanumerics as '-'.

    Names longer than 200 characters are cut to 200 and given a hash suffix; see
    find_claude_dir for locating those.
    """
    return Path(projects_root) / re.sub(r"[^A-Za-z0-9]", "-", worktree_path)


def records_cwd(directory: Path, worktree_path: str) -> bool:
    for path in directory.glob("*.jsonl"):
        for row in load_jsonl(path):
            if "cwd" in row:
                if row["cwd"] == worktree_path:
                    return True
                break
    return False


def find_claude_dir(projects_root: str | Path, worktree_path: str) -> Path:
    exact = claude_project_dir(projects_root, worktree_path)
    if exact.is_dir() or len(exact.name) <= CLAUDE_DIR_LIMIT or not Path(projects_root).is_dir():
        return exact
    prefix = exact.name[:CLAUDE_DIR_LIMIT]
    for directory in Path(projects_root).iterdir():
        if directory.name.startswith(prefix) and records_cwd(directory, worktree_path):
            return directory
    return exact


def load_jsonl(path: str | Path) -> list[dict]:
    rows = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def message_of(entry: dict) -> dict:
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


class QueuedInput(TypedDict):
    text: str
    enqueued_at: str | None
    minutes: int | None


def queued_input(transcript: Path, now: datetime | None = None) -> list[QueuedInput]:
    """Instructions still queued after replaying the transcript's queue operations."""
    now = now or datetime.now(timezone.utc)
    pending: list[QueuedInput] = []
    for entry in load_jsonl(transcript):
        if entry.get("type") != "queue-operation":
            continue
        operation, content = entry.get("operation"), entry.get("content")
        if operation == "enqueue" and isinstance(content, str):
            stamp = entry.get("timestamp")
            when = parse_time(stamp)
            pending.append({"text": content, "enqueued_at": stamp,
                            "minutes": int((now - when).total_seconds() // 60) if when else None})
        elif operation == "dequeue" and pending:
            pending.pop(0)
        elif operation == "remove":
            index = next((i for i, item in enumerate(pending) if item["text"] == content), None)
            if index is not None:
                pending.pop(index)
        elif operation == "popAll":
            pending.clear()
    return pending


def user_text(entry: dict) -> str | None:
    content = message_of(entry).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def is_real_user_message(entry: dict) -> bool:
    """A message the user typed; meta entries and compaction summaries are Claude Code's own."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    value = user_text(entry)
    return bool(value) and not value.lstrip().startswith("<")


# Claude Code keeps <config dir>/sessions/<pid>.json for each running session, with its sessionId and current
# cwd; Orca runs Claude under one config dir per profile.
CLAUDE_SESSION_DIRS = (".claude/sessions", "Library/Application Support/orca/claude-profiles/*/home/sessions")
CLAUDE_PROJECT_DIRS = (".claude/projects", "Library/Application Support/orca/claude-profiles/*/home/projects")


def claude_session_dirs() -> list[Path]:
    return [path for pattern in CLAUDE_SESSION_DIRS for path in Path.home().glob(pattern) if path.is_dir()]


def claude_project_roots() -> list[Path]:
    """Every Claude account's projects directory, once each: an Orca profile may link its own to ~/.claude/projects."""
    found = [path for pattern in CLAUDE_PROJECT_DIRS for path in Path.home().glob(pattern) if path.is_dir()]
    return list({path.resolve(): path for path in found}.values())


def newest_copy(paths: Iterable[Path]) -> Path | None:
    """The copy whose newest timestamped entry is latest: a session resumed in another account keeps writing only there,
    while its first account's copy stays behind."""
    def entry_at(path: Path) -> tuple[datetime, float]:
        try:
            return last_entry_at(path) or EPOCH, path.stat().st_mtime
        except OSError:
            return EPOCH, 0.0
    return max({path.resolve(): path for path in paths}.values(), key=entry_at, default=None)


def process_table() -> dict[int, tuple[int, str]]:
    """Each process's parent and command name, from ps; empty when ps fails."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True,
                             timeout=ORCA_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        pid, ppid, command = [*line.split(None, 2), "", "", ""][:3]
        if pid.isdigit() and ppid.isdigit():
            table[int(pid)] = (int(ppid), command)
    return table


def process_cwds(pids: list[int]) -> dict[int, str]:
    """Each process's working directory from one lsof call; one lsof cannot read is left out."""
    if not pids:
        return {}
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-Fpn"],
                             capture_output=True, text=True, timeout=ORCA_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    cwds, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            cwds[pid] = line[1:]
    return cwds


NAMED_SESSION = re.compile(r"\s(?:--resume|-r|--session-id)[ =]"
                           r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\s|$)")


class TabProcess(NamedTuple):
    handle: str | None  # ORCA_TERMINAL_HANDLE: the Orca tab it runs in
    session: str | None  # the Claude session its --resume or --session-id names


def tab_processes(pids: list[int]) -> dict[int, TabProcess]:
    """Each process's Orca tab (rm_check.process_handles) and the Claude session its arguments name; one with
    neither is left out."""
    if not pids:
        return {}
    handles = rm_check.process_handles(pids)
    try:
        out = subprocess.run(["ps", "-ww", "-o", "pid=,args=", "-p", ",".join(map(str, pids))],
                             capture_output=True, text=True, timeout=ORCA_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    sessions = {}
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and (named := NAMED_SESSION.search(" " + args)):
            sessions[int(pid)] = named[1]
    return {pid: TabProcess(handles.get(pid), sessions.get(pid)) for pid in {*handles, *sessions}}


def ancestry(pid: int, processes: dict[int, tuple[int, str]]) -> list[int]:
    """The process and its ancestors, nearest first."""
    chain = []
    while pid in processes and pid not in chain:
        chain.append(pid)
        pid = processes[pid][0]
    return chain


def session_transcript(session_id: str, cwd: str, roots: Sequence[Path]) -> Path | None:
    """The newest copy of a session's transcript under its cwd's project directory, or under any one after its cwd
    moved."""
    name = session_id + ".jsonl"
    roots = list(dict.fromkeys(roots))
    exact = [path for root in roots if (path := find_claude_dir(root, cwd) / name).is_file()]
    return newest_copy(exact) or newest_copy(path for root in roots if root.is_dir() for path in root.glob("*/" + name))


def running_claude_transcripts(worktree_paths: Sequence[str], session_dirs: Sequence[Path], projects_root: str | Path,
                               processes: dict[int, tuple[int, str]],
                               cwds: Callable[[list[int]], dict[int, str]], tabs: Mapping[str, str] | None = None,
                               tab_of: Callable[[list[int]], dict[int, TabProcess]] = tab_processes,
                               session_details: dict[str, list[dict]] | None = None
                               ) -> dict[str, list[Path]]:
    """The transcripts of running Claude sessions by the worktree each runs under.

    A session can move its cwd out of the worktree (into a worktree under .claude/worktrees, or as a
    background session its tab's Claude started), or start outside it, and its transcript then lands in another
    project directory. `tabs` maps an Orca tab's handle to its worktree: a session whose process or ancestor
    carries one of them in ORCA_TERMINAL_HANDLE (read by `tab_of`) belongs to that tab's worktree, and a Claude in
    such a tab with no session file is known by the session its --resume or --session-id names. Otherwise its
    worktree is the first of the process and its ancestors whose cwd, from its session file or else from lsof,
    is inside one.
    `session_details`, when supplied, also receives live session records whose tab is absent,
    even when no transcript was found for them.
    """
    live = {}
    records = {}
    for directory in session_dirs:
        for path in directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            pid, session_id, cwd = record.get("pid"), record.get("sessionId"), record.get("cwd")
            if (isinstance(pid, int) and "claude" in processes.get(pid, (0, ""))[1] and isinstance(cwd, str)
                    and isinstance(session_id, str) and re.fullmatch(r"[\w-]+", session_id)):
                live[pid] = (session_id, cwd, directory.parent / "projects")
                records[pid] = record
    tab_paths: Mapping[str, str] = tabs or {}
    claude = [pid for pid, (_, command) in processes.items() if "claude" in command] if tab_paths or session_details is not None else []
    in_tab = tab_of(sorted({member for pid in claude for member in ancestry(pid, processes)})) if claude else {}

    def tab_worktree(pid: int) -> str | None:
        """The worktree of the first Orca tab the process or an ancestor names in ORCA_TERMINAL_HANDLE."""
        return next((path for member in ancestry(pid, processes)
                     if (path := tab_paths.get((in_tab.get(member) or TabProcess(None, None)).handle or ""))), None)

    # 2026-11-09: montly-report's tab resumed Claude in another checkout, and no sessions/<pid>.json was written.
    named = {pid: tab.session for pid in claude
             if pid not in live and (tab := in_tab.get(pid)) and tab.session and tab_worktree(pid)}
    chains = {pid: ancestry(pid, processes) for pid in [*live, *named]}
    known = {pid: cwd for pid, (_, cwd, _) in live.items()}
    where = {**cwds(sorted({member for chain in chains.values() for member in chain} - known.keys())), **known}
    live.update({pid: (session, where.get(pid, ""), Path(projects_root)) for pid, session in named.items()})
    # Without its session file, the profile that wrote the transcript is unknown: search each one's projects.
    profiles = [directory.parent / "projects" for directory in session_dirs]

    def worktree_of(cwd: str) -> str | None:
        path = max((path for path in worktree_paths if cwd == path or cwd.startswith(path.rstrip("/") + "/")),
                   key=len, default=None)
        # A checkout nested inside it, such as a worktree Claude made under .claude/worktrees, is not that worktree.
        inside = Path(cwd)
        if path and any((directory / ".git").exists()
                        for directory in [inside, *inside.parents][:len(inside.parts) - len(Path(path).parts)]):
            return None
        return path

    found: dict[str, set[Path]] = {}
    for pid, (session_id, cwd, own_projects) in live.items():
        worktree = (tab_worktree(pid)
                    or next((path for member in chains[pid] if (path := worktree_of(where.get(member, "")))), None))
        transcript = session_transcript(session_id, cwd, [Path(projects_root), own_projects,
                                                          *(profiles if pid in named else [])])
        if worktree and transcript:
            found.setdefault(worktree, set()).add(transcript)
        handle = next((tab.handle for member in chains[pid] if (tab := in_tab.get(member)) and tab.handle), None)
        if worktree and session_details is not None and pid in records and handle not in tab_paths:
            record = records[pid]
            session_details.setdefault(worktree, []).append({
                "pid": pid, "ORCA_TERMINAL_HANDLE": handle,
                "status": record.get("status"), "waitingFor": record.get("waitingFor")})
    return {worktree: sorted(paths) for worktree, paths in found.items()}


# Lines read from the end of a transcript for its newest entry.
TAIL_LINES = 400
EPOCH = datetime.fromtimestamp(0, timezone.utc)
JST = timezone(timedelta(hours=9))


def last_entry_at(path: Path) -> datetime | None:
    """The newest timestamped entry among a transcript's last lines. Claude Code also rewrites an idle session's
    transcript with untimed lines (bridge-session, cost-state), which do not count. An unreadable file raises OSError.
    """
    times = []
    for line in path.read_bytes().splitlines()[-TAIL_LINES:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and (at := parse_time(entry.get("timestamp"))) is not None:
            times.append(at)
    return max(times, default=None)


def one_shot_schedules(rows: list[dict]) -> list[dict]:
    """Replay confirmed CronCreate/CronDelete results and prompts already delivered in this conversation."""
    calls: dict[str, dict] = {}
    jobs: dict[str, dict] = {}
    for entry in rows:
        if entry.get("type") == "user" and (spoken := user_text(entry)):
            jobs = {key: job for key, job in jobs.items() if job["prompt"] not in spoken}
        content = message_of(entry).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in ("CronCreate", "CronDelete"):
                calls[block["id"]] = block
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                call = calls.pop(block.get("tool_use_id"), None)
                if not call:
                    continue
                given = call.get("input") or {}
                if call["name"] == "CronDelete":
                    jobs.pop(given.get("id"), None)
                elif given.get("recurring") is False and given.get("prompt"):
                    result = block.get("content")
                    raw = result if isinstance(result, str) else json.dumps(result)
                    if match := re.search(r"Scheduled one-shot (?:task|job) ([\w-]+)", raw):
                        jobs[match[1]] = {"id": match[1], "cron": given.get("cron") or "時刻不明",
                                          "prompt": given["prompt"]}
    return list(jobs.values())


def newest_claude_transcript(projects_root: str | Path, worktree_path: str, running: Sequence[Path] = ()) -> Path | None:
    """The worktree's newest Claude transcript across every account, preferring by last entry a running session's."""
    roots = {root.resolve(): root for root in (Path(projects_root), *claude_project_roots())}.values()
    newest = [max(found, key=lambda p: p.stat().st_mtime) for root in roots
              if (found := list(find_claude_dir(root, worktree_path).glob("*.jsonl")))]
    path = newest_copy(newest)
    if running:
        # The transcript a session continued from keeps getting untimed lines, which move its mtime alone.
        path = max(dict.fromkeys([*running, *([path] if path else [])]),
                   key=lambda p: (last_entry_at(p) or EPOCH, p.stat().st_mtime))
    return path


def read_claude(projects_root: str | Path, worktree_path: str, running: Sequence[Path] = ()) -> dict | None:
    """Summarize the newest Claude transcript for a worktree, or None when there is none.

    `running` adds transcripts of Claude sessions running under it that live in other project directories. Every
    account's projects directory is searched, since one session can leave a copy in each account it ran in.
    """
    path = newest_claude_transcript(projects_root, worktree_path, running)
    if path is None:
        return None
    rows = load_jsonl(path)
    last = max((i for i, e in enumerate(rows) if is_real_user_message(e)), default=-1)
    texts, final_at, asked, answered, api_error, api_error_kind = [], None, {}, set(), None, None
    question_times: dict[str, str] = {}
    call_times: dict[str, str] = {}
    for entry in rows[last + 1:]:
        content = message_of(entry).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if entry.get("type") == "assistant" and kind == "text" and block["text"].strip():
                texts.append(block["text"])
                final_at = entry.get("timestamp")
                api_error = block["text"] if entry.get("isApiErrorMessage") else None
                api_error_kind = entry.get("error") if api_error else None
            elif kind == "tool_use" and block.get("name") == "AskUserQuestion":
                asked[block.get("id")] = block.get("input")
                if entry.get("timestamp"):
                    question_times[block.get("id")] = entry["timestamp"]
            elif kind == "tool_use" and entry.get("timestamp"):
                call_times[block.get("id")] = entry["timestamp"]
            elif kind == "tool_result":
                answered.add(block.get("tool_use_id"))
    return {
        "transcript": str(path),
        "last_user": user_text(rows[last]) if last >= 0 else None,
        "last_user_at": rows[last].get("timestamp") if last >= 0 else None,
        "final_text": texts[-1] if texts else None,
        "final_at": final_at,
        "pending_questions": [q for i, q in asked.items() if i not in answered],
        "pending_question_at": max((at for key, at in question_times.items() if key not in answered), default=None),
        "pending_call_at": max((at for key, at in call_times.items() if key not in answered), default=None),
        "api_error": api_error,
        "api_error_kind": api_error_kind,
        "one_shot_schedules": one_shot_schedules(rows),
    }


def message_text(payload: dict) -> str:
    return " ".join(c.get("text", "") for c in payload.get("content") or [] if isinstance(c, dict))


API_FAILURE = re.compile(
    r"(?:Selected model is at capacity|API Error:|rate limit(?:ed| exceeded)?|usage limit|"
    r"request failed|connection error|stream error|stream disconnected before completion|server error|context window)",
    re.IGNORECASE)


def api_failure(text: str | None) -> str | None:
    """Return a Codex API failure line, ignoring ordinary discussion of an error."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if (line.startswith("■ ") or line.startswith("API Error:")) and API_FAILURE.search(line):
            return line
    return None


def summarize_rollout(path: str | Path) -> tuple[str | None, dict]:
    """Return (cwd, summary) for one Codex rollout file."""
    cwd, asks, outputs, last_user, last_role = None, {}, set(), None, None
    last_assistant = last_assistant_at = api_error = api_error_at = None
    for row in load_jsonl(path):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        cwd = cwd or payload.get("cwd")
        kind = payload.get("type")
        if row.get("type") == "event_msg" and kind in ("error", "stream_error"):
            message = payload.get("message") or payload.get("error")
            if isinstance(message, str) and API_FAILURE.search(message):
                api_error, api_error_at = message.strip(), row.get("timestamp")
        # A task the service ended (a usage limit) completes with its error and no message.
        if row.get("type") == "event_msg" and kind == "task_complete" and isinstance(payload.get("error"), dict):
            message = payload["error"].get("message")
            if isinstance(message, str) and message.strip():
                api_error, api_error_at = message.strip(), row.get("timestamp")
        if kind in ("function_call", "custom_tool_call") and "user_input" in (payload.get("name") or ""):
            try:
                questions = json.loads(payload.get("arguments") or "{}").get("questions")
            except json.JSONDecodeError:
                questions = None
            asks[payload.get("call_id")] = {"asked_at": row.get("timestamp"), "questions": questions}
        elif kind in ("function_call_output", "custom_tool_call_output"):
            outputs.add(payload.get("call_id"))
        elif kind == "message" and payload.get("role") == "user":
            value = message_text(payload)
            if value and not value.lstrip().startswith(("<", "# AGENTS.md instructions")):
                # A new request starts a turn the earlier failure did not end.
                last_user, last_role = (row.get("timestamp"), value), "user"
                api_error = api_error_at = None
        elif kind == "message" and payload.get("role") == "assistant":
            value = message_text(payload)
            if value:
                last_assistant, last_assistant_at, last_role = value, row.get("timestamp"), "assistant"
                api_error = api_failure(value)
                api_error_at = row.get("timestamp") if api_error else None
    pending = []
    for call_id, ask in asks.items():
        if call_id in outputs:
            continue
        later = bool(last_user and ask["asked_at"] and last_user[0] > ask["asked_at"])
        pending.append({**ask, "answered_in_chat_later": later})
    return cwd, {
        "rollout": str(path),
        "last_user": last_user[1] if last_user else None,
        "last_user_at": last_user[0] if last_user else None,
        "last_assistant": last_assistant,
        "last_assistant_at": last_assistant_at,
        "last_role": last_role,
        "api_error": api_error,
        **({"api_error_at": api_error_at} if api_error_at else {}),
        "pending_requests": pending,
    }


# Jev (TypeSafe) answers typed questions about a text: a yes/no probability
# (noul) and a pick among named options with a confidence (choice).
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_QUESTIONS = {
    # Asked apart from the state: as one more option, "waits for your
    # instruction" read as in_progress.
    "asks_user": {"type": "noul", "instructions": (
        "Does this message end by waiting for the user's answer, decision, or instruction "
        "before the agent can continue?")},
    "status": {"type": "choice",
               "instructions": "What is the state of the agent's work, judging from its last message?",
               "criteria": {
                   "in_progress": "still working, or waiting on its own helper, background job, or CI",
                   # Agents end with what they did not verify; read as work left, a
                   # merged and finished worktree stayed stalled (2026-10-06).
                   "blocked_on_others": "its part is not finished until someone else acts, such as a "
                                        "reviewer who has to review or merge, a coordinator, or another team",
                   "done": "the work is finished; notes on what was not verified, or offers of "
                           "optional follow-up such as deleting a branch, do not count as work left"}},
}
JEV_MAX_CHARS = 4000  # the ask sits at the end; earlier context rarely changes the answer
JEV_TIMEOUT = 15
JEV_KEY_FILE = "~/.config/typesafe/api_key"
# Jev is sure from these bounds on; a score between them, or no score, is
# unsure and the final message is read before the row is classed.
ASKS_FROM = 0.7
SETTLED_BELOW = 0.3
STATE_SURE_FROM = 0.6  # a status confidence below this is unsure
NO_VERDICT = {"asks": None, "status": None, "confidence": None}
NETWORK_DOWN_EXIT = 3


class Reach:
    """Each network read of a scan, by service: None when it reached the service, else why it could not connect.

    A service whose every read failed to connect gave the scan nothing, and its rows would look settled
    without their PR facts or Jev's verdicts; `down` names it, so main stops before writing them.
    """

    def __init__(self) -> None:
        self.reads: dict[str, list[str | None]] = {"GitHub": [], "Jev": []}

    def record(self, service: str, failure: str | None) -> None:
        self.reads[service].append(" ".join(failure.split())[:200] if failure else None)

    def down(self) -> list[str]:
        return [f"{service} への {len(reads)} 回の読み取りがすべて接続できませんでした（{reads[-1]}）"
                for service, reads in self.reads.items() if reads and all(reads)]


def jev_key(env: Mapping[str, str], key_file: str | Path) -> str | None:
    """TYPESAFE_API_KEY from the environment, else the key file's content, else None."""
    if env.get("TYPESAFE_API_KEY"):
        return env["TYPESAFE_API_KEY"]
    try:
        return Path(key_file).expanduser().read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def probability(value: str | float) -> float:
    number = float(value)
    if not 0 <= number <= 1:  # also false for NaN
        raise ValueError(f"probability out of range: {number}")
    return number


def jev_judge(api_key: str, opener: Callable[..., AbstractContextManager] | None = None,
              reach: Reach | None = None) -> Callable[[str], dict | None]:
    """A judge returning Jev's verdict on a message, or None on failure.

    The verdict holds `asks` (probability that it waits on the user), `status`
    (in_progress, blocked_on_others, or done), and that status's `confidence`.
    Each call goes into `reach`: an OSError other than an HTTP answer never reached Jev.
    """
    def judge(message: str) -> dict | None:
        body = {"state": message[-JEV_MAX_CHARS:], "model": "jev-latest", "questions": JEV_QUESTIONS}
        request = urllib.request.Request(JEV_URL, data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        unreachable = None
        try:
            with (opener or urllib.request.urlopen)(request, timeout=JEV_TIMEOUT) as response:
                answers = json.load(response)["answers"]
            status = answers["status"]["choice"]
            if status not in JEV_QUESTIONS["status"]["criteria"]:
                raise ValueError(f"unknown status: {status!r}")
            return {"asks": probability(answers["asks_user"]["noul"]), "status": status,
                    "confidence": probability(answers["status"]["confidence"])}
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
            if isinstance(error, OSError) and not isinstance(error, urllib.error.HTTPError):
                unreachable = repr(error)
            print(f"Jev could not judge a final message: {error!r}", file=sys.stderr)
            return None
        finally:
            if reach is not None:
                reach.record("Jev", unreachable)
    return judge


def is_working(row: dict) -> bool:
    return any(a["state"] == "working" for a in row["agents"])


def main_state(agent: dict) -> str | None:
    main = agent.get("mainAgent")
    return main.get("state") if isinstance(main, dict) else None


def turn_runs(row: dict) -> bool:
    """An agent is in its own turn, not only holding a background shell, monitor, or helper.

    Orca keeps `state` at working while such a job runs after the turn ended;
    `mainAgent.state` then says done.
    """
    return any(a["state"] == "working" and a.get("turn") != "done" for a in row["agents"])


def latest_message(row: dict) -> tuple[str, str] | None:
    """(source, text) of the newest final message, Claude's when the times tie or are missing.

    A helper's report to its lead comes before the lead's own last word, so only
    the newest one says how the worktree stands. Codex counts only when it spoke
    after the user.
    """
    claude = row["claude"] or {}
    latest = row["codex"][-1] if row["codex"] else {}
    codex_spoke = latest.get("last_role") == "assistant" and latest.get("last_assistant")
    if not claude.get("final_text"):
        if codex_spoke:
            return "codex", latest["last_assistant"]
        return ("orca", row["orca_message"]["text"]) if row.get("orca_message") else None
    claude_at, codex_at = claude.get("final_at"), latest.get("last_assistant_at")
    if codex_spoke and claude_at and codex_at and codex_at > claude_at:
        return "codex", latest["last_assistant"]
    # A Codex tab's reply, read from its screen, that came after Claude's last word (a lane Codex took over).
    shown = row.get("orca_message") or {}
    shown_at, said_at = parse_time(shown.get("at")), parse_time(claude_at)
    if shown.get("from") == "screen" and shown_at and said_at and shown_at > said_at:
        return "orca", shown["text"]
    return "claude", claude["final_text"]


def failure(row: dict) -> tuple[str, str | None, str | None] | None:
    """(error, agent, handle) of the API failure that ended the last Claude or Codex turn, or None.

    Claude Code does not resume such a turn, and a monitor it left running keeps
    Orca at working, so only the transcript shows the agent stopped mid-task.
    A failure Codex's tab still shows can be an earlier turn's: it counts only
    while no turn runs. A failure of an agent another one Orca shows in its own
    turn took the worktree over from is history.
    """
    running = {a.get("type") for a in row.get("agents") or [] if a.get("state") == "working" and a.get("turn") != "done"}
    approval_tabs = {prompt["tab"] for prompt in row.get("codex_approvals") or []}
    claude_tabs = [term["handle"] for term in row.get("terminals", [])
                   if term.get("agent") == "claude" and term.get("handle")]
    claude_tab = claude_tabs[0] if approval_tabs and len(claude_tabs) == 1 else None
    rollouts = row["codex"]
    codex_failures = (sorted((item for item in rollouts if item.get("api_error")),
                             key=lambda item: parse_time(item.get("api_error_at")) or EPOCH, reverse=True)
                      if approval_tabs else rollouts[-1:])
    message = row.get("orca_message") or {}
    orca = " ".join((message.get("text") or "").split())
    screen_tab = row.get("codex_screen_error_tab")
    found = [*row.get("pane_errors", []),
             (row.get("codex_screen_error") if not running or approval_tabs and screen_tab else None,
              "codex", screen_tab, None),
             *((item.get("api_error"), "codex", None, item.get("api_error_at")) for item in codex_failures),
             ((row["claude"] or {}).get("api_error"), "claude", claude_tab, None),
             (orca if not (row["claude"] or row["codex"] or message.get("resumed")) and API_ENDED.match(orca) else None,
              message.get("agent"), message.get("tab"), message.get("at"))]
    approval_observed = row.get("approval_observed_at") or row.get("scanned_at")
    approval_captures = row.get("approval_capture_by_tab") or {}
    for error, agent, tab, at in found:
        if not error:
            continue
        # Legacy rollout entries have no terminal handle. Pane evidence is preferred; an unbound
        # Codex error from before the current approval screen does not identify a pane to wake.
        if tab in approval_tabs or not tab and agent == "codex" and approval_tabs:
            observed = parse_time(approval_captures.get(tab, approval_observed))
            if not observed or (parse_time(at) or EPOCH) <= observed:
                continue
        if agent and running and agent not in running and not (tab and approval_tabs):
            continue
        return error, agent, tab
    return None


def cut_off(row: dict) -> str | None:
    """The API error that ended the last Claude or Codex turn (failure), or None."""
    found = failure(row)
    return found[0] if found else None


# The line an API failure leaves as an agent's whole last output, read from Orca's copy when no transcript is here.
API_ENDED = re.compile(r"(?:API Error:|You['’]ve hit your [\w ]+ limit|Login expired|Not logged in|Please run /login|"
                       r"Failed to authenticate|Prompt is too long|"
                       r"(?:■\s*)?(?:stream disconnected before completion|Codex ran out of room in the model's context))")
USAGE_LIMIT = re.compile(r"hit your [\w ]+ limit|usage limit", re.IGNORECASE)
LOGIN_EXPIRED = re.compile(r"Login expired|Please run /login|not logged in|Failed to authenticate", re.IGNORECASE)
# Without Claude Code's tag (Codex, Orca's copy), the text names an error a retry repeats: a safeguard flag, or a
# request the API refused (a 4xx other than a rate limit).
NOT_RETRYABLE = re.compile(r"safeguards flagged|invalid_request|API Error: 4(?!29)\d\d|credit balance", re.IGNORECASE)
# The conversation outgrew the model's context window: a new lane with a smaller scope continues, not a wake.
CONTEXT_LIMIT = re.compile(r"prompt is too long|conversation too long|context window|context_length_exceeded",
                           re.IGNORECASE)
RESETS = re.compile(r"(?:resets|try again at)\s+(.+?)\.?$")


class StopReason(TypedDict):
    kind: str  # connection, usage_limit, login, context_limit, or other
    action: str  # wake, wait (until resets), human, or new_lane
    agent: str | None
    error: str
    resets: str | None
    tab: NotRequired[str]


def stop_reason(row: dict) -> StopReason | None:
    """Why an API failure ended the last turn: a lost connection to wake from, a usage limit to wait out, an
    expired login a person has to renew, a full context window a new lane with a smaller scope gets past, or
    another error a person reads (a safeguard flag, an invalid request), which waking would only repeat. Claude Code tags its error message (`server_error`, `rate_limit`,
    `authentication_failed`, `invalid_request`); Codex and Orca's copy are read by their text."""
    if not (found := failure(row)):
        return None
    error, agent, tab = found
    source = {"tab": tab} if tab else {}
    tag = (row.get("claude") or {}).get("api_error_kind") if agent == "claude" else None
    if tag == "rate_limit" or USAGE_LIMIT.search(error):
        resets = RESETS.search(error)
        return {"kind": "usage_limit", "action": "wait", "agent": agent, "error": error,
                "resets": resets[1] if resets else None, **source}
    if tag == "authentication_failed" or LOGIN_EXPIRED.search(error):
        return {"kind": "login", "action": "human", "agent": agent, "error": error, "resets": None, **source}
    if CONTEXT_LIMIT.search(error):
        return {"kind": "context_limit", "action": "new_lane", "agent": agent, "error": error, "resets": None, **source}
    if tag not in (None, "server_error") or tag is None and NOT_RETRYABLE.search(error):
        return {"kind": "other", "action": "human", "agent": agent, "error": error, "resets": None, **source}
    return {"kind": "connection", "action": "wake", "agent": agent, "error": error, "resets": None, **source}


def stop_line(stop: StopReason) -> str:
    if stop["kind"] == "usage_limit":
        return "usage limit; resets " + stop["resets"] if stop["resets"] else "usage limit: " + stop["error"]
    if stop["kind"] == "login":
        return "login expired; a person has to log in again: " + stop["error"]
    if stop["kind"] == "context_limit":
        return "context window full; hand the rest to a new lane with a smaller scope: " + stop["error"]
    if stop["kind"] == "other":
        return "turn ended with an API error waking does not fix: " + stop["error"]
    return "turn ended with an API error: " + stop["error"]


def is_sure_ask(asks: float | None) -> bool:
    return asks is not None and asks >= ASKS_FROM


def unsure_reason(jev: dict) -> str | None:
    if jev["asks"] is None:
        return "Jev could not judge: read the final message"
    if SETTLED_BELOW <= jev["asks"] < ASKS_FROM:
        return f"Jev unsure whether it asks ({jev['asks']:.2f}): read the final message"
    if jev["confidence"] < STATE_SURE_FROM:
        return f"Jev unsure of the state ({jev['status']} {jev['confidence']:.2f}): read the final message"
    return None


BACKGROUND_REASON = "a background job runs after the agent's turn"
STATE_REASONS = {
    "in_progress": "final message says work continues, yet no agent runs",
    "blocked_on_others": "final message waits on someone else",
    "done": "final message says done",
}


def prompt_question(prompt: dict) -> str:
    """A prompt's question, followed by what a permission prompt asks about."""
    question = prompt.get("question") or ""
    return f"{question} — {prompt['target']}" if question and prompt.get("target") else question


def waiting_reasons(row: dict, decision_marker: str) -> list[str]:
    reasons = []
    primary = row.get("prompt_waiting")
    prompts = [*([primary] if primary else []), *(prompt for prompt in row.get("codex_approvals") or []
                                                  if prompt != primary)]
    for prompt in prompts:
        kind = "permission prompt" if prompt["question"].startswith((PERMISSION_QUESTION, CODEX_PERMISSION, CODEX_APPROVAL)) else "question"
        reasons.append(f"{kind} on screen: " + prompt_question(prompt))
    claude = row["claude"] or {}
    jev = row.get("jev") or {}
    asks = jev.get("asks")
    if claude.get("pending_questions"):
        reasons.append("AskUserQuestion pending")
    elif is_sure_ask(asks) and jev["source"] == "claude":
        reasons.append(f"final message asks (Jev {asks:.2f})")
    if any(not r["answered_in_chat_later"] for s in row["codex"] for r in s["pending_requests"]):
        reasons.append("Codex request pending")
    if is_sure_ask(asks) and jev["source"] == "codex":
        reasons.append(f"Codex final message asks (Jev {asks:.2f})")
    if is_sure_ask(asks) and jev["source"] == "orca":
        reasons.append(f"Orca final message asks (Jev {asks:.2f})")
    if any(a["state"] == "waiting" for a in row["agents"]):
        reasons.append("Orca agent waiting")
    if decision_marker and (row["comment"] or "").startswith(decision_marker):
        reasons.append(MARKER_REASON)
    return reasons


MARKER_REASON = "decision marker in comment"
OLD_MARKER_REASON = "old decision marker in comment: an agent is in its turn"


NO_SESSION_REASON = "no agent session in this worktree"
# A meaningful tab Orca gives no handle is listed by its title after this.
NO_HANDLE = "(no handle) "


def classify(row: dict, decision_marker: str) -> tuple[str, list[str]]:
    """Return (class, reasons): waiting > unstarted > working > unsure > finished > stalled.

    Jev reads the newest final message of an idle worktree. When it is unsure,
    or gave no usable answer, the worktree is unsure: read the message and class
    it yourself before anything closes. Only work Jev calls done can finish; a
    stalled worktree has nothing running and nothing asked, yet is still open,
    and its reason says what Jev read (work continues, waits on someone else,
    or done but not merged). A worktree Orca calls working only for a background
    job is read too: a question makes it waiting or unsure, and it never
    finishes while the job runs. A turn an API error cut off, or a lead that
    left its workers' mail unread, is stalled even then; unread mail is also
    named on a waiting row. A turn an API failure ended is stalled first, even while Orca still shows it working:
    its reason says to wake it, wait until a usage limit resets, or have a person log in.
    """
    if stop := stop_reason(row):
        return "stalled", [stop_line(stop)]
    reasons = waiting_reasons(row, decision_marker)
    mail = [mail_reason(row["unread_mail"])] if row.get("unread_mail") else []
    progress = row.get("screen_progress")
    # An agent works past a decision it marked: the marker alone no longer waits on anyone.
    if reasons == [MARKER_REASON] and (turn_runs(row) or progress):
        return "working", [OLD_MARKER_REASON]
    if reasons:
        return "waiting", reasons + mail
    # The tab's screen is newer than a transcript or Orca state that says the turn ended.
    if progress:
        return "working", [f"{SCREEN_WORKING_REASON}: {progress}"]
    has_transcript = {"claude": bool(row["claude"]), "codex": bool(row["codex"])}
    silent = sorted({t["agent"] for t in row["terminals"] if t["agent"] in has_transcript
                     and not row.get("orca_message")
                     and not has_transcript[t["agent"]]})
    if silent:
        return "unstarted", [agent + " terminal without a recent transcript" for agent in silent]
    jev = row.get("jev")
    if turn_runs(row):
        return "working", []
    if mail:
        return "stalled", mail
    if is_working(row):
        if jev and (jev["asks"] is None or jev["asks"] >= SETTLED_BELOW):
            reason = unsure_reason(jev)
            assert reason is not None
            return "unsure", [reason]
        return "working", [BACKGROUND_REASON]
    if jev and (reason := unsure_reason(jev)):
        return "unsure", [reason]
    state = f"{STATE_REASONS[jev['status']]} (Jev {jev['confidence']:.2f})" if jev else None
    if jev and jev["status"] != "done":
        assert state is not None
        return "stalled", [state]
    if (row["linked_pr"] or {}).get("state") == "merged":
        return "finished", ["PR merged"]
    if row["status"] == "completed" and (row["linked_pr"] or {}).get("state") != "open":
        return "finished", ["board status completed"]
    # Another Mac's tab may show its agent only on its screen; this Mac's transcripts are read directly, and a
    # local Codex with no recent rollout spoke on its tab's screen.
    screen_agent_tab = ((row.get("environment") or (row.get("orca_message") or {}).get("from") == "screen")
                        and any(t["agent"] for t in row["terminals"]))
    if not (row["agents"] or row["claude"] or row["codex"] or screen_agent_tab):
        return "stalled", [NO_SESSION_REASON]
    return "stalled", [state] if state else []


def shell_is_idle(term: dict, now: float, idle_minutes: float, screen: dict | None = None) -> bool:
    """A quiet shell at a bare prompt; remote shells without output metadata use their rendered screen."""
    last_output = term.get("lastOutputAt")
    if term.get("_environment") and not term.get("preview") and last_output is None:
        lines = [line.strip() for line in screen_lines(rendered(screen) or {}) if line.strip()]
        return bool(lines) and lines[-1] in BARE_PROMPTS
    lines = [line.strip() for line in (term.get("preview") or "").splitlines() if line.strip()]
    quiet = last_output is not None and now - last_output / 1000 >= idle_minutes * 60
    return bool(lines) and lines[-1] in BARE_PROMPTS and quiet


def agents_done(row: dict) -> bool:
    """Nothing is pending on the agents: closing their tabs loses no work.

    An agent whose last message says work continues may be waiting on a
    background job that its tab still holds. A turn an API error cut off, and a
    lead with unread worker mail, are woken in that tab.
    """
    still_working = (row.get("jev") or {}).get("status") == "in_progress"
    to_wake = cut_off(row) or row.get("unread_mail")
    return row["class"] in ("finished", "stalled") and not still_working and not to_wake


def closable_terminals(agents_closable: bool, terms: list[dict], now: float, idle_minutes: float,
                       screens: Screens) -> list[str]:
    """Handles to close in a kept worktree: idle shells, plus agents once nothing is pending."""
    return [t["handle"] for t in terms if t.get("handle")
            and (agents_closable if t.get("agentIdentity") else shell_is_idle(
                t, now, idle_minutes, screens.get((t.get("_environment"), t["handle"]))))]


def iso_time(epoch_ms: float | None) -> str | None:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCREEN_TAIL_LINES = 8
RULE = re.compile(r"[─━]+")
# Claude Code's reply bullet and the line it leaves when a turn ends ("✻ Baked for 3m 2s").
CLAUDE_LINE_STARTS = re.compile(r"⏺ |✻ \S+ for ")
CLAUDE_TURN_END = re.compile(r"✻ \S+ for ")
CLAUDE_MODE_LINE = "⏵⏵ "
# The status line Claude Code shows while its turn runs: a spinner glyph, a verb, and how long the turn has run
# ("· Composing… (17m 47s · ↓ 11.9k tokens · thinking more with xhigh effort)", "✢ Honking… (1m 24s · ↓ 3.3k tokens)").
CLAUDE_PROGRESS = re.compile(r"[·✢✳✶✻✽] \S+… \((?:\d+h )?(?:\d+m )?\d+s\b")
SCREEN_WORKING_REASON = "the tab's screen shows Claude working"
# A ⏺ line that names a tool call ("⏺ Bash(git push)", "⏺ serena - find_symbol (MCP)(…)"), sums up collapsed calls
# ("… (ctrl+o to expand)"), or is followed by a tool result (⎿) is not a reply.
TOOL_CALL = re.compile(r"⏺ (?:[A-Za-z][\w.:-]*\(|.*\(MCP\))")
COLLAPSED_CALLS = "(ctrl+o to expand)"
TOOL_RESULT = "⎿"
# Codex's reply bullet ("• text", its continuation lines indented), the line a turn ends with ("Worked for 4m 28s •
# 11:43"; "─ Worked for 1m 02s ───" in older builds), and the recap it can print under that line. A bullet followed
# by a └ result ("• Ran …", "• Hook failed"), a file change with its line counts ("• Edited app.py (+3 -1)"), or the
# status line ("• Working (12s • esc to interrupt)") is not a reply.
CODEX_REPLY = "• "
CODEX_TURN_END = re.compile(r"[─━ ]*Worked for \d[^•·─━]*(?:[•·] *\d{1,2}:\d{2})?[─━ ]*")
# The local clock time a turn-end line ends with ("Worked for 14m 48s • 15:06").
TURN_CLOCK = re.compile(r"[•·] *(\d{1,2}):(\d{2})")
CODEX_ACTION = re.compile(r"• (?:(?:Edited|Added|Deleted) .+\(\+\d+ -\d+\)|Working \(.+\))\s*")
CODEX_WORKING = re.compile(r"• Working \(")
CODEX_RESULT = "└"
CODEX_RECAP = "↳ Recap:"
CODEX_PROMPT = "› "


def screen_lines(screen: dict) -> list[str]:
    tail = screen.get("tail") or []
    return tail if isinstance(tail, list) else tail.splitlines()


def last_codex_activity(lines: list[str]) -> str | None:
    """The last Codex reply, action, or error in a terminal excerpt."""
    return next((line for raw in reversed(lines) if (line := raw.strip())
                 and (line.startswith(CODEX_REPLY) or api_failure(line))), None)


def rendered(screen: dict | None) -> dict | None:
    """The screen when Orca rendered it; None when the read failed or gave accumulated output."""
    return screen if screen and screen.get("source") == "screen" else None


def has_draft(screen: dict) -> bool:
    """Whether Orca reports a nonempty `draft`, which can also be Claude's suggestion."""
    return screen.get("draft") not in (None, "")


# Enough of an unsent draft to tell Claude's suggestion from the user's instruction; the rest is the user's
# private text and stays out of the scan and every saved copy of it.
DRAFT_EXCERPT = 60


def draft_excerpt(screen: dict) -> str:
    """The start of Orca's draft, whitespace collapsed, with … when cut."""
    said = " ".join((screen.get("draft") or "").split())
    return said[:DRAFT_EXCERPT] + ("…" if len(said) > DRAFT_EXCERPT else "")


def composer_empty(screen: dict) -> bool | None:
    """Whether Claude's last input line between rules is bare; None when no composer is visible."""
    lines = [line.strip() for line in screen_lines(screen)]
    rules = [index for index, line in enumerate(lines) if RULE.fullmatch(line)]
    for top, bottom in reversed(list(zip(rules, rules[1:]))):
        box = [line for line in lines[top + 1:bottom] if line]
        prompt = next((index for index, line in enumerate(box) if line.startswith("❯")), None)
        if prompt is not None:
            # A draft that starts with a newline leaves the ❯ line bare and its text on the lines below.
            return box[prompt:] == ["❯"]
    return None


# Claude Code's permission selector: "Do you want to make this edit to …?", then "❯ 1. Yes", "2. No", …,
# with ❯ on whichever choice the user has moved to.
PERMISSION_QUESTION = "Do you want to"
CODEX_PERMISSION = "Would you like to run the following command?"
CODEX_APPROVAL = "Codex approval"
CODEX_PERMISSION_FOOTER = re.compile(
    r"press (?:.+? to confirm(?: or .+? to cancel)?|.+? to cancel)(?: or .+? to open thread)?\.?", re.I)
CODEX_PERMISSION_CHOICE = re.compile(r"(?:[›❯]\s*)?(\d+\.\s+.+)")
CODEX_CLIPPED = re.compile(r"\[… \d+ lines?\](?:\s.*)?")
CODEX_BYPASS_FLAGS = frozenset(("--dangerously-bypass-approvals-and-sandbox", "--yolo"))
CHOICE = re.compile(r"(?:❯\s*)?(\d+\.\s+.+)")
FIRST_CHOICE = re.compile(r"(?:❯\s*)?1\.\s+.+")
# What Claude Code prints when a restart dropped the background jobs a turn was waiting on.
LOST_WAITS = "background shell command tasks didn't finish before the previous session ended"


# Above its question, Claude Code's permission frame names what it asks about: under a rule, the tool and the call's
# description ("Bash command", "Post the approved comment"), then sections split by dashed rules, each line behind a
# │ gutter: the command, and the reason a PreToolUse hook gave for asking, ending with where the hook is set.
DASHED_RULE = re.compile(r"╌+")
GUTTER = "│"
HOOK_ASKS = "Hook PreToolUse"
HOOK_SOURCE = re.compile(r"\s*\[\w+\]$")
COMMAND_HEAD = 100


def prompt_target(lines: list[str]) -> str:
    """The tool and its description, the command's first line, and a PreToolUse hook's reason from the permission
    frame in `lines` (stripped, above the question), joined with " / "; "" when the frame's top rule is not shown."""
    top = max((index for index, line in enumerate(lines) if RULE.fullmatch(line)), default=None)
    if top is None:
        return ""
    sections: list[list[str]] = [[]]
    for line in lines[top + 1:]:
        if DASHED_RULE.fullmatch(line):
            sections.append([])
        else:
            sections[-1].append(line)
    title, *about = sections[0] or [""]
    parts = [f"{title}: {unwrapped(about)}" if about else title]
    for section in sections[1:]:
        shown = [line.removeprefix(GUTTER).strip() for line in section if line.startswith(GUTTER)]
        if shown and shown[0].startswith(HOOK_ASKS):
            # The hook's heading can wrap; its reason starts after the line ending with ":".
            heading = next((index for index, line in enumerate(shown) if line.endswith(":")), 0)
            parts.append("Hook: " + HOOK_SOURCE.sub("", unwrapped(shown[heading + 1:])))
        elif shown and len(parts) == 1:
            cut = len(shown) > 1 or len(shown[0]) > COMMAND_HEAD
            parts.append(shown[0][:COMMAND_HEAD] + ("…" if cut else ""))
    return " / ".join(part for part in parts if part)


def permission_prompt(screen: dict, tab: str) -> PromptWaiting | None:
    """The permission question a rendered screen waits on, with its numbered choices and what it asks about."""
    lines = [line.strip() for line in screen_lines(screen) if line.strip()]
    asked = [index for index, line in enumerate(lines) if line.startswith(PERMISSION_QUESTION)]
    after = lines[asked[-1] + 1:] if asked else []
    first = next((index for index, line in enumerate(after) if FIRST_CHOICE.fullmatch(line)), None)
    choices = list(takewhile(CHOICE.fullmatch, after[first:])) if first is not None else []
    if not any(line.startswith("❯") for line in choices):
        return None
    target = prompt_target(lines[:asked[-1]])
    return {"tab": tab, "question": lines[asked[-1]], "choices": [CHOICE.sub(r"\1", line) for line in choices],
            **({"target": target} if target else {})}


def codex_permission_prompt(screen: dict, tab: str) -> PromptWaiting | None:
    displayed = [line.strip() for line in screen_lines(screen)]
    visible = [index for index, line in enumerate(displayed) if line]
    lines = [displayed[index] for index in visible]
    footer = next((index for index in range(max(0, len(lines) - 5), len(lines))
                   if CODEX_PERMISSION_FOOTER.fullmatch(" ".join(lines[index:]))), None)
    if footer is None:
        return None
    choices: list[str] = []
    end = footer
    first = None
    selected = False
    for index in range(footer - 1, -1, -1):
        if choice := CODEX_PERMISSION_CHOICE.fullmatch(lines[index]):
            choices.append(" ".join([choice[1], *lines[index + 1:end]]))
            selected = selected or lines[index].startswith(("›", "❯"))
            end = index
            if choice[1].startswith("1."):
                first = index
                break
    if first is None or not selected or len(choices) < 2:
        return None
    choices.reverse()
    heading_end = next((end for end in range(first - 1, -1, -1)
                        if any(" ".join(lines[start:end + 1]) == CODEX_PERMISSION
                               for start in range(max(0, end - 3), end + 1))), None)
    if heading_end is None:
        if first and CODEX_CLIPPED.fullmatch(lines[first - 1]):
            return {"tab": tab, "question": CODEX_APPROVAL, "choices": choices,
                    "target": "approval details omitted from terminal view"}
        return None
    body = displayed[visible[heading_end] + 1:visible[first]]
    command = next((index for index, line in enumerate(body)
                    if line.startswith("$ ") and (index == 0 or not body[index - 1])), None)
    if command is None and all(body):
        # Orca's screen projection removes blank rows. Competing markers or a clipped body leave the target unknown.
        candidates = [index for index, line in enumerate(body) if line.startswith("$ ")]
        clipped = any(CODEX_CLIPPED.fullmatch(line) or line.startswith("Input:") for line in body)
        if len(candidates) == 1 and not clipped:
            command = candidates[0]
    if command is None:
        target = "command omitted from terminal view"
    else:
        shown = body[command]
        target = shown[:COMMAND_HEAD] + ("…" if len(shown) > COMMAND_HEAD or any(body[command + 1:]) else "")
    return {"tab": tab, "question": CODEX_PERMISSION, "choices": choices, "target": target}


def lost_background_waits(agents: list[dict], terms: list[dict], screens: Screens,
                          environment: str | None) -> list[str]:
    """A reason per screen line saying a restart dropped background waits, timed by its pane's agent when known."""
    since = {agent["paneKey"]: iso_time(agent.get("stateStartedAt")) for agent in agents if agent.get("paneKey")}
    return [f"background waits lost when the session was replaced (tab {term.get('handle')}"
            + (f", agent state since {at}" if (at := since.get(hourly_runtime.pane_key(term))) else "")
            + f"): {line.strip()}"
            for term in terms for line in screen_lines(rendered(screens.get((environment, term.get("handle")))) or {})
            if LOST_WAITS in line]


# Claude Code's AskUserQuestion: a row of question tabs ("☐ 範囲  ☒ 期限  ✔ Submit"), the current question, its
# numbered choices with their descriptions, and a footer saying how to answer.
SELECTOR_FOOTER = re.compile(r"Enter to select · .*to navigate")
QUESTION_TABS = re.compile(r"[☐☒]")


def unwrapped(lines: list[str]) -> str:
    """Lines the screen wrapped, joined with a space only between two ASCII characters: Japanese wraps anywhere."""
    text = ""
    for line in lines:
        text += (" " if text[-1:].isascii() and text and line[:1].isascii() else "") + line
    return text


def question_prompt(screen: dict, tab: str) -> PromptWaiting | None:
    """The question an AskUserQuestion selector at the end of a rendered screen waits on, with its numbered choices."""
    lines = [line.strip() for line in screen_lines(screen)]
    shown = [index for index, line in enumerate(lines) if line]
    footer = next((index for index in reversed(shown[-3:]) if SELECTOR_FOOTER.search(lines[index])), None)
    first = max((index for index in range(footer) if FIRST_CHOICE.fullmatch(lines[index])), default=None) \
        if footer is not None else None
    if first is None:
        return None
    choices = [line for line in lines[first:footer] if CHOICE.fullmatch(line)]
    if not any(line.startswith("❯") for line in choices):
        return None
    above = [line.lstrip("│").strip() for line in lines[:first]]
    frame = max((index for index, line in enumerate(above) if RULE.fullmatch(line) or QUESTION_TABS.search(line)),
                default=None)
    asked = ([line for line in above[frame + 1:] if line] if frame is not None
             else list(takewhile(bool, dropwhile(lambda line: not line, reversed(above))))[::-1])
    return {"tab": tab, "question": unwrapped(asked), "choices": [CHOICE.sub(r"\1", line) for line in choices]}


def waiting_prompts(terms: list[dict], screens: Screens,
                    environment: str | None) -> Iterator[tuple[PromptWaiting, Literal["pending_call_at", "pending_question_at"] | None]]:
    """Each prompt a tab's screen waits on, with the transcript field that says when it was asked: a permission
    prompt holds back the tool call it asks about, and a selector is an AskUserQuestion call."""
    for term in terms:
        if not term.get("handle") or not (screen := rendered(screens.get((environment, term["handle"])))):
            continue
        if prompt := codex_permission_prompt(screen, term["handle"]):
            yield prompt, None
        elif prompt := permission_prompt(screen, term["handle"]):
            yield prompt, "pending_call_at"
        elif prompt := question_prompt(screen, term["handle"]):
            yield prompt, "pending_question_at"


def screen_tail(screen: dict) -> list[str]:
    """The last lines a rendered screen shows, without blank lines and rules."""
    shown = [line.strip() for line in screen_lines(screen) if line.strip() and not RULE.fullmatch(line.strip())]
    return shown[-SCREEN_TAIL_LINES:]


def screen_agent(screen: dict) -> str | None:
    """"claude" when the screen shows Claude Code (its reply bullet, turn-end line, or mode line), "codex" when it
    shows Codex (its turn-end line, or its › prompt beside a bullet: devenv prints "• Running …" in a shell too)."""
    lines = screen_lines(screen)
    if any(CLAUDE_LINE_STARTS.match(line.strip()) or CLAUDE_MODE_LINE in line for line in lines):
        return "claude"
    if any(CODEX_TURN_END.fullmatch(line.strip()) for line in lines) or (
            any(line.startswith(CODEX_PROMPT) for line in lines) and any(line.startswith(CODEX_REPLY) for line in lines)):
        return "codex"
    return None


def codex_screen_message(lines: list[str]) -> str:
    """Codex's last reply: the last • block that is not an action, up to its turn-end line. When no reply of the last
    ended turn is shown (its bullet scrolled off), the recap Codex printed under that turn's end line."""
    def action(index: int) -> bool:
        following = next((line.strip() for line in lines[index + 1:] if line.strip()), "")
        return bool(CODEX_ACTION.fullmatch(lines[index])) or following.startswith(CODEX_RESULT)

    reply = max((index for index, line in enumerate(lines) if line.startswith(CODEX_REPLY) and not action(index)),
                default=None)
    recap = max((index for index, line in enumerate(lines) if line.strip().startswith(CODEX_RECAP)), default=None)
    if recap is not None:
        ends = [index for index, line in enumerate(lines[:recap]) if CODEX_TURN_END.fullmatch(line.strip())]
        if reply is None or reply < (ends[-2] if len(ends) > 1 else -1):
            said = [lines[recap].strip().removeprefix(CODEX_RECAP),
                    *takewhile(lambda line: line[:1].isspace() and line.strip(), lines[recap + 1:])]
            return unwrapped([line.strip() for line in said])
    if reply is None:
        return ""
    block = [lines[reply][2:]]
    for line in lines[reply + 1:]:
        if (line.strip() and not line[:1].isspace() or CODEX_TURN_END.fullmatch(line.strip())
                or line.strip().startswith(CODEX_RECAP)):
            break
        block.append(line)
    return "\n".join(line[2:] if line.startswith("  ") else line.strip() for line in block).strip()


def screen_message(screen: dict, *, agent: str | None = None) -> str:
    """The agent's last reply on the screen: Claude's last ⏺ block, its indented continuation lines unindented, or
    Codex's (codex_screen_message).

    A reply whose ⏺ line scrolled off the top is read from the indented lines above the last turn-end line.
    """
    lines = screen_lines(screen)
    if (agent or screen_agent(screen)) == "codex":
        return codex_screen_message(lines)
    def tool_call(index: int) -> bool:
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        return bool(TOOL_CALL.match(lines[index]) or lines[index].rstrip().endswith(COLLAPSED_CALLS)
                    or following.startswith(TOOL_RESULT))

    starts = [index for index, line in enumerate(lines) if line.startswith("⏺ ") and not tool_call(index)]
    ends = [index for index, line in enumerate(lines) if line.startswith("✻ ")]
    if starts:
        block = [lines[starts[-1]][2:]]
        for line in lines[starts[-1] + 1:]:
            if line.strip() and not line[:1].isspace():
                break
            block.append(line)
    elif ends:
        above = lines[:ends[-1]]
        top = next((index + 1 for index in range(len(above) - 1, -1, -1)
                    if above[index].strip() and not above[index][:1].isspace()), 0)
        block = above[top:]
    else:
        return ""
    return "\n".join(line[2:] if line.startswith("  ") else line.strip() for line in block).strip()


def claude_progress(screen: dict) -> str | None:
    """The status line of a Claude turn that runs now: the screen's last one, with no turn-end line, reply, or tool
    call below it (a resized terminal can leave an old copy above them)."""
    lines = [line.strip() for line in screen_lines(screen)]
    found = max((index for index, line in enumerate(lines) if CLAUDE_PROGRESS.match(line)), default=None)
    if found is None or any(CLAUDE_TURN_END.match(line) or line.startswith("⏺ ") for line in lines[found + 1:]):
        return None
    return lines[found]


# The start of a transcript's error looked for on the screen.
ERROR_HEAD = 40


def error_notices(lines: list[str], error: str) -> list[int]:
    """The indexes of the stripped screen lines an error notice ends on, worded as a known usage limit or as the
    transcript's error, also where a narrow terminal wrapped it onto the next line (mid-word or at a space)."""
    head = error.strip().splitlines()[0][:ERROR_HEAD] if error.strip() else ""

    def shows(text: str) -> bool:
        return bool(USAGE_LIMIT.search(text) or API_ENDED.match(text.lstrip("⎿■ ")) or head and head in text)

    return [index for index, line in enumerate(lines)
            if shows(line) or index and not shows(lines[index - 1])
            and any(shows(lines[index - 1] + glue + line) for glue in ("", " "))]


def turn_after_error(screen: dict, error: str = "") -> str | None:
    """The turn-end or running status line of a Claude turn that ran after the screen's last error notice, or
    after one that scrolled off: a reply or tool call (⏺) below the notice, then that line. The notice's own turn
    ends with a turn-end line too ("✻ Baked for 1s · done 9:02"), but with no ⏺ between. `error` is the
    transcript's error, found on the screen even when its wording is new."""
    lines = [line.strip() for line in screen_lines(screen)]
    notice = max(error_notices(lines, error), default=-1)
    replied = next((index for index in range(notice + 1, len(lines)) if lines[index].startswith("⏺ ")), None)
    if replied is None:
        return None
    return next((line for line in reversed(lines[replied + 1:])
                 if CLAUDE_TURN_END.match(line) or CLAUDE_PROGRESS.match(line)), None)


def agent_screens(terms: list[dict], screens: Screens, environment: str | None, agent: str) -> list[dict]:
    """The rendered screens of the tabs Orca names `agent` in, or whose screen shows it when Orca names none."""
    found = []
    for term in terms:
        screen = rendered(screens.get((environment, term.get("handle"))))
        if screen and (term.get("agentIdentity") or screen_agent(screen)) == agent:
            found.append(screen)
    return found


class ResumedTurn(NamedTuple):
    kind: str
    line: str
    text: str


def resumed_after_error(row: dict, claude_screens: list[dict]) -> ResumedTurn | None:
    """Evidence that the connection error or usage limit that ended the row's Claude transcript is past: a
    Claude tab shows a later turn, and none shows the notice as its last word. A lane resumed in another account
    writes a transcript elsewhere, so the one read here still ends at the error. A connection error needs
    a later reply/tool call and a running status line; a limit also accepts a completed later turn."""
    stop = stop_reason(row)
    if not (stop and stop["kind"] in ("usage_limit", "connection") and stop["agent"] == "claude"):
        return None
    later = [(line, screen) for screen in claude_screens if (line := turn_after_error(screen, stop["error"]))
             and (stop["kind"] == "usage_limit" or CLAUDE_PROGRESS.match(line))]
    stopped = any(error_notices([line.strip() for line in screen_lines(screen)], stop["error"])
                  and not turn_after_error(screen, stop["error"]) for screen in claude_screens)
    if not later or stopped:
        return None
    line, screen = later[0]
    return ResumedTurn(stop["kind"], line, screen_message(screen))


def turn_clock(line: str, now: float) -> str | None:
    """The UTC time of a turn-end line's local "• HH:MM", on the last day it is not later than `now`, at the minute's
    last second: a Codex that merged its PR replies within the same minute, and its reply has to stay after it."""
    found = TURN_CLOCK.search(line)
    if not found:
        return None
    local = datetime.fromtimestamp(now).astimezone()
    try:
        at = local.replace(hour=int(found[1]), minute=int(found[2]), second=0, microsecond=0)
    except ValueError:
        return None
    if at > local:
        at -= timedelta(days=1)
    return (at + timedelta(seconds=59)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def codex_tab_message(terms: list[dict], screens: Screens) -> tuple[str, str | None]:
    """(the newest reply the worktree's local Codex tabs show under a turn-end line ("Worked for … • HH:MM"), the
    time that line gives or None), or ("", None). The line's clock is read on the last day it is not later than the
    tab's last output, so a tab idle for days does not look new; a tab with no time counts as the oldest."""
    shown = []
    for term in terms:
        screen = rendered(screens.get((None, term.get("handle"))))
        if not screen or screen_agent(screen) != "codex" or term.get("agentIdentity") not in (None, "codex"):
            continue
        lines = [line.strip() for line in screen_lines(screen)]
        ends = [index for index, line in enumerate(lines) if CODEX_TURN_END.fullmatch(line)]
        if not ends or any(CODEX_WORKING.match(line) for line in lines[ends[-1] + 1:]):
            continue  # no turn ended, or a later one runs
        last_output = term.get("lastOutputAt")
        if reply := codex_screen_message(screen_lines(screen)):
            shown.append((reply, turn_clock(lines[ends[-1]], last_output / 1000) if last_output is not None else None))
    return max(shown, key=lambda found: found[1] or "", default=("", None))


def done_cards(agents: list[dict], terms: list[dict], screens: Screens | None = None,
               environment: str | None = None) -> list[DoneCard]:
    """One card per agent Orca shows as done, with the live tab that closes it.

    A tab Orca reopened carries a new pane id, so a card whose pane is gone
    takes the worktree's only unclaimed tab of its agent. `handle` is None
    while the tab sleeps, or when several tabs could be its own.
    `last_message` is what Orca kept of the agent's last word and can be empty;
    the row's transcripts hold the rest. `screen_tail` is the end of the tab's
    rendered screen (`screens` maps (environment, handle) to it). `draft` and
    `draft_text` hold Orca's draft, while `composer_empty` reads Claude's input
    line between rules; [] and None when the screen was not read.
    """
    screens = screens or {}
    by_pane = {hourly_runtime.pane_key(t): t.get("handle") for t in terms}
    panes = {a.get("paneKey") for a in agents}
    unclaimed = {}
    for t in terms:
        if t.get("handle") and hourly_runtime.pane_key(t) not in panes:
            unclaimed.setdefault(t.get("agentIdentity"), []).append(t["handle"])

    def handle_of(agent: dict) -> str | None:
        own = by_pane.get(agent.get("paneKey"))
        spare = unclaimed.get(agent.get("agentType"), [])
        alone = sum(1 for a in agents if a.get("agentType") == agent.get("agentType")
                    and by_pane.get(a.get("paneKey")) is None) == 1
        return own or (spare[0] if len(spare) == 1 and alone else None)

    def card(agent: dict) -> DoneCard:
        handle = handle_of(agent)
        screen = rendered(screens.get((environment, handle))) if handle else None
        return {
            "agent": agent.get("agentType"), "handle": handle,
            "done_at": iso_time(agent.get("stateStartedAt")), "interrupted": bool(agent.get("interrupted")),
            "prompt": agent.get("prompt") or "", "last_message": agent.get("lastAssistantMessage") or "",
            "screen_tail": screen_tail(screen) if screen else [], "draft": has_draft(screen) if screen else None,
            "draft_text": draft_excerpt(screen) if screen else None,
            "composer_empty": composer_empty(screen) if screen else None,
        }

    return [card(a) for a in agents if a.get("state") == "done"]


# A hook Claude Code could not run because its file is gone: "PreToolUse:Bash hook error" with the shell's
# message on the next line, or "Stop hook error: Failed with ...: /bin/sh: PATH: No such file or directory".
HOOK_MISSING = re.compile(r"hook error\b[^\n]*?(?:\n[^\n]*?)??\s(/[^\s:]+): No such file or directory",
                          re.IGNORECASE)
# This Mac's name, as triage-notify records it.
LOCAL_HOST = socket.gethostname()


def hook_errors(terms: list[dict], screens: Screens, environment: str | None) -> list[dict]:
    """Each missing hook file a tab's output or rendered screen shows, once per tab: {host, path, tab}."""
    host = environment or LOCAL_HOST
    shown = ((term.get("handle"), text) for term in terms
             for text in (term.get("preview") or "",
                          "\n".join(screen_lines(rendered(screens.get((environment, term.get("handle")))) or {}))))
    found = {(path, handle): {"host": host, "path": path, "tab": handle}
             for handle, text in shown for path in HOOK_MISSING.findall(text)}
    return list(found.values())


def orca_said(worktree: dict) -> bool:
    """Whether Orca keeps a last message of one of the worktree's agents."""
    return any(agent.get("lastAssistantMessage") for agent in worktree.get("agents") or [])


def with_screen_agents(terms: list[dict], screens: Screens, environment: str | None,
                       approval_tabs: Collection[str]) -> tuple[list[dict], str]:
    """(terms, message): the tabs, each without an agent given the one its screen shows, and the last
    Claude or Codex reply read on them. Another Mac's terminal list names a tab's agent on some reads only."""
    found, message = [], ""
    for term in terms:
        screen = rendered(screens.get((environment, term.get("handle"))))
        if screen and (agent := "codex" if term.get("handle") in approval_tabs else screen_agent(screen)):
            found.append(term if term.get("agentIdentity") else {**term, "agentIdentity": agent})
            message = screen_message(screen, agent=agent) or message
        else:
            found.append(term)
    return found, message


class WorktreeProcess(TypedDict):
    pid: int
    parent: int
    command: str
    elapsed: float | None  # seconds it has run
    cpu: float | None  # its CPU seconds
    still_since: float | None  # epoch since which its CPU time has not moved, from earlier scans
    closed: list[str]  # TCP peers whose side of a connection closed


PROCESS_CPU = "~/.cache/orca-session-triage/process-cpu.json"
STOPPED_AFTER = 3600  # seconds background work runs with its CPU time still before it counts as stopped
# CPU seconds such work may add in that time: under one tick of ps's 0.01 seconds. A poller that runs gh every
# five minutes adds ticks every hour; ps on macOS does not count a process's exited children.
CPU_STILL = 0.005
START_SLACK = 120  # seconds two reads of one process's start time may differ: ps's elapsed time and the scan's clock
CLOSED_TCP = ("CLOSED", "CLOSE_WAIT")
SHELLS = ("zsh", "bash", "sh", "fish")
AGENT_CLIS = ("claude", "codex", "codex.js")


def agent_cli(command: str) -> bool:
    return any(Path(token).name in AGENT_CLIS for token in command.split()[:2])


def waits(command: str) -> bool:
    """A timed wait (`sleep`), or a shell running a command line or script (`zsh -c …`, `bash -lc …`,
    `bash wait.sh`): how agents run their background jobs and how wait loops are written."""
    tokens = command.split()
    if not tokens:
        return False
    if Path(tokens[0]).name == "sleep":
        return True
    return Path(tokens[0]).name in SHELLS and len(tokens) > 1 and (not tokens[1].startswith("-") or any(
        token.startswith("-") and not token.startswith("--") and "c" in token for token in tokens[1:3]))


def job_root(pid: int, table: dict[int, tuple[int, str]]) -> str:
    """The command that started the process's job: its ancestor just below launchd, an agent CLI, or a tab's
    own shell. An MCP server's job is itself, and a leftover dev server's or plugin's is not a shell."""
    seen = set()
    while pid in table and pid not in seen:
        seen.add(pid)
        parent = table[pid][0]
        above = table[parent][1] if parent in table else ""
        if parent <= 1 or not above or agent_cli(above) or rm_check.LOGIN_SHELL.fullmatch(above.strip()):
            break
        pid = parent
    return table[pid][1] if pid in table else ""


def clock_seconds(value: str) -> float | None:
    """Seconds from ps's [[days-]hours:]minutes:seconds."""
    days, _, clock = value.rpartition("-")
    try:
        seconds = 0.0
        for part in clock.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds + int(days or 0) * 86400
    except ValueError:
        return None


def worktree_processes(paths: list[str], cpu_file: Path, now: float) -> dict[str, list[WorktreeProcess]]:
    """The processes of wait loops, timed waits, and agents' background jobs in each local worktree: by
    rm_check.py's rule (current directory or command inside it), those whose job a `sleep` or a shell running a
    command line or script started (see waits and job_root), and the children they started anywhere.

    One process table and directory inventory serves every worktree. `still_since` comes from `cpu_file`, which
    keeps each process's CPU time and when it last moved; an entry is dropped once its process is gone.
    """
    table = rm_check.process_table()
    cwds = rm_check.process_cwds(table) if table else None
    if not paths or not table or cwds is None:
        return {}
    readers = rm_check.output_readers()
    found = {}
    for path in paths:
        # The shell still running a tab's setup is no work.
        found[path] = [item for item in rm_check.processes_using(path, table, cwds, readers)
                       if not any(wait in item["command"] for wait in rm_check.SETUP_WAITS)
                       and waits(job_root(item["pid"], table))]
    children: dict[int, list[int]] = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    for items in found.values():
        # A job's children belong to it wherever they run (`(cd /tmp && sleep 600)`).
        listed = {item["pid"] for item in items}
        stack = list(listed)
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in listed:
                    listed.add(child)
                    stack.append(child)
                    items.append({"pid": child, "command": table[child][1][:160]})
    pids = sorted({item["pid"] for items in found.values() for item in items})
    clocks: dict[int, tuple[float | None, float | None]] = {}
    closed: dict[int, list[str]] = {}
    if pids:
        listed = ",".join(map(str, pids))

        def output(command: list[str]) -> str:
            # A failed socket read must not take the CPU times with it.
            try:
                return subprocess.run(command, capture_output=True, text=True, timeout=ORCA_TIMEOUT).stdout
            except (OSError, subprocess.SubprocessError):
                return ""
        ps = output(["ps", "-o", "pid=,etime=,time=", "-p", listed])
        tcp = output(["lsof", "-n", "-P", "-a", "-p", listed, "-iTCP", "-FpnT"])
        for line in ps.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0].isdigit():
                clocks[int(fields[0])] = (clock_seconds(fields[1]), clock_seconds(fields[2]))
        pid, peer = None, ""
        for line in tcp.splitlines():
            if line.startswith("p"):
                pid = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith("n"):
                peer = line[1:]
            elif pid is not None and line.removeprefix("TST=") in CLOSED_TCP:
                closed.setdefault(pid, []).append(peer)
    try:
        kept = json.loads(cpu_file.read_text())
    except (OSError, ValueError):
        kept = {}
    kept = kept if isinstance(kept, dict) else {}
    # A process of a worktree this scan left out (--only) keeps its history while it lives.
    seen: dict[str, dict] = {key: value for key, value in kept.items()
                             if key.isdigit() and int(key) in table and int(key) not in pids}
    still: dict[int, float | None] = {}
    for pid in pids:
        elapsed, cpu = clocks.get(pid, (None, None))
        if elapsed is None or cpu is None:
            still[pid] = None
            continue
        start, before = now - elapsed, kept.get(str(pid))
        # A pid the system reused started at another time.
        if not (isinstance(before, dict) and isinstance(before.get("start"), (int, float))
                and isinstance(before.get("cpu"), (int, float)) and abs(before["start"] - start) <= START_SLACK):
            before = None
        if before is not None and cpu - before["cpu"] <= CPU_STILL:
            seen[str(pid)] = before
        else:
            # Seen first, a process that has added no CPU time since it started has been still all along.
            seen[str(pid)] = {"start": start, "cpu": cpu,
                              "since": start if before is None and cpu <= CPU_STILL else now}
        still[pid] = seen[str(pid)].get("since")
    try:
        write_json_atomically(cpu_file, seen)
    except OSError:
        pass
    return {path: [{"pid": item["pid"], "parent": table[item["pid"]][0], "command": item["command"],
                    "elapsed": clocks.get(item["pid"], (None,))[0],
                    "cpu": clocks.get(item["pid"], (None, None))[1], "still_since": still.get(item["pid"]),
                    "closed": closed.get(item["pid"], [])} for item in items]
            for path, items in found.items()}


def stopped(process: WorktreeProcess, now: float) -> bool:
    """Background work that stopped: its connection closed, or its CPU time has not moved for an hour. A `sleep`
    is a timed wait and never counts."""
    if Path(process["command"].split()[0]).name == "sleep":
        return False
    since = process["still_since"]
    return bool(process["closed"]) or (since is not None and now - since >= STOPPED_AFTER)


def process_line(process: WorktreeProcess) -> str:
    def span(seconds: float | None) -> str:
        return "?" if seconds is None else f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"
    closed = f", TCP closed: {', '.join(process['closed'])}" if process["closed"] else ""
    return (f"PID {process['pid']} {process['command'][:80]} (running {span(process['elapsed'])}, "
            f"CPU {process['cpu'] if process['cpu'] is not None else '?'}s{closed})")


def with_processes(row: dict, now: float) -> tuple[str, list[str]]:
    """The class and reasons once the worktree's processes are read: a wait still running keeps a stalled or
    finished row working, and background work whose processes at the ends all stopped makes its working row
    stalled. A turn an API failure ended and a lead's unread worker reports stay stalled: the agent does not
    come back for the wait, and the lead is woken for its mail."""
    processes = row.get("processes") or []
    if not processes or row.get("stop") or row.get("unread_mail"):
        return row["class"], row["reasons"]
    lines = [process_line(process) for process in processes[:3]]
    # A shell running a job only waits for its children: the processes at the ends do the work.
    parents = {process["parent"] for process in processes}
    halted = all(stopped(process, now) for process in processes if process["pid"] not in parents)
    if row["class"] in ("stalled", "finished") and not halted:
        return "working", ["a background job runs in the worktree: " + line for line in lines]
    if row["class"] == "working" and row["reasons"] == [BACKGROUND_REASON]:
        if halted:
            return "stalled", ["裏の作業が止まっている: " + "; ".join(lines)]
        return "working", [BACKGROUND_REASON + ": " + "; ".join(lines)]
    return row["class"], row["reasons"]


def one_per_path(worktrees: list[dict], terminals: list[dict]) -> tuple[list[dict], list[dict]]:
    """One worktree per checkout. Orca lists a path once per repo it belongs to, which happens when a linked worktree
    is also registered as a repo: the entry Orca created (else the one with a comment, else the first listed) keeps
    the path, takes the others' agents and tabs, and names their repos in `_other_repos`. It is the main worktree
    only when every entry is, so the linked entry's row is kept."""
    groups: dict[tuple[str | None, str], list[dict]] = {}
    for w in worktrees:
        groups.setdefault((w.get("_environment"), w["path"]), []).append(w)
    kept, moved = [], {}
    for group in groups.values():
        first = max(group, key=lambda w: (w.get("createdAt") is not None, bool(w.get("comment"))))
        others = [w for w in group if w is not first]
        if others:
            moved.update({(w.get("_environment"), w.get("worktreeId")): first.get("worktreeId") for w in others})
            first = {**first, "agents": [agent for w in [first, *others] for agent in w.get("agents") or []],
                     "isMainWorktree": all(w.get("isMainWorktree") for w in group),
                     "_other_repos": [w.get("repo") for w in others]}
        kept.append(first)
    return kept, [{**term, "worktreeId": moved[place]} if (place := (term.get("_environment"), term.get("worktreeId")))
                  in moved else term for term in terminals]


def build_inventory(worktrees: list[dict], terminals: list[dict], claude_root: str | Path,
                    codex_by_cwd: dict[str, list[dict]], decision_marker: str, now: float | None = None,
                    shell_idle_minutes: float = 30, judge: Callable[[str], dict | None] = lambda message: None,
                    mail: dict | None = None, screens: Screens | None = None,
                    live_transcripts: dict[str, list[Path]] | None = None,
                    processes: dict[str, list[WorktreeProcess]] | None = None,
                    live_sessions: dict[str, list[dict]] | None = None) -> list[dict]:
    """Join Orca worktrees with their terminals and transcripts and classify each one.

    `judge(message)` returns Jev's verdict on the newest final message, a dict of
    `asks`, `status`, and `confidence` (see jev_judge), or None when it failed.
    `mail` maps a lead's terminal handle to its unread worker mail (see
    unread_worker_mail). `screens` maps (environment, handle) to a tab's
    rendered screen (see read_screens): done cards show its draft and composer,
    permission prompts and question selectors wait, lost background waits are named, remote shells
    without output metadata use their last line, and another
    Mac's worktree whose agents Orca keeps no message of takes the agent and
    last reply its tabs show. `live_transcripts` maps a local worktree to the transcripts of Claude sessions
    running under it elsewhere (see running_claude_transcripts). `processes` maps a local worktree to the
    processes working in it (see worktree_processes and with_processes).
    A repository's main checkout is read only while it hosts an agent (`main`).
    """
    screens = screens or {}
    now = time.time() if now is None else now
    worktrees, terminals = one_per_path(worktrees, terminals)
    by_worktree = {}
    for term in terminals:
        # An orphaned shell with no agent, title, screen, or output time cannot
        # be read through its stale handle. Keep agent tabs, tabs with output,
        # and tabs whose screen was read: another Mac lists live agent tabs so.
        if (term.get("orphaned") and not term.get("agentIdentity")
                and not term.get("title") and not term.get("preview")
                and term.get("lastOutputAt") is None
                and not rendered(screens.get((term.get("_environment"), term.get("handle"))))):
            continue
        # Orphaned and disconnected is the stale record of a closed tab; a tab
        # Orca detached from the window can stay connected and still run.
        if term.get("connected") or not term.get("orphaned"):
            by_worktree.setdefault((term.get("_environment"), term.get("worktreeId")), []).append(term)
    rows = []
    for w in worktrees:
        remote = bool(w.get("_environment"))
        main = bool(w.get("isMainWorktree"))
        if main and not w.get("agents"):
            continue
        terms = by_worktree.get((w.get("_environment"), w.get("worktreeId")), [])
        ordered_agents = sorted(w.get("agents") or [], key=lambda agent: (
            agent.get("state") in ("working", "waiting"), agent.get("updatedAt") or 0))
        agents_by_pane = {agent["paneKey"]: agent for agent in ordered_agents
                          if agent.get("paneKey") and agent.get("agentType")}
        identified = []
        for term in terms:
            agent = agents_by_pane.get(hourly_runtime.pane_key(term))
            if agent and (agent.get("state") in ("working", "waiting") or not term.get("agentIdentity")):
                term = {**term, "agentIdentity": agent["agentType"]}
            identified.append(term)
        terms = identified
        prompts = list(waiting_prompts(terms, screens, w.get("_environment")))
        waiting = prompts[0] if prompts else None
        codex_approvals = [prompt for prompt, _ in prompts if prompt["question"] in (CODEX_PERMISSION, CODEX_APPROVAL)]
        approval_tabs = {prompt["tab"] for prompt in codex_approvals}
        screen_said = ""
        if remote and not orca_said(w):
            terms, screen_said = with_screen_agents(terms, screens, w.get("_environment"), approval_tabs)
        scanned_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
        approval_captures = {}
        for tab in approval_tabs:
            observed_at = screens[(w.get("_environment"), tab)].get("_observed_at")
            approval_captures[tab] = observed_at if isinstance(observed_at, str) and observed_at else scanned_at
        approval_observed = min(approval_captures.values(), default=scanned_at)
        pane_handles = {key: term["handle"] for term in terms
                        if term.get("handle") and (key := hourly_runtime.pane_key(term))}
        codex_activity = {term["handle"]: last_codex_activity(screen_lines(frame)) for term in terms
                          if term.get("handle") and (frame := rendered(screens.get((w.get("_environment"), term["handle"]))))
                          and (term.get("agentIdentity") == "codex"
                               or (approval_tabs and not term.get("agentIdentity") and screen_agent(frame) == "codex"))}
        screen_error: tuple[str | None, str | None] = (None, None)
        for term in terms:
            handle = term.get("handle")
            if handle in approval_tabs or (term.get("agentIdentity") != "codex" and handle not in codex_activity):
                continue
            activity = (codex_activity[handle] if handle in codex_activity
                        else last_codex_activity(str(term.get("preview") or "").splitlines()))
            if error := api_failure(activity):
                screen_error = error, handle
                break

        def pane_resumed(agent: dict, tab: str) -> bool:
            activity = codex_activity.get(tab)
            if not activity or api_failure(activity):
                return False
            frame = screens.get((w.get("_environment"), tab)) or {}
            observed = parse_time(frame.get("_observed_at")) or datetime.fromtimestamp(now, timezone.utc)
            updated = parse_time(iso_time(agent.get("updatedAt")))
            return updated is None or updated <= observed
        spoken = [agent for agent in w.get("agents") or [] if agent.get("lastAssistantMessage")
                  and (not approval_tabs or agents_by_pane.get(agent.get("paneKey"), agent) is agent)]
        pane_errors = []
        if approval_tabs:
            pane_errors = [
                (error, agent.get("agentType"), tab, iso_time(agent.get("updatedAt")))
                for agent in sorted(spoken, key=lambda item: item.get("updatedAt") or 0, reverse=True)
                if (tab := pane_handles.get(agent.get("paneKey")))
                and API_ENDED.match(error := " ".join(agent["lastAssistantMessage"].split()))
                and not pane_resumed(agent, tab)]
        row = {
            "path": w["path"], "main": main, "repo": w.get("repo"), "branch": w.get("branch"),
            "host_id": w.get("hostId"), "environment": w.get("_environment"),
            "comment": w.get("comment"), "linked_pr": w.get("linkedPR"),
            "linked_issue": w.get("linkedIssue"), "linked_items": w.get("linkedItems") or [],
            "status": w.get("workspaceStatus"),
            "unread": bool(w.get("unread")),
            "agents": [{"type": a.get("agentType"), "state": a.get("state"), "turn": main_state(a)}
                       for a in w.get("agents") or []],
            "terminals": [{"handle": t.get("handle"),
                           "agent": "codex" if t.get("handle") in approval_tabs else t.get("agentIdentity"),
                           "title": t.get("title")} for t in terms],
            "codex_screen_error": screen_error[0],
            "codex_screen_error_tab": screen_error[1],
            **({"pane_errors": pane_errors} if pane_errors else {}),
            "done_cards": done_cards(w.get("agents") or [], terms, screens, w.get("_environment")),
            "prompt_waiting": waiting[0] if waiting else None,
            **({"codex_approvals": codex_approvals,
                "approval_observed_at": approval_observed, "approval_capture_by_tab": approval_captures}
               if codex_approvals else {}),
            "claude": None if remote else read_claude(claude_root, w["path"], (live_transcripts or {}).get(w["path"], [])),
            "codex": [] if remote else codex_by_cwd.get(w["path"], []),
        }
        if not remote and (sessions := (live_sessions or {}).get(w["path"])):
            row["claude_sessions"] = sessions
        if w.get("_other_repos"):
            row["other_repos"] = w["_other_repos"]
        row["queued_input"] = (queued_input(Path(row["claude"]["transcript"]),
                                            datetime.fromtimestamp(now, timezone.utc)) if row["claude"] else [])
        if waiting and waiting[1] and row["claude"] and (asked_at := row["claude"].get(waiting[1])):
            waiting[0]["asked_at"] = asked_at
        if remote:
            if spoken:
                def approval_error(agent: dict) -> bool:
                    return (agent.get("agentType") == "codex" and pane_handles.get(agent.get("paneKey")) in approval_tabs
                            and bool(API_ENDED.match(" ".join(agent["lastAssistantMessage"].split()))))

                current = [agent for agent in spoken if not approval_error(agent)
                           and not (API_ENDED.match(" ".join(agent["lastAssistantMessage"].split()))
                                    and (tab := pane_handles.get(agent.get("paneKey"))) and pane_resumed(agent, tab))]
                newest = max(current or spoken, key=lambda a: a.get("updatedAt") or 0)
                row["orca_message"] = {"text": newest["lastAssistantMessage"],
                                       "at": iso_time(newest.get("updatedAt")),
                                       **({"tab": tab} if (tab := pane_handles.get(newest.get("paneKey"))) else {}),
                                       **({"agent": newest["agentType"]} if newest.get("agentType") else {}),
                                       **({"resumed": True} if tab and pane_resumed(newest, tab) else {})}
            elif screen_said:
                row["orca_message"] = {"text": screen_said, "at": None, "from": "screen"}
        elif not row["codex"] and (codex_said := codex_tab_message(terms, screens))[0]:
            # A Codex this Mac keeps no recent rollout of finished a turn its tab still shows.
            row["orca_message"] = {"text": codex_said[0], "at": codex_said[1], "from": "screen"}
        claude_screens = agent_screens(terms, screens, w.get("_environment"), "claude")
        row["screen_progress"] = next((line for screen in claude_screens if (line := claude_progress(screen))), None)
        resumed = resumed_after_error(row, claude_screens)
        if resumed:
            # The screen's reply stands for the transcript's old error; it carries no time.
            row["claude"] = {**(row["claude"] or {}), "api_error": None, "api_error_kind": None,
                             **({"final_text": resumed.text, "final_at": None} if resumed.text else {})}
        row["unread_mail"] = unread_since_turn(row, mail or {}, now)
        latest = None if turn_runs(row) or cut_off(row) or row["screen_progress"] else latest_message(row)
        row["jev"] = {"source": latest[0], **(judge(latest[1]) or NO_VERDICT)} if latest else None
        row["stop"] = stop_reason(row)
        row["class"], row["reasons"] = classify(row, decision_marker)
        if resumed:
            error_kind = "usage limit" if resumed.kind == "usage_limit" else "connection error"
            row["reasons"] = [*row["reasons"], f"the transcript's {error_kind} is past: the tab's screen shows a "
                                               f"later turn ({resumed.line})"]
        row["processes"] = [] if remote else (processes or {}).get(w["path"], [])
        row["class"], row["reasons"] = with_processes(row, now)
        if decision_marker and (marked := re.search(re.escape(decision_marker) + r"[:：]?\s*(.+)",
                                                     row["comment"] or "", re.DOTALL)):
            row["marker_question"] = marked.group(1).strip()
        row["hook_errors"] = hook_errors(terms, screens, w.get("_environment"))
        # Quoted as the screen shows it: a tab that has not run since keeps an old error on its screen.
        row["reasons"] = [*row["reasons"], *dict.fromkeys(
            f"hook error on {error['host']}: {error['path']}: No such file or directory" for error in row["hook_errors"])]
        row["reasons"].extend(lost_background_waits(w.get("agents") or [], terms, screens, w.get("_environment")))
        row["lost_schedules"] = ((row.get("claude") or {}).get("one_shot_schedules") or []
                                 if not (live_transcripts or {}).get(w["path"])
                                 and not any(term.get("agentIdentity") == "claude" for term in terms) else [])
        if row["lost_schedules"]:
            row["reasons"].extend(f"予約が消えた: {job['cron']} {job['prompt'][:100]}" for job in row["lost_schedules"])
            if row["class"] in ("finished", "unstarted", "unsure"):
                row["class"] = "stalled"
        row["closable"] = closable_terminals(agents_done(row), terms, now, shell_idle_minutes, screens)
        row["meaningful_tabs"] = [t.get("handle") or NO_HANDLE + (t.get("title") or "")
                                  for t in terms if t.get("handle") not in row["closable"]]
        rows.append(row)
    return rows


def with_codex_permission_reasons(rows: list[dict]) -> None:
    waiting = {prompt["tab"]: row for row in rows if not row.get("environment")
               for prompt in row.get("codex_approvals") or []}
    if not waiting:
        return
    codex_processes = {}
    for pid, (_, command) in (rm_check.process_table() or {}).items():
        args = command.split()
        binary = next((index for index, token in enumerate(args[:2])
                       if Path(token).name in ("codex", "codex.js")
                       or Path(token).name.startswith(("codex-aarch64-", "codex-x86_64-"))), None)
        if binary is None:
            continue
        options = args[binary + 1:]
        if "--" in options:
            options = options[:options.index("--")]
        if not options or options[0] not in ("app-server", "exec", "e", "exec-server", "review"):
            codex_processes[pid] = options
    for pid, handle in rm_check.process_handles(sorted(codex_processes)).items():
        if handle in waiting and CODEX_BYPASS_FLAGS.isdisjoint(codex_processes[pid]):
            waiting[handle]["reasons"].append(f"Codex process {pid} shows no approvals bypass flag in its process command")


def github_slug(remote_url: str | None) -> str | None:
    """owner/repo of a github.com remote; None for any other host."""
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote_url or "")
    return match.group(1) if match else None


PR_URL = re.compile(r"https://github\.com/([^/\s)]+/[^/\s)]+)/pull/(\d+)")


def remote_git_hint(row: dict) -> dict:
    """PR lookup hints from Orca, without claiming the remote checkout was inspected."""
    linked = row.get("linked_pr") or {}
    found = PR_URL.search(linked.get("url") or "")
    if not found:
        urls = set(PR_URL.findall((row.get("orca_message") or {}).get("text") or ""))
        if len(urls) == 1:
            slug, number = next(iter(urls))
            found = PR_URL.search(f"https://github.com/{slug}/pull/{number}")
    return {"branch": (row.get("branch") or "").removeprefix("refs/heads/") or None,
            "head": None, "dirty": None, "unpushed": None,
            "github": found.group(1) if found else None,
            "pr_number": int(found.group(2)) if found else linked.get("number")}


def git_state(path: str) -> dict | None:
    """Branch, uncommitted paths, commits not pushed to the upstream, and the GitHub repo.

    None outside a git checkout; `unpushed` is None without an upstream.
    """
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=30)

    try:
        status = run("status", "--porcelain")
        if status.returncode != 0:
            return None
        ahead = run("rev-list", "--count", "@{u}..HEAD")
        branch = run("symbolic-ref", "-q", "--short", "HEAD").stdout.strip() or None
        head, _, head_at = run("log", "-1", "--format=%H %cI").stdout.strip().partition(" ")
        origin = run("remote", "get-url", "origin").stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {
        "branch": branch, "head": head or None, "head_at": head_at or None,
        "dirty": len(status.stdout.splitlines()),
        "unpushed": int(ahead.stdout) if ahead.returncode == 0 else None,
        "github": github_slug(origin),
    }


def latest_human_activity(author: str | None, reviews: Sequence[dict] = (), comments: Sequence[dict] = (), line_comments: Sequence[dict] = ()) -> dict | None:
    """Newest non-author human PR activity from the three GitHub comment surfaces."""
    candidates: list[dict] = []
    for kind, entries, timestamp in (("review", reviews, "submitted_at"),
                                     ("comment", comments, "created_at"),
                                     ("line_comment", line_comments, "created_at")):
        for entry in entries:
            user = entry.get("user") or {}
            login = user.get("login")
            at = entry.get(timestamp)
            if (user.get("type") == "User" and login and at
                    and (not author or login.casefold() != author.casefold())):
                candidates.append({"author": login, "kind": kind, "at": at,
                                   **({"state": entry.get("state")} if kind == "review" else {})})
    return max(candidates, key=lambda item: item["at"]) if candidates else None


def summarize_pr(data: dict, reviews: Sequence[dict] = (), comments: Sequence[dict] = (), line_comments: Sequence[dict] = ()) -> dict:
    """Reduce `gh pr view --json` output to what decides a stalled worktree's next step."""
    checks = {}
    for check in data.get("statusCheckRollup") or []:
        key = (check.get("conclusion") or check.get("state") or check.get("status") or "UNKNOWN").upper()
        checks[key] = checks.get(key, 0) + 1
    author = (data.get("author") or {}).get("login")
    human_reviews = [r for r in reviews if (r.get("user") or {}).get("type") == "User"
                     and (r.get("user") or {}).get("login") and r.get("submitted_at")
                     and (not author or r["user"]["login"].casefold() != author.casefold())]
    latest = max(human_reviews, key=lambda r: r["submitted_at"], default=None)
    activity = latest_human_activity(author, reviews, comments, line_comments)
    commits = data.get("commits") or []
    last_commit_at = commits[-1].get("committedDate") if commits else None
    # GitHub exposes commit time here, not the time a commit was pushed. Keep
    # the source visible so a reader does not mistake this for an exact push time.
    activity_at = parse_time(activity["at"]) if activity else None
    commit_at = parse_time(last_commit_at)
    reply_after_commit = bool(activity_at and commit_at and activity_at > commit_at)
    return {
        "number": data.get("number"), "url": data.get("url"), "state": data.get("state"),
        "head": data.get("headRefName"), "head_oid": data.get("headRefOid"),
        "review": data.get("reviewDecision") or None,
        "requested": [r.get("login") or r.get("name") for r in data.get("reviewRequests") or []],
        # GitHub reports a deleted account's review with a null author, shown as "ghost".
        "reviews": [((r.get("author") or {}).get("login") or "ghost") + ":" + r.get("state", "")
                    for r in data.get("latestReviews") or []],
        "latest_human_review": {"author": latest["user"]["login"], "state": latest["state"],
                                "submitted_at": latest["submitted_at"]} if latest else None,
        "latest_human_activity": activity, "last_commit_at": last_commit_at,
        "reply_after_last_commit": reply_after_commit,
        "merge_state": data.get("mergeStateStatus"), "merged_at": data.get("mergedAt"), "checks": checks,
    }


class Item(NamedTuple):
    """An issue or PR a saved verdict waits on, read only for its state."""
    number: int


PrRef = int | str | Item  # a PR number, the head branch to find the PR by, or an issue or PR to read the state of
PrLookup = Callable[[str, list[PrRef]], dict[PrRef, dict | None]]

GH_TIMEOUT = 15
# What gh prints when its request never reached GitHub: a failed DNS lookup ("error connecting to
# api.github.com"), or a refused, reset, unreachable, or timed-out connection ("dial tcp …").
GH_UNREACHABLE = re.compile(r"error connecting to|dial tcp|proxyconnect|i/o timeout|TLS handshake timeout"
                            r"|connection (?:refused|reset)|network is unreachable|no such host")
GRAPHQL_CHUNK = 25  # 25 PRs by number in one query cost 1 point, as one `gh pr view` did (measured 2026-10-28)
# gh reads 30 PRs of a branch; the newest 5 keep it cheap (25 branches in one query cost 5 points).
BRANCH_CANDIDATES = 5
PR_ACTIVITY_DIR = "~/.cache/orca-session-triage/pr-activity"
SLUG = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
NOT_IN_BRANCH = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")  # git refuses these in a branch name
# The `gh pr view --json` fields summarize_pr reads, with only the last commit
# (gh pages through every commit) and the check states. gh also pages past 100
# checks; a PR with more counts only its first 100 here.
PR_FACTS = """
fragment PrFacts on PullRequest {
  number url state updatedAt isCrossRepository headRefName headRefOid
  reviewDecision mergeStateStatus mergedAt
  author { __typename login }
  reviewRequests(first: 100) { nodes { requestedReviewer {
    __typename ... on User { login } ... on Bot { login } ... on Team { name } } } }
  latestReviews(first: 100) { nodes { author { login } state } }
  commits(last: 1) { nodes { commit { committedDate statusCheckRollup { contexts(first: 100) {
    nodes { __typename ... on CheckRun { status conclusion } ... on StatusContext { state } } } } } } }
}"""
ACTIVITY = (("reviews", "pulls/{}/reviews"), ("comments", "issues/{}/comments"),
            ("line_comments", "pulls/{}/comments"))


def is_pr_ref(ref: object) -> bool:
    """A positive PR number, or a name git accepts for a branch (quoted with json.dumps in the query)."""
    if isinstance(ref, bool):
        return False
    if isinstance(ref, Item):
        return ref.number > 0
    if isinstance(ref, int):
        return ref > 0
    return isinstance(ref, str) and bool(ref) and not NOT_IN_BRANCH.search(ref)


def pr_query(owner: str, name: str, refs: list[PrRef]) -> tuple[str, dict[str, PrRef]]:
    """One GraphQL query for these PRs of a repository, and the ref each alias answers."""
    aliases: dict[str, PrRef] = {}
    fields = ["defaultBranchRef { name }"]
    for index, ref in enumerate(refs):
        if isinstance(ref, Item):
            alias = f"item_{ref.number}"
            fields.append(f"{alias}: issueOrPullRequest(number: {ref.number}) {{ __typename "
                          f"... on Issue {{ state updatedAt closedByPullRequestsReferences(first: {GRAPHQL_CHUNK}) "
                          "{ nodes { number } } } ... on PullRequest { state updatedAt headRefOid } }")
        elif isinstance(ref, int):
            alias = f"pr_{ref}"
            fields.append(f"{alias}: pullRequest(number: {ref}) {{ ...PrFacts }}")
        else:
            alias = f"branch_{index}"
            fields.append(f"{alias}: pullRequests(headRefName: {json.dumps(ref, ensure_ascii=False)}, "
                          f"first: {BRANCH_CANDIDATES}, orderBy: {{field: CREATED_AT, direction: DESC}}) "
                          "{ nodes { ...PrFacts } }")
        aliases[alias] = ref
    # GitHub refuses a fragment the query does not use: a query of states alone leaves it out.
    facts = PR_FACTS if any(not isinstance(ref, Item) for ref in refs) else ""
    return (f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ "
            + " ".join(fields) + " } }" + facts), aliases


def graphql_repository(slug: str, query: str, reach: Reach | None = None) -> dict | None:
    """The `repository` object a query returned, or None when gh gave no data.

    A PR that does not exist comes back as a null alias with a NOT_FOUND error,
    and gh exits 1 while the other aliases still hold data, so stdout is read
    whatever the exit code. The query goes into `reach`, with gh's error when
    its last try timed out or gh says it could not connect (GH_UNREACHABLE).
    """
    for attempt in range(3):
        # Only the last try says whether GitHub was reached: an answer after a timeout reached it.
        out, timed_out = None, False
        try:
            out = subprocess.run(["gh", "api", "graphql", "-f", "query=" + query],
                                 capture_output=True, text=True, timeout=GH_TIMEOUT)
            repository = (json.loads(out.stdout).get("data") or {}).get("repository")
            failure = out.stderr.strip() or out.stdout.strip()
            if isinstance(repository, dict):
                if reach is not None:
                    reach.record("GitHub", None)
                return repository
            # A structured API rejection is not an empty or broken transport response.
            break
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as error:
            timed_out = isinstance(error, subprocess.TimeoutExpired)
            failure = ((out.stderr.strip() or out.stdout.strip()) if out else "") or repr(error)
            if out and re.search(r"GraphQL:|HTTP 4\d\d|Bad credentials|gh auth login", failure):
                break
        if attempt < 2:
            time.sleep(attempt + 1)
    print(f"GitHub query for {slug} failed, so its rows have no PR facts: {failure}", file=sys.stderr)
    if reach is not None:
        reach.record("GitHub", failure if timed_out or GH_UNREACHABLE.search(failure) else None)
    return None


def pr_for_branch(nodes: list, branch: str, default_branch: str | None) -> dict | None:
    """The PR `gh pr view <branch>` shows from the newest PRs with that head branch.

    gh puts an open PR first and keeps the rest newest first; it skips a PR from
    a fork's branch of the same name, and a closed or merged PR whose head is
    the repository's default branch.
    """
    ordered = sorted((node for node in nodes if isinstance(node, dict)), key=lambda node: node.get("state") != "OPEN")
    return next((node for node in ordered if not node.get("isCrossRepository")
                 and (node.get("state") == "OPEN" or branch != default_branch)), None)


def view_fields(node: dict) -> dict:
    """The `gh pr view --json` fields summarize_pr reads, from a PullRequest node of PR_FACTS."""
    author = node.get("author")
    last = ((node.get("commits") or {}).get("nodes") or [None])[-1] or {}
    commit = last.get("commit") or {}
    requested = [(entry or {}).get("requestedReviewer") for entry in (node.get("reviewRequests") or {}).get("nodes") or []]
    return {
        **{key: node.get(key) for key in ("number", "url", "state", "headRefName", "headRefOid",
                                          "reviewDecision", "mergeStateStatus", "mergedAt")},
        # gh prints an author that is not a User (a bot) as app/<login>.
        "author": None if not author else {
            "login": author.get("login") if author.get("__typename") == "User" else f"app/{author.get('login')}"},
        "reviewRequests": [r for r in requested if r and r.get("__typename") in ("User", "Bot", "Team")],
        "latestReviews": (node.get("latestReviews") or {}).get("nodes") or [],
        "statusCheckRollup": (((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []),
        "commits": [commit] if commit else [],
    }


def activity_entry(entry: dict) -> dict:
    """What summarize_pr reads of a review or comment: its author, state, and time."""
    user = entry.get("user")
    return {"user": {"login": user.get("login"), "type": user.get("type")} if isinstance(user, dict) else None,
            **{key: entry[key] for key in ("state", "submitted_at", "created_at") if key in entry}}


def rest_list(endpoint: str) -> list[dict] | None:
    """Every entry of a paginated REST list, or None when the read failed."""
    try:
        result = subprocess.run(["gh", "api", "--paginate", "--slurp", endpoint],
                                capture_output=True, text=True, timeout=GH_TIMEOUT)
        if result.returncode != 0:
            return None
        return [activity_entry(entry) for page in json.loads(result.stdout) for entry in page
                if isinstance(entry, dict)]
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return None


def pr_activity(slug: str, node: dict, cache_dir: Path) -> list[list[dict]]:
    """A PR's reviews, conversation comments, and line comments, kept per PR in `cache_dir`.

    GitHub moves a PR's updatedAt on every review, comment, and push, so the copy
    kept from a run that saw the same updatedAt still holds and the three REST
    reads are skipped. A read that failed counts as empty, as before, and is not kept.
    """
    number, updated = node["number"], node.get("updatedAt")
    path = cache_dir / slug / f"{number}.json"
    kept = read_json_file(path)
    if (updated and isinstance(kept, dict) and kept.get("updated_at") == updated
            and all(isinstance(kept.get(kind), list) for kind, _ in ACTIVITY)):
        return [kept[kind] for kind, _ in ACTIVITY]
    read = {kind: rest_list(f"repos/{slug}/{endpoint.format(number)}") for kind, endpoint in ACTIVITY}
    if updated and all(entries is not None for entries in read.values()):
        try:
            write_json_atomically(path, {"updated_at": updated, **read})
        except OSError as error:
            print(f"could not keep the activity of {slug}#{number}: {error}", file=sys.stderr)
    return [read[kind] or [] for kind, _ in ACTIVITY]


def gh_prs(slug: str, refs: list[PrRef], cache_dir: Path = Path(PR_ACTIVITY_DIR).expanduser(),
           reach: Reach | None = None) -> dict[PrRef, dict | None]:
    """Summaries of a repository's PRs by number or head branch; None where gh finds none or fails.

    One GraphQL query reads up to GRAPHQL_CHUNK PRs. Each PR's reviews and
    comments take three REST reads, skipped while its updatedAt is the one kept
    in `cache_dir` (see pr_activity).
    """
    found: dict[PrRef, dict | None] = dict.fromkeys(refs)
    owner, _, name = slug.partition("/")
    if not SLUG.fullmatch(slug) or name in (".", ".."):
        return found
    wanted = [ref for ref in found if is_pr_ref(ref)]
    for start in range(0, len(wanted), GRAPHQL_CHUNK):
        query, aliases = pr_query(owner, name, wanted[start:start + GRAPHQL_CHUNK])
        repository = graphql_repository(slug, query, reach) or {}
        default_branch = (repository.get("defaultBranchRef") or {}).get("name")
        for alias, ref in aliases.items():
            answer = repository.get(alias) or {}
            if isinstance(ref, Item):
                found[ref] = {"state": answer["state"]} if isinstance(answer.get("state"), str) else None
                continue
            node = (pr_for_branch(answer.get("nodes") or [], ref, default_branch)
                    if isinstance(ref, str) else answer)
            found[ref] = node if isinstance(node, dict) and isinstance(node.get("number"), int) else None
    nodes = {node["number"]: node for ref, node in found.items() if node and not isinstance(ref, Item)}
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(nodes)))) as pool:
        activity = dict(zip(nodes, pool.map(lambda node: pr_activity(slug, node, cache_dir), nodes.values())))
    return {ref: (node if isinstance(ref, Item) else summarize_pr(view_fields(node), *activity[node["number"]]))
            if node else None for ref, node in found.items()}


def parse_time(value: str | None) -> datetime | None:
    """An aware datetime; a time without a zone is UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


WORKER_MAIL = ("worker_done", "question", "escalation")
MAIL_GRACE_MINUTES = 5  # Orca types its own notice into an idle lead first


def unread_worker_mail(messages: list[dict], runs: list[dict]) -> dict[str, list[dict]]:
    """Unread worker reports, questions, and escalations, keyed by the lead terminal they wait on."""
    leads = {run.get("id"): run.get("coordinator_handle") for run in runs}
    mail = {}
    for m in messages:
        run = m.get("run_id")
        if m.get("read") or m.get("type") not in WORKER_MAIL or m.get("to_handle") != f"run:{run}":
            continue
        if lead := leads.get(run):
            mail.setdefault(lead, []).append(
                {"type": m["type"], "created_at": m.get("created_at"), "subject": m.get("subject")})
    return mail


def unread_since_turn(row: dict, mail: dict[str, list[dict]], now: float, grace_minutes: float = MAIL_GRACE_MINUTES) -> list[dict]:
    """The lead's unread worker mail that arrived after its last turn began, oldest first.

    Orca types a notice into an idle lead, yet has left reports unread for hours.
    Mail from before that turn is left out: a lead can read it with `--peek`,
    which leaves it unread, and a notice or wake line that started the turn
    already told it about that mail.
    """
    began = parse_time((row["claude"] or {}).get("last_user_at"))
    if began is None:
        return []
    arrived = []
    for m in (m for t in row["terminals"] for m in mail.get(t["handle"], [])):
        at = parse_time(m.get("created_at"))
        if at is not None and began < at and at.timestamp() <= now - grace_minutes * 60:
            arrived.append(m)
    return sorted(arrived, key=lambda m: m["created_at"])


def mail_reason(unread: list[dict]) -> str:
    noun = "message" if len(unread) == 1 else "messages"
    return f"{len(unread)} orchestration {noun} unread since its last turn began (oldest {unread[0]['created_at']})"


def final_message_at(row: dict) -> datetime | None:
    """When the final message Jev read was written, or None."""
    source = (row.get("jev") or {}).get("source")
    if source == "codex":
        return parse_time((row.get("codex") or [{}])[-1].get("last_assistant_at"))
    if source == "orca":
        return parse_time((row.get("orca_message") or {}).get("at"))
    return parse_time((row.get("claude") or {}).get("final_at"))


def merged_after_final_message(row: dict, pr: dict) -> bool:
    """The worktree's own PR merged after a final message that waited on someone else.

    That message was written before the merge it was waiting for, often a
    review or the user's own merge on GitHub, so the wait it reported is over.
    """
    if not (row["class"] == "stalled" and (row.get("jev") or {}).get("status") == "blocked_on_others"
            and pr.get("state") == "MERGED" and pr.get("head_matches")):
        return False
    said_at, merged_at = final_message_at(row), parse_time(pr.get("merged_at"))
    return said_at is not None and merged_at is not None and merged_at > said_at


def merged_with_nothing_left(row: dict, pr: dict) -> bool:
    """A checkout no agent ran in whose own PR merged at HEAD.

    Orca's PR link is often missing or still says open after the merge, so the
    PR found by the lookup decides. A row an agent spoke in is not finished
    here even when Jev read it as done: Jev's read is fast and shallow, so that
    row waits in the review queue for a careful one.
    """
    return (row["class"] == "stalled" and row.get("reasons") == [NO_SESSION_REASON]
            and pr.get("state") == "MERGED" and bool(pr.get("head_matches")))


def open_pr_with_no_local_work(row: dict, pr: dict, git: dict) -> bool:
    """No current agent or local work remains beyond the published PR head.

    `git.unpushed` is relative to the branch's upstream, which can be the PR's
    base. An exact PR head match proves this HEAD was pushed to the PR.
    """
    return (not row.get("agents")
            and not any(term.get("agent") for term in row.get("terminals", []))
            and pr.get("state") == "OPEN" and pr.get("head_matches") is True
            and git.get("dirty") == 0)


IN_USE_SECONDS = 30 * 60


def checkout_in_use(git: dict, pr: dict) -> str | None:
    """The local time of a HEAD commit or review-pr log written in the last thirty minutes.

    Either one means another lane is working in the checkout though no agent shows in its tabs.
    """
    times = [committed.timestamp()] if (committed := parse_time(git.get("head_at"))) else []
    if SLUG.fullmatch(git.get("github") or "") and pr.get("number"):
        log = (Path(os.environ.get("TRIAGE_REVIEW_DIR") or REVIEW_DIR).expanduser()
               / f"{git['github'].replace('/', '-')}-{pr['number']}.log")
        try:
            times.append(log.stat().st_mtime)
        except OSError:
            pass
    latest = max(times, default=None)
    if latest is None or time.time() - latest > IN_USE_SECONDS:
        return None
    return datetime.fromtimestamp(latest).astimezone().strftime("%m/%d %H:%M")


WAITED_ITEM = re.compile(r"github\.com/(?P<url>[\w.-]+/[\w.-]+)/(?:pull|issues)/(?P<url_number>\d+)"
                         r"|(?<![\w.-])(?:(?P<repo>[A-Za-z0-9][\w.-]*(?:/[\w.-]+)?)(?: PR)? ?)?#(?P<number>\d+)")
NOT_REPOS = ("pr", "prs", "issue", "and", "or", "then", "review")  # words before `#N` that name no repository


def waited_items(waits_on: str, slug: str, names: set[str],
                 prose: frozenset[str] = frozenset()) -> list[tuple[str | None, int]]:
    """The issues and PRs a verdict's waits_on names. `repo#N` is a repository of the row's organization:
    among `names`, the repositories known there, the one named so or the only one whose name ends in `-repo`
    (masters → taihei-coresys-masters), else None. A bare `#N` (or `PR #N`) belongs to the repository named
    before it, else the row's own; so does `word #N` for a lowercase word in `prose` (merge #10)."""
    org = slug.partition("/")[0]
    items: list[tuple[str | None, int]] = []
    last: str | None = slug
    for found in WAITED_ITEM.finditer(waits_on):
        repo = found["url"] or found["repo"]
        if repo and "/" in repo:
            last = repo
        elif repo and repo.lower() not in NOT_REPOS and repo.lower() not in prose:
            ends = [name for name in names if name.lower().endswith("-" + repo.lower())]
            last = (f"{org}/{repo}" if repo in names else f"{org}/{ends[0]}" if len(ends) == 1 else None)
        items.append((last, int(found["url_number"] or found["number"] or 0)))
    return list(dict.fromkeys(items))


# A proposal of the adoption ledger, named by its full ID or by the first 8 characters people write.
PROPOSAL_REF = re.compile(r"(?<![0-9A-Za-z-])[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?"
                          r"(?![0-9A-Za-z-])")
DECIDED_STATUSES = ("adopted", "rejected", "held")


class LedgerProposal(NamedTuple):
    """An adoption-ledger proposal as a lane's wait sees it."""
    decided: bool  # the board adopted, rejected or held it, or it was applied
    holders: frozenset[str] = frozenset()  # the lanes it was handed to while adopted and not yet applied

    def settles(self, worktree: str) -> bool:
        """Whether a wait of `worktree` that names this proposal is over. A lane holding the proposal names it as
        the work it ships (`--applied <id>`), not as something it waits on, until it is applied."""
        return self.decided and worktree not in self.holders


def ledger_proposals() -> dict[str, LedgerProposal] | None:
    """Each adoption-ledger proposal by ID; None, with the reason on stderr, when the ledger cannot be read.
    A lane holds a proposal when a handed_off execution under its last adoption names it as the actor, as
    adoption-triage's `ledger.py owner` reads it."""
    try:
        items = retro_ledger.LedgerClient().list()
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
        print(f"adoption ledger unreadable: {error}", file=sys.stderr)
        return None

    def holders(item: dict) -> frozenset[str]:
        if item.get("implemented_at") or item.get("status") != "adopted":
            return frozenset()
        adoption = next((decision.get("id") for decision in reversed(item.get("decisions") or [])
                         if decision.get("decision") == "adopt"), None)
        return frozenset(str(run["actor"]) for run in item.get("executions") or []
                         if adoption and run.get("decision_id") == adoption
                         and run.get("outcome") == "handed_off" and run.get("actor"))

    return {item["proposal_id"]: LedgerProposal(
                item.get("status") in DECIDED_STATUSES or bool(item.get("implemented_at")), holders(item))
            for item in items if isinstance(item.get("proposal_id"), str)}


def named_proposals(text: str, proposals: Mapping[str, LedgerProposal]) -> list[str]:
    """The ledger proposals `text` names: by full ID, or by an 8-character prefix only one proposal has.
    Other hex words (commit SHAs, event IDs) name none."""
    named = []
    for token in PROPOSAL_REF.findall(text):
        matches = [proposal for proposal in proposals if proposal.startswith(token)]
        if len(matches) == 1:
            named.append(matches[0])
    return list(dict.fromkeys(named))


def add_next_step_facts(rows: list[dict], git_lookup: Callable[[str], dict | None] = git_state,
                        pr_lookup: PrLookup = gh_prs, verdicts: dict | None = None,
                        proposals: Callable[[], Mapping[str, LedgerProposal] | None] | None = None) -> list[dict]:
    """Add git and PR facts to rows needing review or completed rows with no current agent.

    `pr_lookup(slug, refs)` reads every PR one repository's rows need at once
    and returns a summary or None per ref; repositories are read in parallel.
    The issues and PRs each row's saved verdict waits on (`verdicts`) are read in the
    same queries; once every one is closed, the row's `waits_closed` names them and
    the verdict no longer holds (current_verdict), so the row is read again.
    Once `proposals()` (the adoption ledger, read only when a saved verdict's waits_on or next_step holds a
    proposal-like ID) has every proposal they name decided, `waits_decided` names them; read_key includes it, so
    the verdict is read again once, and a re-read that names the same proposals holds.
    `pr.head_matches` is False when the PR found (a branch name can be reused)
    does not point at the checkout's HEAD. A stalled row whose final message
    waited on someone else finishes once its PR merged after that message.
    """
    fact_rows = [row for row in rows if row["class"] in READ_CLASSES or
                 (row["class"] == "finished" and not row.get("agents") and
                  not any(term.get("agent") for term in row.get("terminals", [])))]
    fact_row_ids = {id(row) for row in fact_rows}
    git_by_row = {id(row): (remote_git_hint(row) if row.get("environment") else git_lookup(row["path"]))
                   for row in fact_rows}
    # Orca names a repository as GitHub does; the checkouts read here say which organization holds it.
    known = {git["github"] for git in git_by_row.values() if git and git.get("github")}
    repo_names = {row["repo"] for row in rows if row.get("repo")}
    waits_by_row: dict[int, list[tuple[str | None, int]]] = {}
    for row in fact_rows:
        git, verdict = git_by_row[id(row)], (verdicts or {}).get(row_identity(row))
        if git and git.get("github") and isinstance(verdict, dict) and isinstance(verdict.get("waits_on"), str):
            org = git["github"].partition("/")[0]
            names = repo_names | {slug.partition("/")[2] for slug in known if slug.partition("/")[0] == org}
            waits_by_row[id(row)] = waited_items(verdict["waits_on"], git["github"], names)
    # The re-read replaces waits_on and next_step, so a decided proposal drops a verdict only once.
    wait_texts = {id(row): "\n".join(str(verdict.get(key) or "") for key in ("waits_on", "next_step"))
                  for row in fact_rows if isinstance(verdict := (verdicts or {}).get(row_identity(row)), dict)}
    ledger = (proposals() if proposals and any(PROPOSAL_REF.search(text) for text in wait_texts.values())
              else None) or {}
    refs_by_repo: dict[str, list[PrRef]] = {}
    for row in rows:
        if id(row) not in fact_row_ids:
            continue
        git = git_by_row.get(id(row))
        ref = (row["linked_pr"] or {}).get("number") or (git or {}).get("pr_number") or (git or {}).get("branch")
        if git and git.get("github") and ref:
            refs = refs_by_repo.setdefault(git["github"], [])
            if ref not in refs:
                refs.append(ref)
        for slug, number in waits_by_row.get(id(row), []):
            if slug is None:
                continue
            refs = refs_by_repo.setdefault(slug, [])
            if Item(number) not in refs:
                refs.append(Item(number))

    def lookup(slug: str) -> dict[PrRef, dict | None]:
        try:
            return pr_lookup(slug, refs_by_repo[slug])
        except (OSError, subprocess.TimeoutExpired):
            return {}

    prs: dict[tuple[str, PrRef], dict | None] = {}
    if refs_by_repo:
        with ThreadPoolExecutor(max_workers=min(8, len(refs_by_repo))) as pool:
            for slug, found in zip(refs_by_repo, pool.map(lookup, refs_by_repo)):
                prs.update({(slug, ref): pr for ref, pr in found.items()})

    result = []
    for row in rows:
        if id(row) not in fact_row_ids:
            result.append(row)
            continue
        git, waits, wait_text = git_by_row[id(row)], waits_by_row.get(id(row), []), wait_texts.get(id(row), "")
        ref = (row["linked_pr"] or {}).get("number") or (git or {}).get("pr_number") or (git or {}).get("branch")
        pr = prs.get((git["github"], ref)) if git and git.get("github") and ref else None
        if pr is not None and git is not None:
            pr = {**pr, "head_matches": pr.get("head_oid") == git.get("head") if git.get("head") else None}
        row: dict[str, Any] = {**row, "git": git, "pr": pr}
        open_pr_cleanup = pr is not None and git is not None and open_pr_with_no_local_work(row, pr, git)
        if open_pr_cleanup and git is not None and pr is not None and (used_at := checkout_in_use(git, pr)):
            row = {**row, "reasons": [*row.get("reasons", []), f"別の担当が使用中（{used_at}）"]}
            open_pr_cleanup = False
        if open_pr_cleanup and (note := keep_note(row.get("comment"))):
            row = {**row, "reasons": [*row.get("reasons", []), "Orca comment asks to keep it: " + note]}
            open_pr_cleanup = False
        if open_pr_cleanup:
            row = {**row, "open_pr_cleanup": True}
        if pr is not None and merged_after_final_message(row, pr):
            row = {**row, "class": "finished", "reasons": ["PR merged after the final message"]}
        elif pr is not None and merged_with_nothing_left(row, pr):
            row = {**row, "class": "finished", "reasons": ["PR merged"]}
        elif open_pr_cleanup and row.get("reasons") == [NO_SESSION_REASON]:
            row = {**row, "class": "finished", "reasons": ["no local work beyond open PR head"]}
        elif pr is not None and pr.get("state") == "OPEN" and pr.get("reply_after_last_commit"):
            reasons = row.get("reasons")
            row = {**row, "waits_on": "self",
                   "reasons": [*(reasons if isinstance(reasons, list) else []),
                               "human PR response after the latest commit"]}
        # Read before the inbox reports join the row, the verdict's freshness is not known here.
        if waits and all(slug and (prs.get((slug, Item(number))) or {}).get("state") in ("CLOSED", "MERGED")
                         for slug, number in waits):
            closed = [f"{slug}#{number}" for slug, number in waits]
            row = {**row, "waits_closed": closed,
                   "reasons": [*row.get("reasons", []), "everything the saved verdict waits on closed: "
                               + ", ".join(closed)]}
        named = named_proposals(wait_text, ledger)
        if named and all(ledger[proposal].settles(Path(row["path"]).name) for proposal in named):
            decided = [proposal[:8] for proposal in named]
            row = {**row, "waits_decided": decided,
                   "reasons": [*row.get("reasons", []), "待っていた案が台帳で決まった: " + ", ".join(decided)]}
        result.append(row)
    return result


QUEUE_FIELDS = ("path", "repo", "environment", "class", "reasons", "waits_on", "comment",
                "jev", "pr", "git", "claude", "orca_message", "prompt_waiting")
INBOX_FILE = "~/.cache/orca-briefs/triage-inbox.jsonl"
INBOX_KINDS = ("done", "ask", "info")
VERDICTS_FILE = "~/.cache/orca-session-triage/verdicts.json"
VERDICT_CLASSES = ("waiting", "blocked", "in_progress", "done_unmerged", "finished")
READ_CLASSES = ("unsure", "stalled", "waiting")


class AnsweredAsk(TypedDict):
    at: str
    note: str


class InboxReport(TypedDict):
    at: str
    host: str | None
    text: str
    id: NotRequired[str]
    answered: NotRequired[AnsweredAsk]


InboxReports = dict[tuple[str, str], dict[str, InboxReport]]


def row_final_at(row: dict) -> str | None:
    """When the newest final message a reader would class was written (Claude, Codex, Orca, or a newer done card
    that `final_message` takes), or None."""
    codex = row.get("codex") or []
    said = [at for at in ((row.get("claude") or {}).get("final_at"),
                          codex[-1].get("last_assistant_at") if codex else None,
                          (row.get("orca_message") or {}).get("at"),
                          (newer_card(row, transcript_final(row)) or {}).get("at")) if at]
    return max(said, key=lambda at: parse_time(at) or datetime.min.replace(tzinfo=timezone.utc), default=None)


def ask_answers(path: Path, *, include_sources: bool = False) -> dict[str, dict]:
    """Answers for the scan, with the current explicit wording and the latest relay time."""
    try:
        records = list(inbox_read.answer_entries(path))
        current = inbox_read.answer_records(path)
        explicit = {key: item for key, item in current.items() if item.get("how") in inbox_read.HOURLY_ANSWERS}
    except (OSError, UnicodeDecodeError):
        return {}
    timed = sorted(((at, record) for record in records if isinstance(record.get("id"), str)
                    and (at := parse_time(record.get("ts"))) is not None), key=lambda item: item[0])
    found = {}
    for _, record in timed:
        key = record["id"]
        source = explicit.get(key, record)
        found[key] = {"at": record["ts"], "note": source.get("note") or "",
                      **({"ts": source["ts"], "how": source.get("how")} if include_sources else {})}
    return {key: found[key] for key in current if key in found} if include_sources else found


def ask_answer(item: dict, answers: dict[str, dict]) -> AnsweredAsk | None:
    """The answer that settled an ask: its own, or the latest of its questions' once every one has one
    (inbox_read.open_asks's rule)."""
    ask_id = item["id"]
    numbers = range(1, len(inbox_read.questions_of(item)) + 1)
    whole = answers.get(ask_id)
    settled = inbox_read.answer_parts(ask_id, numbers, answers)
    if not settled or numbers and len(settled) != len(numbers):
        return None
    newest = max([*settled, *([whole] if whole else [])], key=lambda answer: parse_time(answer["at"]) or EPOCH)
    note = "\n".join(dict.fromkeys(answer["note"] for answer in settled)) if whole else newest["note"]
    return {"note": note, "at": newest["at"]}


def inbox_reports(path: Path, answers_file: Path | None = None) -> InboxReports:
    """The newest `done`, `ask`, and `info` each worktree sent to the triage inbox, by (repo, worktree name).

    An entry from before reports named their repository is keyed by ("", name).
    Each ask keeps its id and, once covered, its latest answer's time and note.
    """
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return {}
    answers = ask_answers(answers_file or inbox_read.answers_path().expanduser(), include_sources=True)
    reports: InboxReports = {}
    for raw in lines:
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if (not isinstance(item, dict) or item.get("kind") not in INBOX_KINDS or not item.get("worktree")
                or (at := parse_time(item.get("ts"))) is None):
            continue
        held = reports.setdefault((item.get("repo") or "", item["worktree"]), {})
        older = held.get(item["kind"])
        if older is None or at >= (parse_time(older["at"]) or EPOCH):
            held[item["kind"]] = {"at": item["ts"], "host": item.get("host"),
                                  "text": (item.get("text") or "")[-JEV_MAX_CHARS:]}
            if item["kind"] == "ask" and item.get("id"):
                held["ask"]["id"] = item["id"]
                if answered := ask_answer(item, answers):
                    held["ask"]["answered"] = answered
    return reports


def last_instruction_at(row: dict) -> datetime | None:
    """When the lane's Claude or Codex last took a prompt, or None when this Mac has neither transcript."""
    codex = row.get("codex") or []
    given = [parse_time(at) for at in ((row.get("claude") or {}).get("last_user_at"),
                                       codex[-1].get("last_user_at") if codex else None) if at]
    return max((at for at in given if at), default=None)


def row_repos(row: Mapping) -> tuple[str, ...]:
    """The repos a report may name this row under: every repo Orca lists its checkout in, and none."""
    return tuple(dict.fromkeys((row.get("repo") or "", *row.get("other_repos", ()), "")))


def with_inbox(rows: list[dict], reports: InboxReports) -> list[dict]:
    """Rows carry `inbox`: the newest done and ask their worktree reported under any repo Orca lists it in, and the
    newest info it sent after the lane's last instruction, when it reported any. A lane can deliver the work by info
    and end with other text."""
    def newest(row: dict) -> dict:
        name = Path(row["path"]).name
        found = [reports.get((repo, name)) or {} for repo in row_repos(row)]
        inbox = {kind: max(held, key=lambda report: parse_time(report["at"]) or EPOCH)
                 for kind in INBOX_KINDS if (held := [f[kind] for f in found if kind in f])}
        if "info" in inbox and not ((began := last_instruction_at(row))
                                    and (parse_time(inbox["info"]["at"]) or EPOCH) > began):
            del inbox["info"]
        return inbox
    return [{**row, "inbox": inbox} if (inbox := newest(row)) else row for row in rows]


# How long before a turn's end its lane sends the done or info about it; measured on transcript rows, half of
# such reports come within a minute of the final message.
REPORT_BEFORE_TURN_END = timedelta(minutes=5)


def with_reported_times(rows: list[dict], entries: Sequence[dict]) -> list[dict]:
    """Rows whose Codex reply was read from a tab's screen take the time of the done or info the lane sent just
    before that turn ended, when it was sent on an earlier day than the screen's reading.

    The screen gives only the turn end's clock, read on the day of the tab's last output (codex_tab_message), so a
    tab that redraws days later moves the reply to that day. The newest report of the worktree sent within
    REPORT_BEFORE_TURN_END of that clock time sets the day: on an earlier day, the reply's `at` becomes the report's
    `ts`, `screen_at` keeps the screen's reading, and `at_from` names the report; on the reading's own day, it stays.
    """
    sent: dict[str, list[tuple[datetime, dict]]] = {}
    for item in entries:
        if item.get("kind") in ("done", "info") and item.get("worktree") and (at := parse_time(item.get("ts"))):
            sent.setdefault(item["worktree"], []).append((at, item))

    def reported(row: dict) -> dict:
        message = row.get("orca_message") or {}
        shown = parse_time(message.get("at"))
        if message.get("from") != "screen" or shown is None:
            return row
        reports = sorted(((at, item) for at, item in sent.get(Path(row["path"]).name, [])
                          if at <= shown and (item.get("repo") or "") in row_repos(row)),
                         key=lambda pair: pair[0], reverse=True)
        for at, item in reports:
            if (shown - at) % timedelta(days=1) <= REPORT_BEFORE_TURN_END:
                if shown - at < timedelta(days=1):
                    return row
                return {**row, "orca_message": {**message, "at": item["ts"], "screen_at": message["at"],
                                                "at_from": {"kind": item["kind"], "id": item.get("id")}}}
        return row
    return [reported(row) for row in rows]


def newest_report(row: dict) -> dict | None:
    """Which came last of the final message and the inbox reports; the Orca comment has no time."""
    said = [("final_message", row_final_at(row)),
            *((f"inbox_{kind}", report["at"]) for kind, report in (row.get("inbox") or {}).items())]
    timed = [(source, at, parsed) for source, at in said if at and (parsed := parse_time(at)) is not None]
    if not timed:
        return None
    source, at, _ = max(timed, key=lambda pair: pair[2])
    return {"source": source, "at": at}


def read_key(row: dict) -> str | None:
    """What a careful read saw: final message, PR, human activity, tree, inbox reports, permission prompt,
    Orca comment, and the decided proposals its verdict waits on (`waits_decided`). None without a message.

    A timeless message uses a text digest. An inbox answer changes the key,
    and a verdict holds only while the key is unchanged.
    """
    final_at = row_final_at(row)
    if final_at is None:
        text = final_message(row)["text"]
        if not text.strip():
            return None
        final_at = "text:" + hashlib.sha256(text.encode()).hexdigest()[:16]
    pr, git = row.get("pr") or {}, row.get("git") or {}
    inbox = [[report["at"], report["answered"]["at"]] if report.get("answered") else report["at"]
             for _, report in sorted((row.get("inbox") or {}).items())]
    return json.dumps([final_at, pr.get("state"), pr.get("number"), pr.get("head_oid"),
                       pr.get("latest_human_review"), pr.get("latest_human_activity"), git.get("dirty"),
                       *([inbox] if inbox else []), *([row["prompt_waiting"]] if row.get("prompt_waiting") else []),
                       *([row["codex_approvals"]] if row.get("codex_approvals") else []),
                       *([{"comment": row["comment"]}] if row.get("comment") else []),
                       *([{"decided": row["waits_decided"]}] if row.get("waits_decided") else [])])


def row_identity(row: Mapping) -> str:
    """Keep equal paths on different Orca servers distinct in the verdict store."""
    return f"{row['environment']}::{row['path']}" if row.get("environment") else row["path"]


def pending_handoffs(row: dict, state_file: Path) -> list[dict]:
    """Undelivered promises addressed to this row, read again before closing its tabs."""
    document = read_document(state_file)
    if document is None:
        return []
    data = json.loads(document)
    names = handoff_names(row)
    return [item for item in data.get("handoffs", {}).values()
            if item.get("delivered_at") is None and item.get("to") in names]


def handoff_names(row: Mapping) -> set[str]:
    """Every name a handoff's from or to may give this row: its identity, path, repo/name under each repo Orca lists
    it in, or a local row's name."""
    names = {row_identity(row), row_name(row), row["path"],
             *(f"{repo}/{Path(row['path']).name}" for repo in (row.get("repo"), *row.get("other_repos", ())))}
    if not row.get("environment"):
        names.add(Path(row["path"]).name)
    return names


def with_automation_workspaces(rows: list[dict], orca: str) -> list[dict]:
    """Read automation ownership once per Orca environment and retain each referenced workspace."""
    import hourly_runtime
    inventories = {}
    errors = {}
    for environment in {row.get("environment") for row in rows}:
        try:
            inventories[environment] = hourly_runtime.existing_workspace_automations(orca, environment)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            inventories[environment] = {}
            errors[environment] = f"自動実行の作業場を確認できない: {error}"
    for row in rows:
        names = inventories[row.get("environment")].get(os.path.realpath(row["path"]), [])
        row["automation_workspaces"] = names
        row["automation_blocker"] = errors.get(row.get("environment")) or "; ".join(
            f"自動実行『{name}』の作業場" for name in names)
    return rows


def promise_due(item: Mapping, now: datetime) -> bool:
    """Whether a promise may go out now: it has no `not_before` date, or that date has come in JST."""
    return not item.get("not_before") or now.astimezone(JST).date() >= date.fromisoformat(item["not_before"])


def promise_task(item: Mapping) -> str:
    return item.get("task") or item.get("condition") or ""


def not_before_label(item: Mapping) -> str:
    """「期限前（11/17）」 for a promise before its `not_before` date."""
    day = date.fromisoformat(item["not_before"])
    return f"期限前（{day.month}/{day.day}）"


def with_cleanup_constraints(rows: list[dict], state_file: Path, decisions_file: Path,
                             now: datetime | None = None) -> list[dict]:
    """Keep the tabs and checkout of rows that a block, an undelivered promise, or a removal answer holds.

    A promise before its `not_before` date holds the row without making it stalled. Each reason is added once, so
    reading a saved scan again does not repeat it. `removal_decision` is the newest answer to the row's own removal
    keys and decides `allows_removal`. If the newest answers tie, a non-approving answer takes precedence.
    The scan retains tied candidates so it can reevaluate their conditions when read without the decision log.
    A newer answer to one of the `::` sub-keys is shown as `newest`. An answer recorded with removal terms carries
    `after`, `discard`, and `compose_down`; a condition's PR is read from GitHub now, its state (or null when it
    cannot be read) in `after_state`.
    """
    import lead_decisions
    decisions = lead_decisions.latest(lead_decisions.records(decisions_file))
    now = now or datetime.now(timezone.utc)
    removal_candidates: list[tuple[dict, list[dict]]] = []

    def moment(item: lead_decisions.Decision) -> datetime:
        return lead_decisions.moment(item.decided_at) or EPOCH

    for row in rows:
        if blocker := row.get("automation_blocker"):
            row.update(closable=[], open_pr_cleanup=False)
        promises = pending_handoffs(row, state_file)
        due = [item for item in promises if promise_due(item, now)]
        if promises:
            row.update(pending_handoffs=promises, closable=[], open_pr_cleanup=False)
        if due:
            if row.get("class") == "finished":
                row["class"] = "stalled"
            if (row.get("verdict") or {}).get("class") == "finished":
                row.pop("verdict", None)
        early = [item for item in promises if item not in due]
        added = [*([blocker] if blocker else []),
                 *(["未配信の約束の送り先: " + "; ".join(promise_task(item) for item in due)] if due else []),
                 *(f"{not_before_label(item)}の約束の送り先: {promise_task(item)}" for item in early)]
        row["reasons"] = [*row.get("reasons", []), *dict.fromkeys(reason for reason in added
                                                                  if reason not in row.get("reasons", []))]
        keys = {f"remove:{row.get('repo')}/{Path(row['path']).name}",
                f"remove:{row['path']}", f"remove:{row_identity(row)}"}
        if not row.get("environment"):
            keys.add(f"remove:local::{row['path']}")
        answered = [decisions[key] for key in sorted(keys) if key in decisions]
        if answered:
            latest_at = max(map(moment, answered))
            later = [item for key, item in decisions.items() if any(key.startswith(base + "::") for base in keys)
                     and moment(item) > latest_at]
            newest = max(later, key=moment) if later else None
            removal_candidates.append((row, [
                {"decision": answer.decision, "answer": answer.answer, "decided_at": answer.decided_at,
                 **({"newest": {"decision": newest.decision, "answer": newest.answer,
                                "decided_at": newest.decided_at}} if newest else {}),
                 **({"after": answer.removal.after, "discard": list(answer.removal.discard),
                     "compose_down": answer.removal.compose_down} if answer.removal else {})}
                for answer in answered if moment(answer) == latest_at
            ]))
        elif removal := row.get("removal_decision"):
            removal_candidates.append((row, row.get("removal_candidates") or [removal]))
        names = {row["path"], row_identity(row), row_name(row),
                 f"{row.get('repo')}/{Path(row['path']).name}"}
        if not row.get("environment"):
            names.update((Path(row["path"]).name, f"local::{row['path']}"))
        wake_keys = {f"wake:{name}" for name in names}
        answered = [answer for key, answer in decisions.items()
                    if any(key == prefix or key.startswith(prefix + "::") for prefix in wake_keys)]
        row.pop("wake_decision", None)
        if answered:
            answer = max(answered, key=lambda item: lead_decisions.moment(item.decided_at) or EPOCH)
            row["wake_decision"] = {"answer": answer.answer, "decided_at": answer.decided_at}
    conditions: dict[str, set[int]] = {}
    for pr in (lead_decisions.RemovalTerms(removal.get("after")).pull_request()
               for _, candidates in removal_candidates for removal in candidates):
        if pr:
            conditions.setdefault(pr.repo, set()).add(pr.number)
    states = {(repo, number): state.state for repo, numbers in conditions.items()
              for number, state in (lead_decisions.github_pr_states(repo, sorted(numbers)) or {}).items()}
    for row, candidates in removal_candidates:
        for removal in candidates:
            if pr := lead_decisions.RemovalTerms(removal.get("after")).pull_request():
                removal["after_state"] = states.get((pr.repo, pr.number))
            removal["allows_removal"] = removal_answer_approves(removal)
        row["removal_decision"] = min(candidates, key=lambda removal: removal["allows_removal"])
        if len(candidates) > 1:
            row["removal_candidates"] = candidates
        else:
            row.pop("removal_candidates", None)
    return rows


def removal_approved(row: dict) -> bool:
    return removal_answer_approves(row.get("removal_decision") or {})


def removal_answer_approves(decision: Mapping) -> bool:
    """An explicit removal answer; retaining or deferring in that answer takes precedence. An answer recorded with
    removal terms is decided by them, whatever conditions its words give: one with a PR condition (`after`) once that
    PR merged, and one with only cleanup (`discard`, `compose_down`), which rm-check-batch runs first, at once."""
    if decision.get("after"):
        return decision.get("after_state") == "MERGED"
    if decision.get("discard") or decision.get("compose_down"):
        return True
    text = str(decision.get("answer") or "")
    # The preflight itself proves these conditions. Other future conditions still need a new answer.
    text = re.sub(r"rm_check(?:\.py)?\s*(?:が\s*)?ok\s*なら|検査が通れば", "", text, flags=re.IGNORECASE)
    text = re.sub(r"タブを閉じてから|--archive-scratch\s*で(?:作業記録を|控えに)?移してから", "", text)
    if re.search(r"残[すしさ]|保留|消さ(?:ない|ず)|消しては|削除しない|削除しては|"
                 r"たら|れば|なら|次第|まで|から|後に|後で|消すか|消せない|(?:消す|削除する)な|"
                 r"\b(?:keep|defer|do not remove|don't remove)\b", text, re.IGNORECASE):
        return False
    # English answers are whole commands: a substring cannot establish approval or its timing.
    if re.fullmatch(r"\s*(?:remove it|ready to remove)\s*[.!]?\s*", text, re.IGNORECASE):
        return True
    return bool(re.search(r"(?:消す|消します|消して(?:よい|いい|ください|構わない|かまわない)|"
                          r"削除(?:する|して(?:よい|いい|ください)))(?=$|[\s。、（()）」])", text))


def removal_blocker(row: dict) -> str | None:
    if blocker := row.get("automation_blocker"):
        return blocker
    if row.get("pending_handoffs"):
        return "未配信の約束の送り先"
    if (answer := row.get("removal_decision")) and not removal_approved(row):
        return f"removal decision {answer['decided_at']} {answer['answer']}"
    return None


def removal_facts(row: dict) -> str:
    """The scan values used when explaining why a row was left out."""
    shown = (row.get("verdict") or {}).get("class") or row["class"]
    return (f"class={class_label(shown, row['class'])}, open_pr_cleanup={bool(row.get('open_pr_cleanup'))}, "
            f"removal_decision={json.dumps(row.get('removal_decision'), ensure_ascii=False)}")


def current_verdict(row: dict, verdicts: dict[str, dict]) -> dict | None:
    """The stored verdict for what the row shows now, or None once everything it waits on closed."""
    if row.get("waits_closed"):
        return None
    key = read_key(row)
    verdict = verdicts.get(row_identity(row)) if key is not None else None
    return verdict if isinstance(verdict, dict) and verdict.get("read") == key else None


def review_queue(rows: list[dict], verdicts: dict | None = None, *, force: bool = False) -> list[dict]:
    """Rows a careful reader must class before anything closes.

    Jev reads a final message fast and shallow, so every row it judged from one
    (unsure, stalled, or waiting) is queued, and so is a stalled row whose own PR
    merged although its message did not say it was done. A row keeps the verdict
    orca-row-classify gave it while its message, PR, review, and tree stay as read, so
    only new or changed rows are read again. `force` includes the row explicitly selected by --only.
    """
    verdicts = verdicts or {}

    def needs_reader(row: dict) -> bool:
        if force:
            return True
        merged = (row.get("pr") or {}).get("state") == "MERGED"
        judged = row.get("jev") is not None and row["class"] in READ_CLASSES
        if not (row.get("codex_approvals") or judged or row["class"] == "unsure" or (row["class"] == "stalled" and merged)):
            return False
        return current_verdict(row, verdicts) is None

    return [{**{key: row.get(key) for key in QUEUE_FIELDS},
             **{key: row[key] for key in ("claude_sessions", "automation_workspaces", "automation_blocker",
                                           "codex_approvals") if row.get(key)},
             **({"inbox": row["inbox"], "newest_report": newest_report(row)} if row.get("inbox") else {}),
             **({"open_pr_cleanup": True} if row.get("open_pr_cleanup") else {}),
             **({"identity": row_identity(row)} if row.get("environment") else {}),
             "codex": row["codex"][-1] if row.get("codex") else None,
             "final": final_message(row), "final_at": row_final_at(row), "read": read_key(row)}
            for row in rows if needs_reader(row)]


def read_json_file(path: str | Path) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomically(path: Path, data: object) -> None:
    """Write through a temporary file beside `path`, so a reader never sees half a file.

    Each write gets its own temporary name: two scans running at once must not
    replace each other's half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    os.close(handle)
    temp = Path(name)
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def load_verdicts(store_path: str | Path) -> dict[str, dict]:
    """The verdict store as a dict; a damaged one is moved aside to `<name>.bad`, never overwritten."""
    store = Path(store_path).expanduser()
    if not store.exists():
        return {}
    data = read_json_file(store)
    if isinstance(data, dict):
        return {path: v for path, v in data.items() if isinstance(v, dict)}
    store.replace(store.with_name(store.name + ".bad"))
    return {}


def classed_queue(session: Path) -> dict[str, dict]:
    """{path: verdict} from a session directory whose result answers its own queue, else {}.

    Each verdict carries `queued_at`, when its queue was written, so a later read wins over an earlier one.
    """
    queue, result = read_json_file(session / "review-queue.json"), read_json_file(session / "review-result.json")
    if not (isinstance(queue, dict) and isinstance(result, dict)
            and result.get("queue_generated_at") == queue.get("generated_at")):
        return {}
    read = {e.get("identity") or e["path"]: e.get("read") for e in queue.get("rows") or []
            if isinstance(e, dict) and "path" in e}
    queued_at = str(queue.get("generated_at") or "")
    return {v.get("identity") or v["path"]: {**{k: x for k, x in v.items()
                                                if k not in ("path", "identity", "read", "queued_at")},
                        "read": read[v.get("identity") or v["path"]], "queued_at": queued_at}
            for v in result.get("rows") or []
            if isinstance(v, dict) and (v.get("identity") or v.get("path")) in read
            and v.get("class") in VERDICT_CLASSES and read[v.get("identity") or v["path"]] is not None}


def newer_verdicts(verdicts: dict[str, dict], incoming: dict[str, dict]) -> dict[str, dict]:
    """`verdicts` updated with each incoming verdict whose read is not older than the one it replaces.

    A session directory keeps its last classed queue, so an old session's read is folded in on every
    scan; it must not undo a later read of the same worktree that another session already stored.
    """
    def older(path: str, verdict: dict) -> bool:
        held = verdicts.get(path)
        return held is not None and str(verdict.get("queued_at") or "") < str(held.get("queued_at") or "")
    return {**verdicts, **{path: v for path, v in incoming.items() if not older(path, v)}}


def absorb_verdicts(store_path: str | Path, write: bool = True) -> dict[str, dict]:
    """Fold session and hourly reads/<run> classed queues into the store and return it."""
    store = Path(store_path).expanduser()
    verdicts = load_verdicts(store)
    queues = [*store.parent.glob("*/review-queue.json"), *store.parent.glob("reads/*/review-queue.json")]
    for queue in sorted(queues, key=lambda p: p.stat().st_mtime):
        verdicts = newer_verdicts(verdicts, classed_queue(queue.parent))
    if write:
        write_json_atomically(store, verdicts)
    return verdicts


def with_verdicts(rows: list[dict], verdicts: dict[str, dict]) -> list[dict]:
    """Rows a careful reader classed carry that verdict while what it read still holds."""
    def marked(row: dict) -> dict:
        # A prompt on the tab's screen is the state now, whatever a reader saved before.
        verdict = (current_verdict(row, verdicts) if row["class"] in READ_CLASSES and not row.get("prompt_waiting")
                   else None)
        return {**row, "verdict": verdict} if verdict else row
    return [marked(row) for row in rows]


def read_codex(session_roots: Sequence[str | Path], since: float | None = None) -> dict[str, list[dict]]:
    """Group Codex rollout summaries by the cwd each session ran in.

    Rollouts last modified before `since` (epoch seconds) are skipped.
    """
    by_cwd = {}
    for root in session_roots:
        for path in sorted(Path(root).glob("**/rollout-*.jsonl")):
            if since is not None and path.stat().st_mtime < since:
                continue
            cwd, summary = summarize_rollout(path)
            if cwd:
                by_cwd.setdefault(cwd, []).append(summary)
    return by_cwd


def orca_payload(orca: str, args: list[str], json_file: str | None = None, timeout: float | None = None) -> dict:
    if json_file:
        data = json.loads(Path(json_file).read_text())
    else:
        out = subprocess.run([orca, *args, "--json"], capture_output=True, text=True,
                             check=True, timeout=timeout or ORCA_TIMEOUT).stdout
        data = json.loads(out)
    result = data.get("result", data) if isinstance(data, dict) else data
    if not isinstance(result, dict):
        raise ValueError("Orca response must contain a JSON object")
    return result


def orca_result(orca: str, args: list[str], key: str, json_file: str | None, timeout: float | None = None) -> list[dict]:
    rows = orca_payload(orca, args, json_file, timeout)[key]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Orca {key} must contain an array of JSON objects")
    return rows


def endpoint_diagnostic(environment: str, environments: list[dict] | None) -> str:
    """Describe the saved endpoint and whether this Mac can connect to its TCP port."""
    if environments is None:
        return "saved endpoint and TCP port could not be checked"
    saved = next((row for row in environments
                  if environment in (row.get("id"), row.get("name"))), None)
    if saved is None:
        return f"{environment} is not in orca environment list"
    endpoints = saved.get("endpoints") or []
    endpoint = next((row.get("endpoint") for row in endpoints
                     if row.get("id") == saved.get("preferredEndpointId")), None)
    endpoint = endpoint or next((row.get("endpoint") for row in endpoints if row.get("endpoint")), None)
    if not endpoint:
        return "no saved endpoint; TCP port could not be checked"
    try:
        parsed = urlsplit(endpoint)
        if not parsed.hostname or not parsed.port:
            raise ValueError("missing host or port")
    except ValueError as error:
        return f"saved endpoint {endpoint}; TCP port could not be checked ({error})"
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2):
            return f"saved endpoint {endpoint}; TCP port {parsed.port} reachable from this Mac: yes"
    except OSError as error:
        return f"saved endpoint {endpoint}; TCP port {parsed.port} reachable from this Mac: no ({error})"


def collect_worktrees_and_terminals(orca: str, ps_json: str | None = None, terminals_json: str | None = None,
                                    remote_timeout: float | None = None) -> tuple[list[dict], list[dict]]:
    """Read this Mac and the paired Orca servers named in its scope.

    `remote_timeout` caps each read of a paired server (default ORCA_TIMEOUT), so one that
    does not answer is skipped with its warning sooner.
    """
    local = orca_payload(orca, ["worktree", "ps"], ps_json)
    worktrees = list(local["worktrees"])
    terminals = orca_result(orca, ["terminal", "list"], "terminals", terminals_json)
    if ps_json:
        return worktrees, terminals
    selectors = local.get("hostScope", {}).get("omittedHostSelectors") or []
    if not selectors:
        return worktrees, terminals
    try:
        environments = orca_result(orca, ["environment", "list"], "environments", None)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        environments = None
    for omitted in selectors:
        selector = omitted.get("selector") or ""
        try:
            parts = shlex.split(selector)
        except ValueError:
            parts = []
        if len(parts) != 2 or parts[0] != "--environment":
            host_id = omitted.get("hostId")
            listed_ids = {row.get("id") for row in environments} if environments is not None else None
            missing = ("; ID is not in orca environment list" if listed_ids is not None and
                       str(host_id).removeprefix("runtime:") not in listed_ids else "")
            print(f"cannot scan host ID {host_id}: no Orca environment selector{missing}", file=sys.stderr)
            continue
        environment = parts[1]
        try:
            remote = orca_payload(orca, ["worktree", "ps", "--environment", environment], timeout=remote_timeout)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            detail = endpoint_diagnostic(environment, environments)
            print(f"cannot scan Orca environment {environment}: {error}; {detail}; local rows remain available",
                  file=sys.stderr)
            continue
        worktrees.extend({**worktree, "_environment": environment} for worktree in remote["worktrees"])
        try:
            remote_terms = orca_result(orca, ["terminal", "list", "--environment", environment],
                                       "terminals", None, remote_timeout)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            detail = endpoint_diagnostic(environment, environments)
            print(f"cannot read terminals in Orca environment {environment}: {error}; {detail}",
                  file=sys.stderr)
            remote_terms = []
        terminals.extend({**term, "_environment": environment} for term in remote_terms)
    return worktrees, terminals


ORCA_TIMEOUT = 60


def screen_targets(worktrees: list[dict], terminals: list[dict], codex_cwds: Collection[str] | None = None) -> list[dict]:
    """Tabs of done or waiting worktrees, agent tabs of worktrees Orca keeps no agent for (a background
    session's hooks do not reach it), remote worktrees with no agent message, tabs of working Codex worktrees,
    local Codex tabs with no rollout, and remote shells without output metadata:
    read their cards, permission prompts, questions, final replies, and bare prompts."""
    worktrees, terminals = one_per_path(worktrees, terminals)
    idle = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees
            if any(a.get("state") in ("done", "waiting") for a in w.get("agents") or [])}
    untracked = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees if not w.get("agents")}
    bare = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees
            if w.get("_environment") and not orca_said(w)}
    quiet_codex = set() if codex_cwds is None else {
        (None, w.get("worktreeId")) for w in worktrees if not w.get("_environment") and w["path"] not in codex_cwds}
    rollout_tabs = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees
                    if codex_cwds is not None and not w.get("_environment") and w["path"] in codex_cwds}
    working_codex = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees
                     if any(a.get("agentType") == "codex" and a.get("state") == "working" for a in w.get("agents") or [])}
    codex_panes = {(w.get("_environment"), w.get("worktreeId"), a["paneKey"]) for w in worktrees
                   for a in w.get("agents") or [] if a.get("agentType") == "codex" and a.get("paneKey")}
    return [t for t in terminals if t.get("handle") and (
        (key := (t.get("_environment"), t.get("worktreeId"))) in idle or key in bare or key in working_codex
        or (key in untracked and (t.get("agentIdentity") or key in rollout_tabs))
        or (key in quiet_codex and (t.get("agentIdentity") == "codex" or (*key, hourly_runtime.pane_key(t)) in codex_panes))
        or (t.get("_environment") and not t.get("agentIdentity") and not t.get("preview")
            and t.get("lastOutputAt") is None))]


def read_screens(orca: str, terms: list[dict]) -> Screens:
    """Each tab's rendered screen by (environment, handle), read from the Orca server that issued the
    handle; a tab whose read fails is left out."""
    def read(term: dict) -> dict | None:
        where = ["--environment", term["_environment"]] if term.get("_environment") else []
        try:
            result = orca_payload(orca, ["terminal", "read", "--terminal", term["handle"], "--screen", *where])
            screen = result.get("terminal") or result
            return {**screen, "_observed_at": datetime.now(timezone.utc).isoformat()} if isinstance(screen, dict) else None
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
            return None

    if not terms:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(terms))) as pool:
        read_back = list(pool.map(read, terms))
    return {(term.get("_environment"), term["handle"]): screen
            for term, screen in zip(terms, read_back) if isinstance(screen, dict)}


def orca_runs(orca: str) -> list[dict]:
    """Every orchestration Run; run-list returns at most 100 a page."""
    runs, cursor, seen = [], None, set()
    while True:
        argv = [orca, "orchestration", "run-list", "--limit", "100", *(["--cursor", cursor] if cursor else []), "--json"]
        out = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=ORCA_TIMEOUT).stdout
        page = json.loads(out)["result"]
        runs.extend(page["runs"])
        seen.add(cursor)
        cursor = page.get("nextCursor")
        if not cursor or cursor in seen:
            return runs


def read_mail(args: argparse.Namespace) -> dict[str, list[dict]]:
    """Unread worker mail by lead terminal; none when Orca state comes from files without a mailbox file."""
    if args.ps_json and not args.inbox_json:
        return {}
    try:
        messages = orca_result(args.orca, ["orchestration", "inbox", "--limit", "2000"], "messages",
                               args.inbox_json)
        runs = orca_result(args.orca, [], "runs", args.runs_json) if args.runs_json else orca_runs(args.orca)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"could not read the orchestration mailbox ({error}): unread worker mail is not checked",
              file=sys.stderr)
        return {}
    return unread_worker_mail(messages, runs)


def expand_roots(patterns: list[str]) -> list[str]:
    roots = []
    for pattern in patterns:
        expanded = Path(pattern).expanduser()
        if not any(char in str(expanded) for char in "*?["):
            if expanded.is_dir():
                roots.append(str(expanded))
            continue
        base = Path(expanded.anchor) if expanded.is_absolute() else Path()
        relative_pattern = str(expanded.relative_to(base)) if expanded.is_absolute() else str(expanded)
        # glob.glob() was non-recursive and skipped hidden names for '*' parts.
        glob_pattern = re.sub(r"\*+", "*", relative_pattern)
        pattern_parts = Path(glob_pattern).parts
        roots.extend(str(path) for path in base.glob(glob_pattern)
                     if path.is_dir() and all(
                         not part.startswith(".") or expected.startswith(".")
                         for part, expected in zip(path.relative_to(base).parts, pattern_parts)))
    return roots


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    parser.add_argument("--ps-json", help="read `orca worktree ps --json` output from this file")
    parser.add_argument("--terminals-json", help="read `orca terminal list --json` output from this file")
    parser.add_argument("--inbox-json", help="read `orca orchestration inbox --json` output from this file; "
                                             "with --ps-json and without it, worker mail is not checked")
    parser.add_argument("--runs-json", help="read `orca orchestration run-list --json` output from this file")
    parser.add_argument("--claude-projects", default="~/.claude/projects",
                        help="Claude Code transcript root (default: ~/.claude/projects)")
    parser.add_argument("--codex-sessions", action="append",
                        help="Codex session root or glob; repeatable (default: Orca codex accounts and ~/.codex/sessions)")
    parser.add_argument("--since-hours", type=float, default=24,
                        help="skip Codex rollouts not modified within this many hours; an older quiet "
                             "Codex then reads as unstarted (default: 24)")
    parser.add_argument("--decision-marker", default="要判断",
                        help="worktree comment prefix that marks a pending human decision (default: 要判断)")
    parser.add_argument("--shell-idle-minutes", type=float, default=30,
                        help="a shell at its prompt counts as closable after this long without output "
                             "(default: 30); remote shells without output metadata use their rendered last line")
    parser.add_argument("--jev-key-file", default=JEV_KEY_FILE,
                        help="Jev (TypeSafe) API key file, read when TYPESAFE_API_KEY is unset "
                             "(default: %(default)s)")
    parser.add_argument("--review-out",
                        help="also write the rows a careful reader must class (every row Jev judged from a final "
                             "message without a verdict for it yet, and a stalled row whose own PR merged) to this "
                             "JSON file, for orca-row-classify; verdicts already written for any session's queue "
                             "beside the store are kept in --verdicts first")
    parser.add_argument("--only", metavar="NAME",
                        help="scan only this worktree (same names as --row), and include it in --review-out "
                             "even when it would not normally need a careful read")
    views = parser.add_mutually_exclusive_group()
    views.add_argument("--summary", action="store_true",
                       help="print class counts, review queue rows, and finished worktree/tab counts instead of JSON")
    views.add_argument("--rows", action="store_true",
                       help="print one line per row: name | class (the verdict's, else the scan's) | GitHub "
                            "repo#PR state | tab count | newest final message time | question, waits_on, "
                            "next_step, or the final message's start | reasons | git.dirty | git.unpushed")
    parser.add_argument("--class", dest="classes", action="append", metavar="CLASS",
                        help="only --rows of this class (the verdict's, else the scan's); repeatable")
    parser.add_argument("--sort", choices=("final",), help="with --rows, newest final message first; missing times last")
    views.add_argument("--cards", action="store_true",
                       help="print one line per done card (handle, agent, interrupted, first 60 draft characters, "
                            "whether its screen composer is empty, Orca's last message, and screen end), closable tab, and "
                            "meaningful tab, each led by the worktree's name and class as in --rows; with --row NAME "
                            "(repeatable), only the named rows")
    parser.add_argument("--row", metavar="NAME", action="append", default=[],
                        help="print one row as JSON with fixed keys: " + ", ".join(ROW_KEYS) + ". "
                             "Strings are never null, `final` is the newest "
                             "final message of Claude, Codex, or Orca, `transcripts` is "
                             '{"claude": [path…], "codex": [path…]} (--transcripts prints them, or on stderr why '
                             'there are none), `question_sources` the lane\'s ask and info naming the question '
                             'label (Q4) of an Orca comment summary the row shows without a transcript, and `github` '
                             'is owner/repo from origin; '
                             "NAME is repo/name, the last path segment (with optional ENVIRONMENT:: on another "
                             "Mac), its path, or its row identity. Reviews are read from $TRIAGE_REVIEW_DIR "
                             "or " + REVIEW_DIR + ". With --rm-specs or --cards, repeatable: the spec or the "
                             "lines of each named row")
    views.add_argument("--rm-specs", action="store_true",
                       help="with --from, print <path> <PR|-> for this Mac's open_pr_cleanup rows and finished "
                            "rows without meaningful tabs, for rm-check-batch; another Mac's rows are counted on "
                            "stderr, and --summary prints their rm-check-batch --host commands. With --row NAME "
                            "(repeatable), only the named rows, a finished one also while its tabs remain; a row "
                            "left out is named on stderr with why, and the exit status is 1. After the specs, "
                            "stderr prints the full rm-check-batch --from command with --remove-ok --summary")
    views.add_argument("--wake", action="store_true",
                       help="with --from, print one line per row an API failure stopped: name | its agent's tab | "
                            "what it needs (通信断: wake it, 利用上限: wait until it resets, ログイン切れ: a person "
                            "logs in, 文脈の上限: a new lane with a smaller scope, その他: a person reads it) | the error's first 200 characters")
    parser.add_argument("--transcripts", action="store_true",
                        help="with --row, print the row's transcript paths instead of JSON, one per line as "
                             "`claude <path>` or `codex <path>`")
    parser.add_argument("--from", dest="from_file", metavar="SCAN_JSON",
                        help="read the rows a previous scan printed as JSON instead of scanning again")
    parser.add_argument("--inbox", default=os.environ.get("TRIAGE_INBOX") or INBOX_FILE,
                        help="triage inbox whose newest done and ask per worktree, and newest info since the "
                             "lane's last instruction, go into the review queue (default: $TRIAGE_INBOX or %(default)s)")
    parser.add_argument("--ask-answers", default=str(inbox_read.answers_path()),
                        help="answer records for inbox asks (default: $TRIAGE_ASK_ANSWERS or %(default)s)")
    parser.add_argument("--verdicts", default=VERDICTS_FILE,
                        help="where orca-row-classify verdicts are kept per worktree (default: %(default)s)")
    parser.add_argument("--pr-cache", default=PR_ACTIVITY_DIR,
                        help="where each PR's reviews and comments are kept between runs, one file per PR; "
                             "they are read again only when the PR's updatedAt moves (default: %(default)s)")
    parser.add_argument("--state-file", type=Path, default=HOURLY_STATE,
                        help="shared hourly state; undelivered destinations stay open")
    parser.add_argument("--decisions-file", type=Path, default=LEAD_DECISIONS,
                        help="lead answers; rows with remove answers are left out of automatic removal")
    args = parser.parse_args(argv)
    if args.from_file and args.review_out:
        parser.error("--review-out needs a live scan; drop --from")
    if args.rm_specs and not args.from_file:
        parser.error("--rm-specs needs --from")
    if args.wake and not args.from_file:
        parser.error("--wake needs --from")
    if args.classes and not args.rows:
        parser.error("--class needs --rows")
    if args.row and (other := next((flag for flag, on in (("--summary", args.summary), ("--rows", args.rows),
                                                          ("--wake", args.wake)) if on), None)):
        parser.error(f"argument --row: not allowed with argument {other}")
    if len(args.row) > 1 and not (args.rm_specs or args.cards):
        parser.error("--row is repeatable only with --rm-specs or --cards")
    if args.transcripts and (args.rm_specs or args.cards):
        parser.error("--transcripts needs --row without --rm-specs or --cards")
    if args.transcripts and not args.row:
        parser.error("--transcripts needs --row")
    return args


def write_review_queue(path: str, rows: list[dict], verdicts: dict | None = None, *, force: bool = False) -> None:
    """Write {"generated_at", "rows"} so a later check can tell whether this scan's queue was classed."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "rows": review_queue(rows, verdicts, force=force)}
    write_json_atomically(target, payload)


def print_summary(rows: list[dict], verdicts: dict, *, force_review: bool = False,
                  scan_file: Path | None = None) -> None:
    # The class --rows --class keeps (the verdict's, else the scan's), with the scan's own count where it differs.
    shown = Counter((row.get("verdict") or {}).get("class") or row["class"] for row in rows)
    scanned = Counter(row["class"] for row in rows)
    print("classes: " + ", ".join(f"{name}={shown[name]}" + (f" (scan: {scanned[name]})" if scanned[name] != shown[name]
                                                            else "") for name in sorted(set(shown) | set(scanned))))
    queue = review_queue(rows, verdicts, force=force_review)
    print(f"review queue: {len(queue)}")
    for row in queue:
        print(f"  {row['repo']}/{Path(row['path']).name} [{row['class']}]")
    finished = [row for row in rows if ((row.get("verdict") or {}).get("class") or row["class"]) == "finished"]
    open_pr_cleanup = [row for row in rows if row.get("open_pr_cleanup")]

    if any(row.get("environment") and not removal_blocker(row)
           for row in (*finished, *open_pr_cleanup)) and batch_has_preview():
        print(REMOTE_CHECK_RELEASE)
    print(f"finished: {len(finished)} worktrees, {sum(len(row['terminals']) for row in finished)} tabs")
    for row in finished:
        print(f"  {row['repo']}/{Path(row['path']).name} ({len(row['terminals'])} tabs)")
        if not row.get("open_pr_cleanup") and not removal_blocker(row):
            print("    " + rm_check_command(row, scan_file))
    print(f"open PR cleanup: {len(open_pr_cleanup)} worktrees")
    for row in open_pr_cleanup:
        print(f"  {row['repo']}/{Path(row['path']).name} [{row['class']}]")
        if not removal_blocker(row):
            print("    " + rm_check_command(row, scan_file))
            print("    " + rm_check_command(row, scan_file, remove_ok=True))
    for row in rows:
        if blocker := row.get("automation_blocker"):
            print(f"{row_name(row)}: {blocker}")
        if answer := row.get("removal_decision"):
            newest = answer.get("newest")
            print(f"removal decision: {row_name(row)} {answer['decided_at']} {answer['answer']}" + (
                f" | newer {newest['decision']} {newest['decided_at']} {newest['answer']}" if newest else ""))
    hooks = [(row, error) for row in rows for error in row.get("hook_errors") or []]
    print(f"hook errors: {len(hooks)}")
    for row, error in hooks:
        print(f"  {error['host']} {error['path']} ({row_name(row)} {error.get('tab') or '-'})")


def row_name(row: Mapping) -> str:
    name = Path(row["path"]).name
    return f"{row['environment']}::{name}" if row.get("environment") else name


def row_aliases(row: Mapping) -> list[str]:
    """The names a view shows for a row, shortest first: name, repo/name (also under each other repo Orca lists the
    path in), the name another Mac's row shows, that with repo/name, and the path."""
    name = Path(row["path"]).name
    qualified = [f"{repo or ''}/{name}" for repo in (row.get("repo"), *(row.get("other_repos") or []))]
    remote = [f"{row['environment']}::{each}" for each in qualified] if row.get("environment") else []
    return [name, *qualified, row_name(row), *remote, row["path"]]


def matching_rows(rows: Sequence[dict], key: str) -> list[dict]:
    """The rows `key` names, an exact identity taking precedence over aliases."""
    return [row for row in rows if row_identity(row) == key] or [row for row in rows if key in row_aliases(row)]


def row_choice(rows: Sequence[dict], row: dict) -> str:
    """The name the views show when it selects the row alone, else its first alias that does, else its identity."""
    return next((alias for alias in (row_name(row), *row_aliases(row))
                 if len(found := matching_rows(rows, alias)) == 1 and found[0] is row), row_identity(row))


def select_row(rows: Sequence[dict], key: str, option: str = "--row") -> dict:
    """Resolve names shown by the views. Several matches are listed with the environment, the repo, and the value
    that selects each one."""
    found = matching_rows(rows, key)
    if len(found) == 1:
        return found[0]
    if found:
        choices = [f"{row_choice(rows, row)} (environment {row.get('environment') or 'local'}, "
                   f"repo {row.get('repo') or '-'})" for row in found]
        raise ValueError(f"{option} {key}: matches multiple rows; candidates: " + ", ".join(choices))
    # With no match, the rows whose name or path contains the key, else every row.
    candidates = [row for row in rows if key.casefold() in f"{row_name(row)} {row['path']}".casefold()] or rows
    raise ValueError(f"{option} {key}: no row has this name; candidates: "
                     + (", ".join(row_choice(rows, row) for row in candidates) or "(none)"))


def transcript_final(row: dict) -> dict:
    """{source, at, text} of the newest final message the row's transcripts or Orca hold; empty strings without
    one."""
    latest = latest_message({"claude": row.get("claude"), "codex": row.get("codex") or [],
                             "orca_message": row.get("orca_message")})
    if latest is None:
        return {"source": "", "at": "", "text": ""}
    source, text = latest
    at = {"claude": (row.get("claude") or {}).get("final_at"),
          "codex": ((row.get("codex") or [{}])[-1]).get("last_assistant_at"),
          "orca": (row.get("orca_message") or {}).get("at")}[source]
    return {"source": source, "at": at or "", "text": text or ""}


def newer_card(row: dict, said: dict) -> dict | None:
    """The newest done card's last message as {source: "card", at, text}, when Orca recorded it after `said` and it
    says something else; else None.

    A session the scan could not tie to the worktree (no process, or a cwd outside it) leaves the worktree's
    transcript behind its tab, while Orca's card holds the tab's last word. The same message, recorded a second
    after the transcript's entry, keeps the transcript's.
    """
    cards = [(at, card) for card in row.get("done_cards") or []
             if card.get("last_message") and (at := parse_time(card.get("done_at")))]
    if not cards:
        return None
    at, card = max(cards, key=lambda item: item[0])
    said_at = parse_time(said["at"])
    if " ".join(card["last_message"].split()) == " ".join(said["text"].split()) or (said_at and at <= said_at):
        return None
    return {"source": "card", "at": card["done_at"], "text": card["last_message"]}


def final_message(row: dict) -> dict:
    """{source, at, text} of the tab's newest final message: the transcripts' or Orca's, or a newer done card's
    (source "card"); empty strings without one."""
    said = transcript_final(row)
    return newer_card(row, said) or said


def github_of(row: dict) -> str:
    """owner/repo of the row's checkout: from the scan's git facts, else from a local checkout's origin."""
    known = (row.get("git") or {}).get("github")
    if known or row.get("environment"):
        return known or ""
    try:
        origin = subprocess.run(["git", "-C", row["path"], "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if origin.returncode != 0:
        return ""
    return github_slug(origin.stdout.strip()) or ""


REVIEW_DIR = "~/.cache/orca-briefs/review/out"
MERGE = re.compile(r"(?<![A-Za-z])merge(?![A-Za-z])|マージ", re.IGNORECASE)
ROW_KEYS = ("identity", "name", "path", "repo", "environment", "github", "class", "scan_class", "reasons",
            "question", "merge_question", "review", "stop", "waits_on", "next_step", "verdict", "final",
            "last_user", "transcripts", "comment", "pr", "git", "prompt_waiting", "queued_input", "processes",
            "tabs", "done_cards", "closable", "meaningful_tabs", "open_pr_cleanup", "claude_sessions", "codex_approvals",
            "automation_workspaces", "automation_blocker", "question_sources")


class ReviewResult(TypedDict):
    head: str
    verdict: str
    path: str
    head_matches: bool | None
    changes_requested: bool


class Transcripts(TypedDict):
    claude: list[str]
    codex: list[str]


def asks_merge(question: str, comment: str) -> bool:
    """Whether a question, or a 要判断 in the worktree's Orca comment, asks about a merge."""
    return bool(MERGE.search(question) or ("要判断" in comment and MERGE.search(comment)))


def review_result(row: dict, github: str) -> ReviewResult | None:
    """The completed review of this PR, with whether it still describes the current PR head."""
    pr = row.get("pr") or row.get("linked_pr") or {}
    ref = PR_URL.search(pr.get("url") or "")
    slug = github or (ref[1] if ref else "")
    number = pr.get("number") or (ref[2] if ref else None)
    if not SLUG.fullmatch(slug) or not number:
        return None
    directory = Path(os.environ.get("TRIAGE_REVIEW_DIR") or REVIEW_DIR).expanduser()
    path = directory / f"{slug.replace('/', '-')}-{number}.md"
    try:
        raw = path.read_bytes()
        stamp = json.loads(path.with_suffix(".reviewed.json").read_text(encoding="utf-8"))
        if (not isinstance(stamp, dict) or not isinstance(stamp.get("head"), str) or not stamp["head"]
                or stamp.get("sha256") != hashlib.sha256(raw).hexdigest()):
            return None
        verdict = next((line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()), "")
    except (OSError, ValueError, UnicodeError):
        return None
    if not verdict:
        return None
    current = pr["head_oid"].startswith(stamp["head"]) if pr.get("head_oid") else None
    return {"head": stamp["head"], "verdict": verdict, "path": str(path.absolute()),
            "head_matches": current, "changes_requested": current is True and "直してから" in verdict}


def row_question(row: dict) -> str:
    """What a row asks the user. A waiting row asks a prompt on its screen, else the text after the decision marker
    in its Orca comment, the Codex reply read on its screen, its verdict's question, or its final message, in that
    order; another row asks only a prompt or its verdict's question."""
    verdict = row.get("verdict") or {}
    primary = row.get("prompt_waiting") or {}
    prompt = "\n".join(filter(None, [prompt_question(primary), *(
        f"{approval['tab']}: {prompt_question(approval)}" for approval in row.get("codex_approvals") or []
        if approval != primary)]))
    if (verdict.get("class") or row.get("class")) != "waiting":
        return prompt or str(verdict.get("question") or "")
    screen = row.get("orca_message") or {}
    return next((str(text) for text in (prompt, row.get("marker_question"),
                                        screen.get("text") if screen.get("from") == "screen" else "",
                                        verdict.get("question"), final_message(row)["text"]) if text), "")


QUESTION_LABEL = re.compile(r"(?<![A-Za-z0-9])Q\d+(?![0-9])")
QUESTION_SOURCES = 3  # labels such as Q1 come back in later rounds of a lane's questions


def question_sources(row: dict, entries: Sequence[dict]) -> list[dict]:
    """The lane's ask and info reports behind a question this Mac knows only from its Orca comment's summary.

    Without a transcript here, the 要判断 line is all the row shows. Each report of the worktree naming a question
    label the summary names (Q4) is a source: the newest QUESTION_SOURCES, oldest first, with id, kind, ts, and
    the paragraphs naming the label.
    """
    summary = str(row.get("marker_question") or "")
    labels = set(QUESTION_LABEL.findall(summary))
    if (not labels or row_question(row) != summary or (row.get("claude") or {}).get("transcript")
            or row.get("codex")):
        return []
    name, repos = Path(row["path"]).name, row_repos(row)
    found = []
    for item in entries:
        if (item.get("kind") not in ("ask", "info") or item.get("worktree") != name
                or (item.get("repo") or "") not in repos or (at := parse_time(item.get("ts"))) is None):
            continue
        asked = [str(item.get("text") or ""), *(str(question.get("question") or "")
                                               for question in inbox_read.questions_of(item)
                                               if isinstance(question, dict))]
        paragraphs = [part.strip() for text in asked for part in re.split(r"\n\s*\n", text)
                      if labels & set(QUESTION_LABEL.findall(part))]
        if paragraphs:
            found.append((at, {"id": item.get("id"), "kind": item["kind"], "ts": item["ts"],
                               "text": "\n\n".join(paragraphs)}))
    found.sort(key=lambda pair: pair[0])
    return [source for _, source in found[-QUESTION_SOURCES:]]


def missing_transcripts(row: dict) -> str:
    """Why --transcripts lists nothing for a row: what the scan looked for, and where the shown reply came from."""
    if row.get("environment"):
        return f"{row_name(row)}: no transcript on this Mac; the worktree is on {row['environment']}"
    source = final_message(row)["source"]
    said = ("the tab's screen" if source == "orca" and (row.get("orca_message") or {}).get("from") == "screen"
            else {"card": "Orca's done card", "orca": "Orca", "": "nowhere"}.get(source, source))
    return (f"{row_name(row)}: no transcript. The scan found no Claude transcript for {row['path']} under the Claude "
            f"projects directory and no Codex rollout with that cwd modified within --since-hours; the final "
            f"message shown was read from {said}")


def row_view(row: dict, entries: Sequence[dict] = ()) -> dict:
    """One row with fixed keys and no null strings, so a reader never has to know the scan's raw shapes."""
    verdict = row.get("verdict") or {}
    final = final_message(row)
    asked = {"claude": (row.get("claude") or {}).get("last_user"),
             "codex": ((row.get("codex") or [{}])[-1]).get("last_user")}.get(final["source"])
    github = github_of(row)
    question = row_question(row)
    merge_question = asks_merge(question, row.get("comment") or "")
    transcripts: Transcripts = {
        "claude": [path] if (path := (row.get("claude") or {}).get("transcript")) else [],
        "codex": [rollout["rollout"] for rollout in row.get("codex") or [] if rollout.get("rollout")],
    }
    return {
        "identity": row_identity(row), "name": row_name(row), "path": row["path"],
        "repo": row.get("repo") or "", "environment": row.get("environment") or "", "github": github,
        "class": verdict.get("class") or row["class"], "scan_class": row["class"],
        "reasons": row.get("reasons") if isinstance(row.get("reasons"), list) else [],
        "question": question, "merge_question": merge_question, "review": review_result(row, github),
        "stop": row.get("stop"),
        "waits_on": verdict.get("waits_on") or row.get("waits_on") or "",
        "next_step": verdict.get("next_step") or "", "verdict": verdict or None,
        "final": final, "last_user": asked or "", "transcripts": transcripts,
        "comment": row.get("comment") or "", "pr": row.get("pr"), "git": row.get("git"),
        "prompt_waiting": row.get("prompt_waiting"), "codex_approvals": row.get("codex_approvals") or [],
        "queued_input": row.get("queued_input") or [],
        "processes": row.get("processes") or [],
        "claude_sessions": row.get("claude_sessions") or [],
        "automation_workspaces": row.get("automation_workspaces") or [],
        "automation_blocker": row.get("automation_blocker") or "",
        "tabs": [{"handle": t.get("handle") or "", "agent": t.get("agent") or "", "title": t.get("title") or ""}
                 for t in row.get("terminals") or []],
        # Whole, unlike --cards: the prompt and last message are what a reader came for.
        "done_cards": [{"agent": card.get("agent") or "", "handle": card.get("handle") or "",
                        "done_at": card.get("done_at") or "", "interrupted": bool(card.get("interrupted")),
                        "prompt": card.get("prompt") or "", "last_message": card.get("last_message") or "",
                        "screen_tail": card.get("screen_tail") or [], "draft": card.get("draft"),
                        "draft_text": card.get("draft_text") or "", "composer_empty": card.get("composer_empty")}
                       for card in row.get("done_cards") or []],
        "closable": row.get("closable") or [], "meaningful_tabs": row.get("meaningful_tabs") or [],
        "open_pr_cleanup": bool(row.get("open_pr_cleanup")),
        "question_sources": question_sources(row, entries),
    }


def class_label(shown: str, scanned: str) -> str:
    return shown + (f" (scan: {scanned})" if shown != scanned else "")


def row_line(view: dict) -> str:
    klass = class_label(view["class"], view["scan_class"])
    pr = view["pr"] or {}
    where = (view["github"] + (f"#{pr['number']} {pr.get('state') or ''}".rstrip() if pr.get("number") else "")
             if view["github"] else "-")
    said = " ".join(view["final"]["text"].split())
    note = " ".join((view["question"] or view["waits_on"] or view["next_step"]).split()) or (
        said[:80] + ("…" if len(said) > 80 else ""))
    reasons = " ".join("; ".join(view["reasons"]).split()) or "-"
    git = view["git"] or {}
    dirty = json.dumps(git.get("dirty"))
    unpushed = json.dumps(git.get("unpushed"))
    sessions = "; ".join(" ".join(f"{key}={session.get(key) if session.get(key) is not None else '-'}"
                                  for key in ("pid", "ORCA_TERMINAL_HANDLE", "status", "waitingFor"))
                         for session in view.get("claude_sessions") or [])
    approvals = " ; ".join(f"{item['tab']} {item.get('target') or item['question']} [{'; '.join(item['choices'])}]"
                           for item in view.get("codex_approvals") or [])
    return (f"{view['name']} | {klass} | {where} | tabs {len(view['tabs'])} | {view['final']['at'] or '-'} | {note}"
            f" | reasons {reasons} | git.dirty {dirty} | git.unpushed {unpushed}"
            + (f" | Claude sessions {sessions}" if sessions else "")
            + (f" | Codex approvals {approvals}" if approvals else ""))


CLOSE_TABS = Path(__file__).absolute().with_name("close_tabs.py")
RM_CHECK = Path(__file__).absolute().with_name("rm_check.py")
RM_CHECK_BATCH = Path.home() / ".local/share/eotel-harness/current/bin/rm-check-batch"
REMOTE_CHECK_RELEASE = "Remote checks require the same harness release on both Macs."


def pr_number(row: dict) -> int | None:
    return (row.get("pr") or {}).get("number") or (row.get("linked_pr") or {}).get("number")


def rm_spec(row: dict) -> str:
    path = row["path"]
    name = shlex.quote(path) if any(char.isspace() for char in path) else path
    return f"{name} {pr_number(row) or '-'}"


@cache
def batch_has_preview() -> bool:
    """The same harness capability introduces read-only cleanup planning and quoted specs."""
    try:
        help_text = subprocess.run([str(RM_CHECK_BATCH), "--help"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return help_text.returncode == 0 and re.search(r"(?<!\S)--preview\s", help_text.stdout) is not None


def batch_requirement(row: dict) -> str | None:
    if any(char.isspace() for char in row["path"]) and not batch_has_preview():
        return f"{row['path']}: batch checks for paths containing spaces require the harness release with --preview"
    return None


def rm_check_command(row: dict, scan_file: Path | None = None, *, remove_ok: bool = False) -> str:
    """A one-line check for this row, using the saved scan's timestamp when available."""
    environment = row.get("environment")
    from_scan = ["--from", str(scan_file)] if scan_file else []
    options = [*from_scan, "--summary"]
    plan_cleanup = bool(row.get("open_pr_cleanup") and scan_file and batch_has_preview())
    if not remove_ok and not plan_cleanup and not environment:
        return shlex.join([str(RM_CHECK), *options, row["path"], str(pr_number(row) or "-")])
    if requirement := batch_requirement(row):
        return requirement
    if not batch_has_preview():
        options = from_scan if remove_ok else []
    if remove_ok:
        options.extend(("--detach-nested", "--archive-scratch", "--remove-ok"))
    elif plan_cleanup:
        options.extend(("--preview", "--detach-nested"))
    return shlex.join([str(RM_CHECK_BATCH), *(["--host", environment] if environment else []), *options, rm_spec(row)])


def card_lines(row: dict, scan_file: Path | None = None) -> list[str]:
    """A line per done card, closable tab, and meaningful tab; a scan saved before cards had screens reads as unknown.

    With the saved scan's path, a row with closable tabs includes the close_tabs.py command that closes them
    once nothing moved after the scan. Finished and open_pr_cleanup rows include a removal check command.
    """
    lead = f"{row_name(row)} | {class_label((row.get('verdict') or {}).get('class') or row['class'], row['class'])}"
    where = ["--environment", row["environment"]] if row.get("environment") else []

    def read(handle: str | None) -> str:
        # The handle goes behind --terminal: `orca terminal read HANDLE` is an unknown command.
        return (" | read: " + shlex.join(["orca", "terminal", "read", "--terminal", handle, *where])
                if handle and not handle.startswith(NO_HANDLE) else "")

    lines = []
    for card in row.get("done_cards") or []:
        said = " ".join((card.get("last_message") or "").split())
        who = f"card {card.get('handle') or '-'} {card.get('agent') or '-'}" + (
            " interrupted" if card.get("interrupted") else "")
        # A scan saved before cards kept the draft's start has only `draft`.
        draft = ("unknown" if card.get("draft") is None
                 else card.get("draft_text") or ("yes" if card["draft"] else "-"))
        composer = {True: "empty", False: "has text"}.get(card.get("composer_empty"), "unknown")
        excerpt = (said[:160] + ("…" if len(said) > 160 else "")) or "-"
        lines.append(f"{lead} | {who}{read(card.get('handle'))} | draft {draft} | composer {composer} | {excerpt}"
                     " | screen: " + (" ⏎ ".join(card.get("screen_tail") or []) or "-"))
    lines.extend(f"{lead} | closable {handle}" for handle in row.get("closable") or [])
    lines.extend(f"{lead} | meaningful {handle}{read(handle)}" for handle in row.get("meaningful_tabs") or [])
    if scan_file and row.get("closable"):
        lines.append(f"{lead} | close: " + shlex.join([str(CLOSE_TABS), "--from", str(scan_file),
                                                      *row["closable"]]))
    if not removal_blocker(row) and (row.get("open_pr_cleanup") or (
            (row.get("verdict") or {}).get("class") or row["class"]) == "finished"):
        if row.get("environment") and batch_has_preview():
            lines.append(f"{lead} | {REMOTE_CHECK_RELEASE}")
        lines.append(f"{lead} | rm-check: " + rm_check_command(row, scan_file))
    return lines


def saved_rows(path: str) -> list[dict]:
    rows = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(row, dict) and "path" in row for row in rows):
        raise ValueError(f"{path} is not the JSON list a scan prints")
    return rows


def print_row(rows: list[dict], key: str, transcripts: bool = False, inbox: Path | None = None) -> int:
    try:
        row = select_row(rows, key)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        entries = inbox_read.inbox_entries(inbox) if inbox and row.get("marker_question") else []
    except OSError:
        entries = []
    view = row_view(row, entries)
    if transcripts:
        for agent, paths in view["transcripts"].items():
            for path in paths:
                print(f"{agent} {path}")
        if not any(view["transcripts"].values()):
            print(missing_transcripts(row), file=sys.stderr)
        return 0
    json.dump(view, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.from_file:
        try:
            rows = saved_rows(args.from_file)
        except (OSError, ValueError) as error:
            print(f"--from: {error}", file=sys.stderr)
            return 1
        verdicts = absorb_verdicts(args.verdicts, write=False)
        if args.only:
            try:
                rows = [select_row(rows, args.only, "--only")]
            except ValueError as error:
                print(error, file=sys.stderr)
                return 1
        rows = with_cleanup_constraints(with_verdicts(rows, verdicts), args.state_file.expanduser(),
                                        args.decisions_file.expanduser())
        return print_rows(args, rows, verdicts)
    # close_tabs.py keeps a tab whose worktree moved after this moment.
    scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    worktrees, terminals = collect_worktrees_and_terminals(args.orca, args.ps_json, args.terminals_json)
    if args.only:
        worktrees, terminals = one_per_path(worktrees, terminals)
        choices = [{"path": w["path"], "repo": w.get("repo"), "environment": w.get("_environment"),
                    "other_repos": w.get("_other_repos")}
                   for w in worktrees]
        try:
            selected = select_row(choices, args.only, "--only")
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        worktrees = [w for w in worktrees if (w["path"], w.get("_environment"))
                     == (selected["path"], selected.get("environment"))]
        ids = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees}
        terminals = [t for t in terminals if (t.get("_environment"), t.get("worktreeId")) in ids]
    since = time.time() - args.since_hours * 3600 if args.since_hours else None
    codex = read_codex(expand_roots(args.codex_sessions or DEFAULT_CODEX_SESSIONS), since)
    key = jev_key(os.environ, args.jev_key_file)
    if key is None:
        print(f"TYPESAFE_API_KEY is unset and {args.jev_key_file} is missing or unreadable: "
              "idle final messages come back unsure, so read them", file=sys.stderr)
    reach = Reach()
    judge = jev_judge(key, reach=reach) if key else (lambda message: None)
    # Orca state read from files has no live tabs to read.
    screens = {} if args.ps_json else read_screens(args.orca, screen_targets(worktrees, terminals, set(codex)))
    claude_root = str(Path(args.claude_projects).expanduser())
    processes = {} if args.ps_json else worktree_processes(
        [w["path"] for w in worktrees if not w.get("_environment")], Path(PROCESS_CPU).expanduser(), time.time())
    local = {w.get("worktreeId"): w["path"] for w in worktrees if not w.get("_environment")}
    live_sessions: dict[str, list[dict]] = {}
    live = {} if args.ps_json else running_claude_transcripts(
        list(local.values()), claude_session_dirs(), claude_root, process_table(), process_cwds,
        tabs={t["handle"]: local[t.get("worktreeId")] for t in terminals
              if t.get("handle") and not t.get("_environment") and t.get("worktreeId") in local},
        session_details=live_sessions)
    verdicts = absorb_verdicts(args.verdicts, write=bool(args.review_out))
    rows = add_next_step_facts(build_inventory(worktrees, terminals, claude_root,
                                               codex, args.decision_marker,
                                               shell_idle_minutes=args.shell_idle_minutes, judge=judge,
                                               mail=read_mail(args), screens=screens, live_transcripts=live,
                                               processes=processes, live_sessions=live_sessions),
                               pr_lookup=partial(gh_prs, cache_dir=Path(args.pr_cache).expanduser(), reach=reach),
                               verdicts=verdicts, proposals=ledger_proposals)
    if not args.ps_json:
        with_codex_permission_reasons(rows)
    if down := reach.down():
        line = f"ネットワーク断: {'。'.join(down)}。接続が戻ってから走査し直してください（--review-out は書いていません）"
        print(line)
        print(line, file=sys.stderr)
        return NETWORK_DOWN_EXIT
    rows = [{**row, "scanned_at": scanned_at}
            for row in with_inbox(rows, inbox_reports(Path(args.inbox).expanduser(), Path(args.ask_answers).expanduser()))]
    try:
        rows = with_reported_times(rows, inbox_read.inbox_entries(Path(args.inbox).expanduser()))
    except OSError:
        pass
    if not args.ps_json:
        rows = with_automation_workspaces(rows, args.orca)
    rows = with_verdicts(rows, verdicts)
    rows = with_cleanup_constraints(rows, args.state_file.expanduser(), args.decisions_file.expanduser())
    if args.review_out:
        write_review_queue(args.review_out, rows, verdicts, force=bool(args.only))
    return print_rows(args, rows, verdicts)


WAKE_ACTIONS = {"connection": "通信断: 起こす", "login": "ログイン切れ: 人が /login する",
                "context_limit": "文脈の上限: 範囲を小さくして新しい担当に渡す", "other": "その他: 人が読む"}


def reset_time(value: str | None, now: datetime, failed_at: datetime | None) -> datetime | None:
    """Read an absolute reset timestamp or the dated/time-only notice shown by the CLI."""
    if value and (parsed := parse_time(value)):
        return parsed
    match = re.fullmatch(r"(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2}) at )?"
                         r"(\d{1,2})(?::(\d{2}))?(am|pm) \(([^)]+)\)", value or "", re.IGNORECASE)
    if not match:
        return None
    month, day, hour, minute, period, zone = match.groups()
    if not month and failed_at is None:
        return None
    try:
        local = (failed_at or now).astimezone(ZoneInfo(zone))
        hour_number = int(hour)
        if not 1 <= hour_number <= 12:
            return None
        months: Sequence[str] = "jan feb mar apr may jun jul aug sep oct nov dec".split()
        month_number = next(number for number, label in enumerate(months, 1) if label == month.lower()) if month else local.month
        reset = local.replace(month=month_number, day=int(day) if day else local.day,
                              hour=hour_number % 12 + (12 if period.lower() == "pm" else 0),
                              minute=int(minute or 0), second=0, microsecond=0)
        return reset + timedelta(days=1) if not month and reset < local else reset
    except (ValueError, ZoneInfoNotFoundError):
        return None


def stopped_claude_tab(row: dict, candidates: list[str]) -> str | None:
    """Resolve a stopped transcript through its live sessions/<pid>.json and the process's Orca handle."""
    transcript = (row.get("claude") or {}).get("transcript")
    if row.get("environment") or not transcript:
        return None
    processes = process_table()
    pids = set()
    for directory in claude_session_dirs():
        for path in directory.glob("*.json"):
            record = read_json_file(path)
            if not isinstance(record, dict) or record.get("sessionId") != Path(transcript).stem:
                continue
            pid = record.get("pid")
            if isinstance(pid, int) and "claude" in processes.get(pid, (0, ""))[1]:
                pids.add(pid)
    chains = [ancestry(pid, processes) for pid in pids]
    handles = rm_check.process_handles(sorted({pid for chain in chains for pid in chain})) if chains else {}
    matched = {handle for chain in chains
               if (handle := next((handles[pid] for pid in chain if handles.get(pid) in candidates), None))}
    return next(iter(matched)) if len(matched) == 1 else None


def wake_line(row: dict, stop: StopReason) -> str:
    """name | the stopped agent's tab, or its candidates when several agent tabs could be it | what it needs |
    the error."""
    tabs = [t["handle"] for t in row.get("terminals") or [] if t.get("handle") and t.get("agent")
            and (stop["agent"] is None or t["agent"] == stop["agent"])]
    tab = tabs[0] if len(tabs) == 1 else f"不明（{', '.join(tabs)}）" if tabs else None
    if stop.get("tab"):
        tab = stop["tab"]
    elif len(tabs) > 1 and stop["agent"] == "claude":
        tab = stopped_claude_tab(row, tabs) or tab
    action = (f"利用上限: {stop['resets'] or '戻る時刻は不明'} まで待つ" if stop["kind"] == "usage_limit"
              else WAKE_ACTIONS[stop["kind"]])
    if stop["kind"] == "usage_limit":
        now = datetime.fromtimestamp(time.time(), timezone.utc)
        resets = reset_time(stop["resets"], now, parse_time(row_final_at(row)))
        if resets and resets <= now:
            action = "利用上限: 解除済み・起こせる" + ("（タブの開き直しが必要）" if not tabs else "")
        if (answer := row.get("wake_decision")) and "起こさない" in answer["answer"]:
            action = f"利用上限: {answer['decided_at']} " + " ".join(answer["answer"].split())
    error = " ".join(stop["error"].split())
    return f"{row_name(row)} | {tab or '-'} | {action} | {error[:200]}{'…' if len(error) > 200 else ''}"


def named_rm_specs(rows: list[dict], names: list[str], scan_file: Path) -> int:
    """Print `<path> <PR|->` for each named row a removal check may take: an open PR cleanup row, or a finished one
    even while its tabs remain (rm_check.py names the tabs to close). Every other row is named on stderr with why,
    and makes the exit status 1; an unknown name prints nothing."""
    try:
        selected = [select_row(rows, name) for name in names]
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    left_out = 0
    remote_preview = False
    specs = []
    # A row named twice (by name and by path) is printed once.
    for row in {id(row): row for row in selected}.values():
        shown = (row.get("verdict") or {}).get("class") or row["class"]
        if blocker := removal_blocker(row):
            why = blocker
        elif not (removal_approved(row) or row.get("open_pr_cleanup") or shown == "finished"):
            why = f"class {class_label(shown, row['class'])} is neither finished nor open PR cleanup"
        elif requirement := batch_requirement(row):
            why = requirement
        elif row.get("environment"):
            why = "on another Mac: " + rm_check_command(row, scan_file)
            remote_preview = remote_preview or batch_has_preview()
        else:
            specs.append(rm_spec(row))
            continue
        left_out += 1
        print(f"{row_name(row)}: left out: {why} | {removal_facts(row)}", file=sys.stderr)
    if remote_preview:
        print(REMOTE_CHECK_RELEASE, file=sys.stderr)
    print_rm_specs(specs, scan_file)
    return 1 if left_out else 0


def print_rm_specs(specs: list[str], scan_file: Path) -> None:
    """Keep stdout usable as specs; give the executable next command on stderr after them."""
    for spec in specs:
        print(spec)
    if specs:
        sys.stdout.flush()
        print(shlex.join([str(RM_CHECK_BATCH), "--from", str(scan_file), "--remove-ok", "--summary", *specs]),
              file=sys.stderr)


def print_rows(args: argparse.Namespace, rows: list[dict], verdicts: dict) -> int:
    if args.row and args.rm_specs:
        return named_rm_specs(rows, args.row, Path(args.from_file).expanduser().absolute())
    if args.row and args.cards:
        try:
            rows = list({id(row): row for row in (select_row(rows, name) for name in args.row)}.values())
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
    elif args.row:
        return print_row(rows, args.row[0], args.transcripts, Path(args.inbox).expanduser())
    if args.rows:
        if args.sort == "final":
            rows = sorted(rows, key=lambda row: parse_time(row_final_at(row)) or
                          datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for row in rows:
            if not args.classes or ((row.get("verdict") or {}).get("class") or row["class"]) in args.classes:
                print(row_line(row_view(row)))
    elif args.rm_specs:
        remote = 0
        specs = []
        for row in rows:
            finished = ((row.get("verdict") or {}).get("class") or row["class"]) == "finished"
            if blocker := removal_blocker(row):
                if not row.get("pending_handoffs") or row.get("automation_blocker"):
                    print(f"{row_name(row)}: {blocker} | {removal_facts(row)}", file=sys.stderr)
                continue
            if removal_approved(row) or row.get("open_pr_cleanup") or finished and not row.get("meaningful_tabs"):
                if requirement := batch_requirement(row):
                    print(requirement, file=sys.stderr)
                elif row.get("environment"):
                    remote += 1
                else:
                    specs.append(rm_spec(row))
        print_rm_specs(specs, Path(args.from_file).expanduser().absolute())
        if remote:
            print(f"{remote} rows on another Mac left out: --summary prints their rm-check-batch --host commands",
                  file=sys.stderr)
    elif args.wake:
        for row in rows:
            if stop := row.get("stop"):
                print(wake_line(row, stop))
    elif args.cards:
        scan_file = Path(args.from_file).expanduser().absolute() if args.from_file else None
        for row in rows:
            for line in card_lines(row, scan_file):
                print(line)
    elif args.summary:
        print_summary(rows, verdicts, force_review=bool(args.only),
                      scan_file=Path(args.from_file).expanduser().absolute() if args.from_file else None)
    else:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
