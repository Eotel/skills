#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
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
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
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
from typing import Any, NamedTuple, NotRequired, TypedDict
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hourly_runtime
import inbox_read
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


def claude_session_dirs() -> list[Path]:
    return [path for pattern in CLAUDE_SESSION_DIRS for path in Path.home().glob(pattern) if path.is_dir()]


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
    """The transcript of a session under its cwd's project directory, or under any one after its cwd moved."""
    name = session_id + ".jsonl"
    roots = list(dict.fromkeys(roots))
    exact = (path for root in roots if (path := find_claude_dir(root, cwd) / name).is_file())
    moved = (path for root in roots if root.is_dir() for path in root.glob("*/" + name))
    return next(exact, None) or next(moved, None)


def running_claude_transcripts(worktree_paths: Sequence[str], session_dirs: Sequence[Path], projects_root: str | Path,
                               processes: dict[int, tuple[int, str]],
                               cwds: Callable[[list[int]], dict[int, str]], tabs: Mapping[str, str] | None = None,
                               tab_of: Callable[[list[int]], dict[int, TabProcess]] = tab_processes
                               ) -> dict[str, list[Path]]:
    """The transcripts of running Claude sessions by the worktree each runs under.

    A session can move its cwd out of the worktree (into a worktree under .claude/worktrees, or as a
    background session its tab's Claude started), or start outside it, and its transcript then lands in another
    project directory. `tabs` maps an Orca tab's handle to its worktree: a session whose process or ancestor
    carries one of them in ORCA_TERMINAL_HANDLE (read by `tab_of`) belongs to that tab's worktree, and a Claude in
    such a tab with no session file is known by the session its --resume or --session-id names. Otherwise its
    worktree is the first of the process and its ancestors whose cwd, from its session file or else from lsof,
    is inside one.
    """
    live = {}
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
    tab_paths: Mapping[str, str] = tabs or {}
    claude = [pid for pid, (_, command) in processes.items() if "claude" in command] if tab_paths else []
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
    return {worktree: sorted(paths) for worktree, paths in found.items()}


# Lines read from the end of a transcript for its newest entry.
TAIL_LINES = 400
EPOCH = datetime.fromtimestamp(0, timezone.utc)


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


def read_claude(projects_root: str | Path, worktree_path: str, running: Sequence[Path] = ()) -> dict | None:
    """Summarize the newest Claude transcript for a worktree, or None when there is none.

    `running` adds transcripts of Claude sessions running under it that live in other project directories.
    """
    candidates = list(find_claude_dir(projects_root, worktree_path).glob("*.jsonl"))
    if not candidates and not running:
        return None
    path = max(candidates, key=lambda p: p.stat().st_mtime, default=None)
    if running:
        # The transcript a session continued from keeps getting untimed lines, which move its mtime alone.
        path = max(dict.fromkeys([*running, *([path] if path else [])]),
                   key=lambda p: (last_entry_at(p) or EPOCH, p.stat().st_mtime))
    assert path is not None
    rows = load_jsonl(path)
    last = max((i for i, e in enumerate(rows) if is_real_user_message(e)), default=-1)
    texts, final_at, asked, answered, api_error, api_error_kind = [], None, {}, set(), None, None
    question_times: dict[str, str] = {}
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
        "api_error": api_error,
        "api_error_kind": api_error_kind,
        "one_shot_schedules": one_shot_schedules(rows),
    }


def message_text(payload: dict) -> str:
    return " ".join(c.get("text", "") for c in payload.get("content") or [] if isinstance(c, dict))


API_FAILURE = re.compile(
    r"(?:Selected model is at capacity|API Error:|rate limit(?:ed| exceeded)?|usage limit|"
    r"request failed|connection error|stream error|stream disconnected before completion|server error)", re.IGNORECASE)


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
    last_assistant = last_assistant_at = api_error = None
    for row in load_jsonl(path):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        cwd = cwd or payload.get("cwd")
        kind = payload.get("type")
        if row.get("type") == "event_msg" and kind in ("error", "stream_error"):
            message = payload.get("message") or payload.get("error")
            if isinstance(message, str) and API_FAILURE.search(message):
                api_error = message.strip()
        # A task the service ended (a usage limit) completes with its error and no message.
        if row.get("type") == "event_msg" and kind == "task_complete" and isinstance(payload.get("error"), dict):
            message = payload["error"].get("message")
            api_error = message.strip() if isinstance(message, str) and message.strip() else api_error
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
                last_user, last_role, api_error = (row.get("timestamp"), value), "user", None
        elif kind == "message" and payload.get("role") == "assistant":
            value = message_text(payload)
            if value:
                last_assistant, last_assistant_at, last_role = value, row.get("timestamp"), "assistant"
                api_error = api_failure(value)
    pending = []
    for call_id, ask in asks.items():
        if call_id in outputs:
            continue
        later = bool(last_user and ask["asked_at"] and last_user[0] > ask["asked_at"])
        pending.append({**ask, "answered_in_chat_later": later})
    return cwd, {
        "rollout": str(path),
        "last_user": last_user[1] if last_user else None,
        "last_assistant": last_assistant,
        "last_assistant_at": last_assistant_at,
        "last_role": last_role,
        "api_error": api_error,
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
    return "claude", claude["final_text"]


def failure(row: dict) -> tuple[str, str | None] | None:
    """(error, agent) of the API failure that ended the last Claude or Codex turn, or None.

    Claude Code does not resume such a turn, and a monitor it left running keeps
    Orca at working, so only the transcript shows the agent stopped mid-task.
    A failure Codex's tab still shows can be an earlier turn's: it counts only
    while no turn runs. A failure of an agent another one Orca shows in its own
    turn took the worktree over from is history. Orca's copy of another Mac's
    message names no agent.
    """
    running = {a.get("type") for a in row.get("agents") or [] if a.get("state") == "working" and a.get("turn") != "done"}
    codex = row["codex"][-1] if row["codex"] else {}
    orca = " ".join(((row.get("orca_message") or {}).get("text") or "").split())
    found = [(None if running else row.get("codex_screen_error"), "codex"), (codex.get("api_error"), "codex"),
             ((row["claude"] or {}).get("api_error"), "claude"),
             (orca if not (row["claude"] or row["codex"]) and API_ENDED.match(orca) else None, None)]
    return next(((error, agent) for error, agent in found
                 if error and not (agent and running and agent not in running)), None)


def cut_off(row: dict) -> str | None:
    """The API error that ended the last Claude or Codex turn (failure), or None."""
    return (failure(row) or (None, None))[0]


# The line an API failure leaves as an agent's whole last output, read from Orca's copy when no transcript is here.
API_ENDED = re.compile(r"(?:API Error:|You['’]ve hit your [\w ]+ limit|Login expired|Not logged in|Please run /login|"
                       r"Failed to authenticate|(?:■\s*)?stream disconnected before completion)")
USAGE_LIMIT = re.compile(r"hit your [\w ]+ limit|usage limit", re.IGNORECASE)
LOGIN_EXPIRED = re.compile(r"Login expired|Please run /login|not logged in|Failed to authenticate", re.IGNORECASE)
# Without Claude Code's tag (Codex, Orca's copy), the text names an error a retry repeats: a safeguard flag, or a
# request the API refused (a 4xx other than a rate limit).
NOT_RETRYABLE = re.compile(r"safeguards flagged|invalid_request|API Error: 4(?!29)\d\d|prompt is too long|"
                           r"credit balance", re.IGNORECASE)
RESETS = re.compile(r"(?:resets|try again at)\s+(.+?)\.?$")


class StopReason(TypedDict):
    kind: str  # connection, usage_limit, login, or other
    action: str  # wake, wait (until resets), or human
    agent: str | None
    error: str
    resets: str | None


def stop_reason(row: dict) -> StopReason | None:
    """Why an API failure ended the last turn: a lost connection to wake from, a usage limit to wait out, an
    expired login a person has to renew, or another error a person reads (a safeguard flag, an invalid request),
    which waking would only repeat. Claude Code tags its error message (`server_error`, `rate_limit`,
    `authentication_failed`, `invalid_request`); Codex and Orca's copy are read by their text."""
    if not (found := failure(row)):
        return None
    error, agent = found
    tag = (row.get("claude") or {}).get("api_error_kind") if agent == "claude" else None
    if tag == "rate_limit" or USAGE_LIMIT.search(error):
        resets = RESETS.search(error)
        return {"kind": "usage_limit", "action": "wait", "agent": agent, "error": error,
                "resets": resets[1] if resets else None}
    if tag == "authentication_failed" or LOGIN_EXPIRED.search(error):
        return {"kind": "login", "action": "human", "agent": agent, "error": error, "resets": None}
    if tag not in (None, "server_error") or tag is None and NOT_RETRYABLE.search(error):
        return {"kind": "other", "action": "human", "agent": agent, "error": error, "resets": None}
    return {"kind": "connection", "action": "wake", "agent": agent, "error": error, "resets": None}


def stop_line(stop: StopReason) -> str:
    if stop["kind"] == "usage_limit":
        return "usage limit; resets " + stop["resets"] if stop["resets"] else "usage limit: " + stop["error"]
    if stop["kind"] == "login":
        return "login expired; a person has to log in again: " + stop["error"]
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
    if prompt := row.get("prompt_waiting"):
        kind = "permission prompt" if prompt["question"].startswith(PERMISSION_QUESTION) else "question"
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
    # An agent works past a decision it marked: the marker alone no longer waits on anyone.
    if reasons == [MARKER_REASON] and turn_runs(row):
        return "working", [OLD_MARKER_REASON]
    if reasons:
        return "waiting", reasons + mail
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
    # Another Mac's tab may show its agent only on its screen; this Mac's transcripts are read directly.
    screen_agent_tab = row.get("environment") and any(t["agent"] for t in row["terminals"])
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
CLAUDE_MODE_LINE = "⏵⏵ "
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
CODEX_ACTION = re.compile(r"• (?:(?:Edited|Added|Deleted) .+\(\+\d+ -\d+\)|Working \(.+\))\s*")
CODEX_RESULT = "└"
CODEX_RECAP = "↳ Recap:"
CODEX_PROMPT = "› "


def screen_lines(screen: dict) -> list[str]:
    tail = screen.get("tail") or []
    return tail if isinstance(tail, list) else tail.splitlines()


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


def screen_message(screen: dict) -> str:
    """The agent's last reply on the screen: Claude's last ⏺ block, its indented continuation lines unindented, or
    Codex's (codex_screen_message).

    A reply whose ⏺ line scrolled off the top is read from the indented lines above the last turn-end line.
    """
    lines = screen_lines(screen)
    if screen_agent(screen) == "codex":
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


def with_screen_agents(terms: list[dict], screens: Screens, environment: str | None) -> tuple[list[dict], str]:
    """(terms, message): the tabs, each without an agent given the one its screen shows, and the last
    Claude or Codex reply read on them. Another Mac's terminal list names a tab's agent on some reads only."""
    found, message = [], ""
    for term in terms:
        screen = rendered(screens.get((environment, term.get("handle"))))
        agent = screen_agent(screen) if screen else None
        if screen and agent:
            found.append(term if term.get("agentIdentity") else {**term, "agentIdentity": agent})
            message = screen_message(screen) or message
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


def build_inventory(worktrees: list[dict], terminals: list[dict], claude_root: str | Path,
                    codex_by_cwd: dict[str, list[dict]], decision_marker: str, now: float | None = None,
                    shell_idle_minutes: float = 30, judge: Callable[[str], dict | None] = lambda message: None,
                    mail: dict | None = None, screens: Screens | None = None,
                    live_transcripts: dict[str, list[Path]] | None = None,
                    processes: dict[str, list[WorktreeProcess]] | None = None) -> list[dict]:
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
        screen_said = ""
        if remote and not orca_said(w):
            terms, screen_said = with_screen_agents(terms, screens, w.get("_environment"))
        row = {
            "path": w["path"], "main": main, "repo": w.get("repo"), "branch": w.get("branch"),
            "host_id": w.get("hostId"), "environment": w.get("_environment"),
            "comment": w.get("comment"), "linked_pr": w.get("linkedPR"),
            "linked_issue": w.get("linkedIssue"), "linked_items": w.get("linkedItems") or [],
            "status": w.get("workspaceStatus"),
            "unread": bool(w.get("unread")),
            "agents": [{"type": a.get("agentType"), "state": a.get("state"), "turn": main_state(a)}
                       for a in w.get("agents") or []],
            "terminals": [{"handle": t.get("handle"), "agent": t.get("agentIdentity"), "title": t.get("title")}
                          for t in terms],
            "codex_screen_error": next((error for t in terms if t.get("agentIdentity") == "codex"
                                        if (error := api_failure(t.get("preview")))), None),
            "done_cards": done_cards(w.get("agents") or [], terms, screens, w.get("_environment")),
            "prompt_waiting": next((prompt for term in terms if term.get("handle")
                                    if (screen := rendered(screens.get((w.get("_environment"), term["handle"]))))
                                    if (prompt := permission_prompt(screen, term["handle"])
                                        or question_prompt(screen, term["handle"]))), None),
            "claude": None if remote else read_claude(claude_root, w["path"], (live_transcripts or {}).get(w["path"], [])),
            "codex": [] if remote else codex_by_cwd.get(w["path"], []),
        }
        row["queued_input"] = (queued_input(Path(row["claude"]["transcript"]),
                                            datetime.fromtimestamp(now, timezone.utc)) if row["claude"] else [])
        if row["prompt_waiting"] and "target" not in row["prompt_waiting"] and row["claude"]:
            if asked_at := row["claude"].get("pending_question_at"):
                row["prompt_waiting"]["asked_at"] = asked_at
        if remote:
            spoken = [a for a in w.get("agents") or [] if a.get("lastAssistantMessage")]
            if spoken:
                newest = max(spoken, key=lambda a: a.get("updatedAt") or 0)
                row["orca_message"] = {"text": newest["lastAssistantMessage"],
                                       "at": iso_time(newest.get("updatedAt"))}
            elif screen_said:
                row["orca_message"] = {"text": screen_said, "at": None, "from": "screen"}
        row["unread_mail"] = unread_since_turn(row, mail or {}, now)
        latest = None if turn_runs(row) or cut_off(row) else latest_message(row)
        row["jev"] = {"source": latest[0], **(judge(latest[1]) or NO_VERDICT)} if latest else None
        row["stop"] = stop_reason(row)
        row["class"], row["reasons"] = classify(row, decision_marker)
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
        row["meaningful_tabs"] = [t.get("handle") or "(no handle) " + (t.get("title") or "")
                                  for t in terms if t.get("handle") not in row["closable"]]
        rows.append(row)
    return rows


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
    if (row.get("jev") or {}).get("source") == "codex":
        return parse_time((row.get("codex") or [{}])[-1].get("last_assistant_at"))
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


def waited_items(waits_on: str, slug: str, names: set[str]) -> list[tuple[str | None, int]]:
    """The issues and PRs a verdict's waits_on names. `repo#N` is a repository of the row's organization:
    among `names`, the repositories known there, the one named so or the only one whose name ends in `-repo`
    (masters → taihei-coresys-masters), else None. A bare `#N` (or `PR #N`) belongs to the repository named
    before it, else the row's own."""
    org = slug.partition("/")[0]
    items: list[tuple[str | None, int]] = []
    last: str | None = slug
    for found in WAITED_ITEM.finditer(waits_on):
        repo = found["url"] or found["repo"]
        if repo and "/" in repo:
            last = repo
        elif repo and repo.lower() not in NOT_REPOS:
            ends = [name for name in names if name.lower().endswith("-" + repo.lower())]
            last = (f"{org}/{repo}" if repo in names else f"{org}/{ends[0]}" if len(ends) == 1 else None)
        items.append((last, int(found["url_number"] or found["number"] or 0)))
    return list(dict.fromkeys(items))


def add_next_step_facts(rows: list[dict], git_lookup: Callable[[str], dict | None] = git_state,
                        pr_lookup: PrLookup = gh_prs, verdicts: dict | None = None) -> list[dict]:
    """Add git and PR facts to rows needing review or completed rows with no current agent.

    `pr_lookup(slug, refs)` reads every PR one repository's rows need at once
    and returns a summary or None per ref; repositories are read in parallel.
    The issues and PRs each row's saved verdict waits on (`verdicts`) are read in the
    same queries; once every one is closed, the row's `waits_closed` names them and
    the verdict no longer holds (current_verdict), so the row is read again.
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
        git, waits = git_by_row[id(row)], waits_by_row.get(id(row), [])
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
        result.append(row)
    return result


QUEUE_FIELDS = ("path", "repo", "environment", "class", "reasons", "waits_on", "comment",
                "jev", "pr", "git", "claude", "orca_message", "prompt_waiting")
INBOX_FILE = "~/.cache/orca-briefs/triage-inbox.jsonl"
INBOX_KINDS = ("done", "ask")
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


def ask_answers(path: Path) -> dict[str, AnsweredAsk]:
    """The latest answer to each ask id, or `<ask id>:<n>` question, that inbox_read.py recorded."""
    try:
        records = load_jsonl(path)
    except (OSError, UnicodeDecodeError):
        return {}
    timed = sorted(((at, record) for record in records if isinstance(record.get("id"), str)
                    and (at := parse_time(record.get("ts"))) is not None), key=lambda item: item[0])
    return {record["id"]: {"at": record["ts"], "note": record.get("note") or ""} for _, record in timed}


def ask_answer(item: dict, answers: dict[str, AnsweredAsk]) -> AnsweredAsk | None:
    """The answer that settled an ask: its own, or the latest of its questions' once every one has one
    (inbox_read.open_asks's rule)."""
    questions = [f"{item['id']}:{number}" for number in range(1, len(inbox_read.questions_of(item)) + 1)]
    covering = ([item["id"]] if item["id"] in answers
                else questions if questions and all(question in answers for question in questions) else [])
    settled = [answers[key] for key in covering]
    return max(settled, key=lambda answer: parse_time(answer["at"]) or EPOCH) if settled else None


def inbox_reports(path: Path, answers_file: Path | None = None) -> InboxReports:
    """The newest `done` and `ask` each worktree sent to the triage inbox, by (repo, worktree name).

    An entry from before reports named their repository is keyed by ("", name).
    Each ask keeps its id and, once covered, its latest answer's time and note.
    """
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return {}
    answers = ask_answers(answers_file or inbox_read.answers_path().expanduser())
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


def with_inbox(rows: list[dict], reports: InboxReports) -> list[dict]:
    """Rows carry `inbox`: the newest done and ask their worktree reported, when it reported any."""
    def newest(row: dict) -> dict:
        name = Path(row["path"]).name
        found = [reports.get((row.get("repo") or "", name)) or {}, reports.get(("", name)) or {}]
        return {kind: max(held, key=lambda report: parse_time(report["at"]) or EPOCH)
                for kind in INBOX_KINDS if (held := [f[kind] for f in found if kind in f])}
    return [{**row, "inbox": inbox} if (inbox := newest(row)) else row for row in rows]


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
    """What a careful read saw: final message, PR, human activity, tree, inbox reports, permission prompt, and
    Orca comment. None without a message.

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
                       *([{"comment": row["comment"]}] if row.get("comment") else [])])


def row_identity(row: dict) -> str:
    """Keep equal paths on different Orca servers distinct in the verdict store."""
    return f"{row['environment']}::{row['path']}" if row.get("environment") else row["path"]


def pending_handoffs(row: dict, state_file: Path) -> list[dict]:
    """Undelivered promises addressed to this row, read again before closing its tabs."""
    if not state_file.exists():
        return []
    data = json.loads(state_file.read_text(encoding="utf-8"))
    names = {row_identity(row), row_name(row), row["path"],
             f"{row.get('repo')}/{Path(row['path']).name}"}
    if not row.get("environment"):
        names.add(Path(row["path"]).name)
    return [item for item in data.get("handoffs", {}).values()
            if item.get("delivered_at") is None and item.get("to") in names]


def with_cleanup_constraints(rows: list[dict], state_file: Path, decisions_file: Path) -> list[dict]:
    import lead_decisions
    decisions = lead_decisions.latest(lead_decisions.records(decisions_file))
    for row in rows:
        promises = pending_handoffs(row, state_file)
        if promises:
            row.update(pending_handoffs=promises, closable=[], open_pr_cleanup=False)
            row["reasons"] = [*row.get("reasons", []), "未配信の約束の送り先: " +
                              "; ".join(item.get("task") or item.get("condition") or "" for item in promises)]
            if row.get("class") == "finished":
                row["class"] = "stalled"
            if (row.get("verdict") or {}).get("class") == "finished":
                row.pop("verdict", None)
        keys = {f"remove:{row.get('repo')}/{Path(row['path']).name}",
                f"remove:{row['path']}", f"remove:{row_identity(row)}"}
        if not row.get("environment"):
            keys.add(f"remove:local::{row['path']}")
        answered = [decisions[key] for key in keys if key in decisions]
        if answered:
            answer = max(answered, key=lambda item: lead_decisions.moment(item.decided_at) or
                         datetime.min.replace(tzinfo=timezone.utc))
            row["removal_decision"] = {"decision": answer.decision, "answer": answer.answer,
                                       "decided_at": answer.decided_at}
    return rows


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
        if not (judged or row["class"] == "unsure" or (row["class"] == "stalled" and merged)):
            return False
        return current_verdict(row, verdicts) is None

    return [{**{key: row.get(key) for key in QUEUE_FIELDS},
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
    """Fold every session's classed queue (the store's sibling directories) into the store and return it."""
    store = Path(store_path).expanduser()
    verdicts = load_verdicts(store)
    for queue in sorted(store.parent.glob("*/review-queue.json"), key=lambda p: p.stat().st_mtime):
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


def screen_targets(worktrees: list[dict], terminals: list[dict]) -> list[dict]:
    """Tabs of done or waiting worktrees, agent tabs of worktrees Orca keeps no agent for (a background
    session's hooks do not reach it), remote worktrees with no agent message, and remote shells without
    output metadata: read their cards, permission prompts, questions, final replies, and bare prompts."""
    idle = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees
            if any(a.get("state") in ("done", "waiting") for a in w.get("agents") or [])}
    untracked = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees if not w.get("agents")}
    bare = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees
            if w.get("_environment") and not orca_said(w)}
    return [t for t in terminals if t.get("handle") and (
        (key := (t.get("_environment"), t.get("worktreeId"))) in idle or key in bare
        or (key in untracked and t.get("agentIdentity"))
        or (t.get("_environment") and not t.get("agentIdentity") and not t.get("preview")
            and t.get("lastOutputAt") is None))]


def read_screens(orca: str, terms: list[dict]) -> Screens:
    """Each tab's rendered screen by (environment, handle), read from the Orca server that issued the
    handle; a tab whose read fails is left out."""
    def read(term: dict) -> dict | None:
        where = ["--environment", term["_environment"]] if term.get("_environment") else []
        try:
            result = orca_payload(orca, ["terminal", "read", "--terminal", term["handle"], "--screen", *where])
            return result.get("terminal") or result
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
                            "meaningful tab, each led by the worktree's name and class as in --rows")
    views.add_argument("--row", metavar="NAME",
                       help="print one row as JSON with fixed keys: " + ", ".join(ROW_KEYS) + ". "
                            "Strings are never null, `final` is the newest "
                            "final message of Claude, Codex, or Orca, `transcripts` is "
                            '{"claude": [path…], "codex": [path…]}, and `github` is owner/repo from origin; '
                            "NAME is repo/name, the last path segment (with optional ENVIRONMENT:: on another "
                            "Mac), its path, or its row identity. Reviews are read from $TRIAGE_REVIEW_DIR "
                            "or " + REVIEW_DIR)
    views.add_argument("--rm-specs", action="store_true",
                       help="with --from, print <path> <PR|-> for this Mac's open_pr_cleanup rows and finished "
                            "rows without meaningful tabs, for rm-check-batch; another Mac's rows are counted on "
                            "stderr, and --summary prints their rm-check-batch --host commands")
    views.add_argument("--wake", action="store_true",
                       help="with --from, print one line per row an API failure stopped: name | its agent's tab | "
                            "what it needs (通信断: wake it, 利用上限: wait until it resets, ログイン切れ: a person "
                            "logs in, その他: a person reads it) | the error's first 200 characters")
    parser.add_argument("--transcripts", action="store_true",
                        help="with --row, print the row's transcript paths instead of JSON, one per line as "
                             "`claude <path>` or `codex <path>`")
    parser.add_argument("--from", dest="from_file", metavar="SCAN_JSON",
                        help="read the rows a previous scan printed as JSON instead of scanning again")
    parser.add_argument("--inbox", default=os.environ.get("TRIAGE_INBOX") or INBOX_FILE,
                        help="triage inbox whose newest done and ask per worktree go into the review queue "
                             "(default: $TRIAGE_INBOX or %(default)s)")
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
    counts = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    print("classes: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    queue = review_queue(rows, verdicts, force=force_review)
    print(f"review queue: {len(queue)}")
    for row in queue:
        print(f"  {row['repo']}/{Path(row['path']).name} [{row['class']}]")
    finished = [row for row in rows if ((row.get("verdict") or {}).get("class") or row["class"]) == "finished"]
    print(f"finished: {len(finished)} worktrees, {sum(len(row['terminals']) for row in finished)} tabs")
    for row in finished:
        print(f"  {row['repo']}/{Path(row['path']).name} ({len(row['terminals'])} tabs)")
        if not row.get("removal_decision"):
            print("    " + rm_check_command(row, scan_file))
    open_pr_cleanup = [row for row in rows if row.get("open_pr_cleanup")]
    print(f"open PR cleanup: {len(open_pr_cleanup)} worktrees")
    for row in open_pr_cleanup:
        print(f"  {row['repo']}/{Path(row['path']).name} [{row['class']}]")
        if not row.get("removal_decision"):
            print("    " + rm_check_command(row, scan_file, remove_ok=True))
    for row in rows:
        if answer := row.get("removal_decision"):
            print(f"removal decision: {row_name(row)} {answer['decided_at']} {answer['answer']}")
    hooks = [(row, error) for row in rows for error in row.get("hook_errors") or []]
    print(f"hook errors: {len(hooks)}")
    for row, error in hooks:
        print(f"  {error['host']} {error['path']} ({row_name(row)} {error.get('tab') or '-'})")


def row_name(row: dict) -> str:
    name = Path(row["path"]).name
    return f"{row['environment']}::{name}" if row.get("environment") else name


def select_row(rows: Sequence[dict], key: str, option: str = "--row") -> dict:
    """Resolve names shown by the views, with an exact identity taking precedence over aliases."""
    found = [row for row in rows if row_identity(row) == key]
    if not found:
        for row in rows:
            name = Path(row["path"]).name
            qualified = f"{row.get('repo') or ''}/{name}"
            aliases = (row["path"], name, qualified, row_name(row),
                       f"{row['environment']}::{qualified}" if row.get("environment") else qualified)
            if key in aliases:
                found.append(row)
    if len(found) == 1:
        return found[0]
    # With no match, the rows whose name or path contains the key, else every row.
    candidates = found or [row for row in rows if key.casefold() in f"{row_name(row)} {row['path']}".casefold()] or rows
    names = [row_name(row) for row in candidates]
    choices = [name if names.count(name) == 1 else row_identity(row)
               for row, name in zip(candidates, names)]
    raise ValueError(f"{option} {key}: " + ("matches multiple rows" if found else "no row has this name")
                     + "; candidates: " + (", ".join(choices) or "(none)"))


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
            "tabs", "done_cards", "closable", "meaningful_tabs", "open_pr_cleanup")


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
    prompt = prompt_question(row.get("prompt_waiting") or {})
    if (verdict.get("class") or row.get("class")) != "waiting":
        return prompt or str(verdict.get("question") or "")
    screen = row.get("orca_message") or {}
    return next((str(text) for text in (prompt, row.get("marker_question"),
                                        screen.get("text") if screen.get("from") == "screen" else "",
                                        verdict.get("question"), final_message(row)["text"]) if text), "")


def row_view(row: dict) -> dict:
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
        "prompt_waiting": row.get("prompt_waiting"), "queued_input": row.get("queued_input") or [],
        "processes": row.get("processes") or [],
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
    return (f"{view['name']} | {klass} | {where} | tabs {len(view['tabs'])} | {view['final']['at'] or '-'} | {note}"
            f" | reasons {reasons} | git.dirty {dirty} | git.unpushed {unpushed}")


CLOSE_TABS = Path(__file__).absolute().with_name("close_tabs.py")
RM_CHECK = Path(__file__).absolute().with_name("rm_check.py")
# Runs rm_check.py on another Mac over ssh; see references/cleanup.md.
RM_CHECK_BATCH = Path.home() / ".cache/orca-briefs/bin/rm-check-batch"


def pr_number(row: dict) -> int | None:
    return (row.get("pr") or {}).get("number") or (row.get("linked_pr") or {}).get("number")


def rm_check_command(row: dict, scan_file: Path | None = None, *, remove_ok: bool = False) -> str:
    """The rm_check.py command for a row; with the saved scan, its scanned_at bounds what may move after it.
    Another Mac's row is checked on that Mac through rm-check-batch --host."""
    pr = pr_number(row)
    if row.get("open_pr_cleanup") and remove_ok:
        return shlex.join([str(RM_CHECK_BATCH), *(["--host", row["environment"]] if row.get("environment") else []),
                           *(["--from", str(scan_file)] if scan_file else []),
                           "--detach-nested", "--archive-scratch", "--remove-ok", f"{row['path']} {pr or '-'}"])
    if row.get("environment"):
        return shlex.join([str(RM_CHECK_BATCH), "--host", row["environment"], f"{row['path']} {pr or '-'}"])
    return shlex.join([str(RM_CHECK), "--worktree", row["path"], *(["--pr", str(pr)] if pr else []),
                       *(["--from", str(scan_file)] if scan_file else [])])


def card_lines(row: dict, scan_file: Path | None = None) -> list[str]:
    """A line per done card, closable tab, and meaningful tab; a scan saved before cards had screens reads as unknown.

    With the saved scan's path, a row with closable tabs includes the close_tabs.py command that closes them
    once nothing moved after the scan. Finished and open_pr_cleanup rows include their rm_check.py command.
    """
    lead = f"{row_name(row)} | {class_label((row.get('verdict') or {}).get('class') or row['class'], row['class'])}"
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
        lines.append(f"{lead} | {who} | draft {draft} | composer {composer} | {excerpt} | screen: "
                     + (" ⏎ ".join(card.get("screen_tail") or []) or "-"))
    lines.extend(f"{lead} | closable {handle}" for handle in row.get("closable") or [])
    lines.extend(f"{lead} | meaningful {handle}" for handle in row.get("meaningful_tabs") or [])
    if scan_file and row.get("closable"):
        lines.append(f"{lead} | close: " + shlex.join([str(CLOSE_TABS), "--from", str(scan_file),
                                                      *row["closable"]]))
    if row.get("open_pr_cleanup") or ((row.get("verdict") or {}).get("class") or row["class"]) == "finished":
        lines.append(f"{lead} | rm-check: " + rm_check_command(row, scan_file))
    return lines


def saved_rows(path: str) -> list[dict]:
    rows = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(row, dict) and "path" in row for row in rows):
        raise ValueError(f"{path} is not the JSON list a scan prints")
    return rows


def print_row(rows: list[dict], key: str, transcripts: bool = False) -> int:
    try:
        row = select_row(rows, key)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    view = row_view(row)
    if transcripts:
        for agent, paths in view["transcripts"].items():
            for path in paths:
                print(f"{agent} {path}")
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
        choices = [{"path": w["path"], "repo": w.get("repo"), "environment": w.get("_environment")}
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
    screens = {} if args.ps_json else read_screens(args.orca, screen_targets(worktrees, terminals))
    claude_root = str(Path(args.claude_projects).expanduser())
    processes = {} if args.ps_json else worktree_processes(
        [w["path"] for w in worktrees if not w.get("_environment")], Path(PROCESS_CPU).expanduser(), time.time())
    local = {w.get("worktreeId"): w["path"] for w in worktrees if not w.get("_environment")}
    live = {} if args.ps_json else running_claude_transcripts(
        list(local.values()), claude_session_dirs(), claude_root, process_table(), process_cwds,
        tabs={t["handle"]: local[t.get("worktreeId")] for t in terminals
              if t.get("handle") and not t.get("_environment") and t.get("worktreeId") in local})
    verdicts = absorb_verdicts(args.verdicts, write=bool(args.review_out))
    rows = add_next_step_facts(build_inventory(worktrees, terminals, claude_root,
                                               codex, args.decision_marker,
                                               shell_idle_minutes=args.shell_idle_minutes, judge=judge,
                                               mail=read_mail(args), screens=screens, live_transcripts=live,
                                               processes=processes),
                               pr_lookup=partial(gh_prs, cache_dir=Path(args.pr_cache).expanduser(), reach=reach),
                               verdicts=verdicts)
    if down := reach.down():
        line = f"ネットワーク断: {'。'.join(down)}。接続が戻ってから走査し直してください（--review-out は書いていません）"
        print(line)
        print(line, file=sys.stderr)
        return NETWORK_DOWN_EXIT
    rows = [{**row, "scanned_at": scanned_at}
            for row in with_inbox(rows, inbox_reports(Path(args.inbox).expanduser(), Path(args.ask_answers).expanduser()))]
    rows = with_verdicts(rows, verdicts)
    rows = with_cleanup_constraints(rows, args.state_file.expanduser(), args.decisions_file.expanduser())
    if args.review_out:
        write_review_queue(args.review_out, rows, verdicts, force=bool(args.only))
    return print_rows(args, rows, verdicts)


WAKE_ACTIONS = {"connection": "通信断: 起こす", "login": "ログイン切れ: 人が /login する", "other": "その他: 人が読む"}


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


def wake_line(row: dict, stop: StopReason) -> str:
    """name | the stopped agent's tab, or its candidates when several agent tabs could be it | what it needs |
    the error."""
    tabs = [t["handle"] for t in row.get("terminals") or [] if t.get("handle") and t.get("agent")
            and (stop["agent"] is None or t["agent"] == stop["agent"])]
    tab = tabs[0] if len(tabs) == 1 else f"不明（{', '.join(tabs)}）" if tabs else None
    action = (f"利用上限: {stop['resets'] or '戻る時刻は不明'} まで待つ" if stop["kind"] == "usage_limit"
              else WAKE_ACTIONS[stop["kind"]])
    if stop["kind"] == "usage_limit":
        now = datetime.fromtimestamp(time.time(), timezone.utc)
        resets = reset_time(stop["resets"], now, parse_time(row_final_at(row)))
        if resets and resets <= now:
            action = "利用上限: 解除済み・起こせる" + ("（タブの開き直しが必要）" if not tabs else "")
    error = " ".join(stop["error"].split())
    return f"{row_name(row)} | {tab or '-'} | {action} | {error[:200]}{'…' if len(error) > 200 else ''}"


def print_rows(args: argparse.Namespace, rows: list[dict], verdicts: dict) -> int:
    if args.row:
        return print_row(rows, args.row, args.transcripts)
    if args.rows:
        if args.sort == "final":
            rows = sorted(rows, key=lambda row: parse_time(row_final_at(row)) or
                          datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for row in rows:
            if not args.classes or ((row.get("verdict") or {}).get("class") or row["class"]) in args.classes:
                print(row_line(row_view(row)))
    elif args.rm_specs:
        remote = 0
        for row in rows:
            finished = ((row.get("verdict") or {}).get("class") or row["class"]) == "finished"
            if answer := row.get("removal_decision"):
                print(f"{row_name(row)}: {answer['decided_at']} {answer['answer']}", file=sys.stderr)
                continue
            if row.get("open_pr_cleanup") or finished and not row.get("meaningful_tabs"):
                if row.get("environment"):
                    remote += 1
                else:
                    print(f"{row['path']} {pr_number(row) or '-'}")
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
