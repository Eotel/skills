#!/usr/bin/env python3
"""Inventory Orca agent sessions from Orca state and agent transcripts.

GitHub calls per run, for the rows that get PR facts: one GraphQL query per
repository (25 PRs a query), plus three REST reads (reviews, conversation
comments, line comments) for each PR whose updatedAt changed since the copy
kept in --pr-cache.
"""

import argparse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
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
from typing import Any
import urllib.request
from urllib.parse import urlsplit

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


def claude_project_dir(projects_root, worktree_path):
    """Claude Code stores a cwd's transcripts under the path with non-alphanumerics as '-'.

    Names longer than 200 characters are cut to 200 and given a hash suffix; see
    find_claude_dir for locating those.
    """
    return Path(projects_root) / re.sub(r"[^A-Za-z0-9]", "-", worktree_path)


def records_cwd(directory, worktree_path):
    for path in directory.glob("*.jsonl"):
        for row in load_jsonl(path):
            if "cwd" in row:
                if row["cwd"] == worktree_path:
                    return True
                break
    return False


def find_claude_dir(projects_root, worktree_path):
    exact = claude_project_dir(projects_root, worktree_path)
    if exact.is_dir() or len(exact.name) <= CLAUDE_DIR_LIMIT or not Path(projects_root).is_dir():
        return exact
    prefix = exact.name[:CLAUDE_DIR_LIMIT]
    for directory in Path(projects_root).iterdir():
        if directory.name.startswith(prefix) and records_cwd(directory, worktree_path):
            return directory
    return exact


def load_jsonl(path):
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


def message_of(entry):
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def user_text(entry):
    content = message_of(entry).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def is_real_user_message(entry):
    """A message the user typed; meta entries and compaction summaries are Claude Code's own."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return False
    value = user_text(entry)
    return bool(value) and not value.lstrip().startswith("<")


def read_claude(projects_root, worktree_path):
    """Summarize the newest Claude transcript for a worktree, or None when there is none."""
    candidates = list(find_claude_dir(projects_root, worktree_path).glob("*.jsonl"))
    if not candidates:
        return None
    path = max(candidates, key=lambda p: p.stat().st_mtime)
    rows = load_jsonl(path)
    last = max((i for i, e in enumerate(rows) if is_real_user_message(e)), default=-1)
    texts, final_at, asked, answered, api_error = [], None, {}, set(), None
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
            elif kind == "tool_use" and block.get("name") == "AskUserQuestion":
                asked[block.get("id")] = block.get("input")
            elif kind == "tool_result":
                answered.add(block.get("tool_use_id"))
    return {
        "transcript": str(path),
        "last_user": user_text(rows[last]) if last >= 0 else None,
        "last_user_at": rows[last].get("timestamp") if last >= 0 else None,
        "final_text": texts[-1] if texts else None,
        "final_at": final_at,
        "pending_questions": [q for i, q in asked.items() if i not in answered],
        "api_error": api_error,
    }


def message_text(payload):
    return " ".join(c.get("text", "") for c in payload.get("content") or [] if isinstance(c, dict))


API_FAILURE = re.compile(
    r"(?:Selected model is at capacity|API Error:|rate limit(?:ed| exceeded)?|"
    r"request failed|connection error|stream error|server error)", re.IGNORECASE)


def api_failure(text):
    """Return a Codex API failure line, ignoring ordinary discussion of an error."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if (line.startswith("■ ") or line.startswith("API Error:")) and API_FAILURE.search(line):
            return line
    return None


def summarize_rollout(path):
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
                last_user, last_role = (row.get("timestamp"), value), "user"
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


def jev_key(env, key_file):
    """TYPESAFE_API_KEY from the environment, else the key file's content, else None."""
    if env.get("TYPESAFE_API_KEY"):
        return env["TYPESAFE_API_KEY"]
    try:
        return Path(key_file).expanduser().read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def probability(value):
    value = float(value)
    if not 0 <= value <= 1:  # also false for NaN
        raise ValueError(f"probability out of range: {value}")
    return value


def jev_judge(api_key, opener=None):
    """A judge returning Jev's verdict on a message, or None on failure.

    The verdict holds `asks` (probability that it waits on the user), `status`
    (in_progress, blocked_on_others, or done), and that status's `confidence`.
    """
    def judge(message):
        body = {"state": message[-JEV_MAX_CHARS:], "model": "jev-latest", "questions": JEV_QUESTIONS}
        request = urllib.request.Request(JEV_URL, data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        try:
            with (opener or urllib.request.urlopen)(request, timeout=JEV_TIMEOUT) as response:
                answers = json.load(response)["answers"]
            status = answers["status"]["choice"]
            if status not in JEV_QUESTIONS["status"]["criteria"]:
                raise ValueError(f"unknown status: {status!r}")
            return {"asks": probability(answers["asks_user"]["noul"]), "status": status,
                    "confidence": probability(answers["status"]["confidence"])}
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
            print(f"Jev could not judge a final message: {error!r}", file=sys.stderr)
            return None
    return judge


def is_working(row):
    return any(a["state"] == "working" for a in row["agents"])


def main_state(agent):
    main = agent.get("mainAgent")
    return main.get("state") if isinstance(main, dict) else None


def turn_runs(row):
    """An agent is in its own turn, not only holding a background shell, monitor, or helper.

    Orca keeps `state` at working while such a job runs after the turn ended;
    `mainAgent.state` then says done.
    """
    return any(a["state"] == "working" and a.get("turn") != "done" for a in row["agents"])


def latest_message(row):
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


def cut_off(row):
    """The API error that ended the last Claude or Codex turn, or None.

    Claude Code does not resume such a turn, and a monitor it left running keeps
    Orca at working, so only the transcript shows the agent stopped mid-task.
    """
    codex = row["codex"][-1] if row["codex"] else {}
    return (row.get("codex_screen_error") or codex.get("api_error")
            or (row["claude"] or {}).get("api_error"))


def is_sure_ask(asks):
    return asks is not None and asks >= ASKS_FROM


def unsure_reason(jev):
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


def waiting_reasons(row, decision_marker):
    reasons = []
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
        reasons.append("decision marker in comment")
    return reasons


NO_SESSION_REASON = "no agent session in this worktree"


def classify(row, decision_marker):
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
    named on a waiting row.
    """
    reasons = waiting_reasons(row, decision_marker)
    mail = [mail_reason(row["unread_mail"])] if row.get("unread_mail") else []
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
    if cut_off(row):
        return "stalled", ["turn ended with an API error: " + cut_off(row)]
    if mail:
        return "stalled", mail
    if is_working(row):
        if jev and (jev["asks"] is None or jev["asks"] >= SETTLED_BELOW):
            return "unsure", [unsure_reason(jev)]
        return "working", [BACKGROUND_REASON]
    if jev and unsure_reason(jev):
        return "unsure", [unsure_reason(jev)]
    state = f"{STATE_REASONS[jev['status']]} (Jev {jev['confidence']:.2f})" if jev else None
    if jev and jev["status"] != "done":
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


def shell_is_idle(term, now, idle_minutes):
    """A shell whose screen ends at a bare prompt and that printed nothing for idle_minutes."""
    lines = [line.strip() for line in (term.get("preview") or "").splitlines() if line.strip()]
    last_output = term.get("lastOutputAt")
    quiet = last_output is not None and now - last_output / 1000 >= idle_minutes * 60
    return bool(lines) and lines[-1] in BARE_PROMPTS and quiet


def agents_done(row):
    """Nothing is pending on the agents: closing their tabs loses no work.

    An agent whose last message says work continues may be waiting on a
    background job that its tab still holds. A turn an API error cut off, and a
    lead with unread worker mail, are woken in that tab.
    """
    still_working = (row.get("jev") or {}).get("status") == "in_progress"
    to_wake = cut_off(row) or row.get("unread_mail")
    return row["class"] in ("finished", "stalled") and not still_working and not to_wake


def closable_terminals(agents_closable, terms, now, idle_minutes):
    """Handles to close in a kept worktree: idle shells, plus agents once nothing is pending."""
    return [t["handle"] for t in terms if t.get("handle")
            and (agents_closable if t.get("agentIdentity") else shell_is_idle(t, now, idle_minutes))]


def iso_time(epoch_ms):
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


def screen_lines(screen: dict) -> list[str]:
    tail = screen.get("tail") or []
    return tail if isinstance(tail, list) else tail.splitlines()


def rendered(screen: dict | None) -> dict | None:
    """The screen when Orca rendered it; None when the read failed or gave accumulated output."""
    return screen if screen and screen.get("source") == "screen" else None


def has_draft(screen: dict) -> bool:
    """Whether the composer holds unsent text; Orca reports it as `draft`, apart from the screen's tail."""
    return screen.get("draft") not in (None, "")


def screen_tail(screen: dict) -> list[str]:
    """The last lines a rendered screen shows, without blank lines and rules."""
    shown = [line.strip() for line in screen_lines(screen) if line.strip() and not RULE.fullmatch(line.strip())]
    return shown[-SCREEN_TAIL_LINES:]


def screen_agent(screen: dict) -> str | None:
    """"claude" when the screen shows Claude Code: its reply bullet, its turn-end line, or its mode line."""
    shows = any(CLAUDE_LINE_STARTS.match(line.strip()) or CLAUDE_MODE_LINE in line for line in screen_lines(screen))
    return "claude" if shows else None


def screen_message(screen: dict) -> str:
    """Claude's last reply on the screen: the last ⏺ block, its indented continuation lines unindented.

    A reply whose ⏺ line scrolled off the top is read from the indented lines above the last turn-end line.
    """
    lines = screen_lines(screen)
    def tool_call(index):
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        return (TOOL_CALL.match(lines[index]) or lines[index].rstrip().endswith(COLLAPSED_CALLS)
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


def done_cards(agents, terms, screens=None, environment=None):
    """One card per agent Orca shows as done, with the live tab that closes it.

    A tab Orca reopened carries a new pane id, so a card whose pane is gone
    takes the worktree's only unclaimed tab of its agent. `handle` is None
    while the tab sleeps, or when several tabs could be its own.
    `last_message` is what Orca kept of the agent's last word and can be empty;
    the row's transcripts hold the rest. `screen_tail` is the end of the tab's
    rendered screen (`screens` maps (environment, handle) to it) and `draft`
    whether its composer holds unsent text; [] and None when it was not read.
    """
    screens = screens or {}
    by_pane = {f"{t.get('tabId')}:{t.get('leafId')}": t.get("handle") for t in terms}
    panes = {a.get("paneKey") for a in agents}
    unclaimed = {}
    for t in terms:
        if t.get("handle") and f"{t.get('tabId')}:{t.get('leafId')}" not in panes:
            unclaimed.setdefault(t.get("agentIdentity"), []).append(t["handle"])

    def handle_of(agent):
        own = by_pane.get(agent.get("paneKey"))
        spare = unclaimed.get(agent.get("agentType"), [])
        alone = sum(1 for a in agents if a.get("agentType") == agent.get("agentType")
                    and by_pane.get(a.get("paneKey")) is None) == 1
        return own or (spare[0] if len(spare) == 1 and alone else None)

    def card(agent):
        handle = handle_of(agent)
        screen = rendered(screens.get((environment, handle))) if handle else None
        return {
            "agent": agent.get("agentType"), "handle": handle,
            "done_at": iso_time(agent.get("stateStartedAt")), "interrupted": bool(agent.get("interrupted")),
            "prompt": agent.get("prompt") or "", "last_message": agent.get("lastAssistantMessage") or "",
            "screen_tail": screen_tail(screen) if screen else [], "draft": has_draft(screen) if screen else None,
        }

    return [card(a) for a in agents if a.get("state") == "done"]


# A hook Claude Code could not run because its file is gone: "PreToolUse:Bash hook error" with the shell's
# message on the next line, or "Stop hook error: Failed with ...: /bin/sh: PATH: No such file or directory".
HOOK_MISSING = re.compile(r"hook error\b[^\n]*?(?:\n[^\n]*?)??\s(/[^\s:]+): No such file or directory",
                          re.IGNORECASE)
# This Mac's name, as triage-notify records it.
LOCAL_HOST = socket.gethostname()


def hook_errors(terms, screens, environment):
    """Each missing hook file a tab's output or rendered screen shows, once per tab: {host, path, tab}."""
    host = environment or LOCAL_HOST
    shown = ((term.get("handle"), text) for term in terms
             for text in (term.get("preview") or "",
                          "\n".join(screen_lines(rendered(screens.get((environment, term.get("handle")))) or {}))))
    found = {(path, handle): {"host": host, "path": path, "tab": handle}
             for handle, text in shown for path in HOOK_MISSING.findall(text)}
    return list(found.values())


def orca_said(worktree):
    """Whether Orca keeps a last message of one of the worktree's agents."""
    return any(agent.get("lastAssistantMessage") for agent in worktree.get("agents") or [])


def with_screen_agents(terms, screens, environment):
    """(terms, message): the tabs, each without an agent given the one its screen shows, and the last
    Claude reply read on them. Another Mac's terminal list names a tab's agent on some reads only."""
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


def build_inventory(worktrees, terminals, claude_root, codex_by_cwd, decision_marker,
                    now=None, shell_idle_minutes=30, judge=lambda message: None, mail=None, screens=None):
    """Join Orca worktrees with their terminals and transcripts and classify each one.

    `judge(message)` returns Jev's verdict on the newest final message, a dict of
    `asks`, `status`, and `confidence` (see jev_judge), or None when it failed.
    `mail` maps a lead's terminal handle to its unread worker mail (see
    unread_worker_mail). `screens` maps (environment, handle) to a tab's
    rendered screen (see read_screens): done cards show its end, and another
    Mac's worktree whose agents Orca keeps no message of takes the agent and
    last reply its tabs show.
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
            "claude": None if remote else read_claude(claude_root, w["path"]),
            "codex": [] if remote else codex_by_cwd.get(w["path"], []),
        }
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
        row["class"], row["reasons"] = classify(row, decision_marker)
        row["hook_errors"] = hook_errors(terms, screens, w.get("_environment"))
        # Quoted as the screen shows it: a tab that has not run since keeps an old error on its screen.
        row["reasons"] = [*row["reasons"], *dict.fromkeys(
            f"hook error on {error['host']}: {error['path']}: No such file or directory" for error in row["hook_errors"])]
        row["closable"] = closable_terminals(agents_done(row), terms, now, shell_idle_minutes)
        row["meaningful_tabs"] = [t.get("handle") or "(no handle) " + (t.get("title") or "")
                                  for t in terms if t.get("handle") not in row["closable"]]
        rows.append(row)
    return rows


def github_slug(remote_url):
    """owner/repo of a github.com remote; None for any other host."""
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote_url or "")
    return match.group(1) if match else None


PR_URL = re.compile(r"https://github\.com/([^/\s)]+/[^/\s)]+)/pull/(\d+)")


def remote_git_hint(row):
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


def git_state(path):
    """Branch, uncommitted paths, commits not pushed to the upstream, and the GitHub repo.

    None outside a git checkout; `unpushed` is None without an upstream.
    """
    def run(*args):
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=30)

    try:
        status = run("status", "--porcelain")
        if status.returncode != 0:
            return None
        ahead = run("rev-list", "--count", "@{u}..HEAD")
        branch = run("symbolic-ref", "-q", "--short", "HEAD").stdout.strip() or None
        head = run("rev-parse", "HEAD").stdout.strip() or None
        origin = run("remote", "get-url", "origin").stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {
        "branch": branch, "head": head,
        "dirty": len(status.stdout.splitlines()),
        "unpushed": int(ahead.stdout) if ahead.returncode == 0 else None,
        "github": github_slug(origin),
    }


def latest_human_activity(author, reviews=(), comments=(), line_comments=()):
    """Newest non-author human PR activity from the three GitHub comment surfaces."""
    candidates = []
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
    return max(candidates, key=lambda item: item["at"], default=None)


def summarize_pr(data, reviews=(), comments=(), line_comments=()):
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
    reply_after_commit = bool(activity and last_commit_at and parse_time(activity["at"])
                              and parse_time(last_commit_at)
                              and parse_time(activity["at"]) > parse_time(last_commit_at))
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


PrRef = int | str  # a PR number, or the head branch to find the PR by
PrLookup = Callable[[str, list[PrRef]], dict[PrRef, dict | None]]

GH_TIMEOUT = 15
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
    if isinstance(ref, int):
        return ref > 0
    return isinstance(ref, str) and bool(ref) and not NOT_IN_BRANCH.search(ref)


def pr_query(owner: str, name: str, refs: list[PrRef]) -> tuple[str, dict[str, PrRef]]:
    """One GraphQL query for these PRs of a repository, and the ref each alias answers."""
    aliases: dict[str, PrRef] = {}
    fields = ["defaultBranchRef { name }"]
    for index, ref in enumerate(refs):
        if isinstance(ref, int):
            alias = f"pr_{ref}"
            fields.append(f"{alias}: pullRequest(number: {ref}) {{ ...PrFacts }}")
        else:
            alias = f"branch_{index}"
            fields.append(f"{alias}: pullRequests(headRefName: {json.dumps(ref, ensure_ascii=False)}, "
                          f"first: {BRANCH_CANDIDATES}, orderBy: {{field: CREATED_AT, direction: DESC}}) "
                          "{ nodes { ...PrFacts } }")
        aliases[alias] = ref
    return (f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ "
            + " ".join(fields) + " } }" + PR_FACTS), aliases


def graphql_repository(slug: str, query: str) -> dict | None:
    """The `repository` object a query returned, or None when gh gave no data.

    A PR that does not exist comes back as a null alias with a NOT_FOUND error,
    and gh exits 1 while the other aliases still hold data, so stdout is read
    whatever the exit code.
    """
    try:
        out = subprocess.run(["gh", "api", "graphql", "-f", "query=" + query],
                             capture_output=True, text=True, timeout=GH_TIMEOUT)
        repository = (json.loads(out.stdout).get("data") or {}).get("repository")
        failure = out.stderr.strip() or out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as error:
        repository, failure = None, repr(error)
    if isinstance(repository, dict):
        return repository
    print(f"GitHub query for {slug} failed, so its rows have no PR facts: {failure}", file=sys.stderr)
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


def gh_prs(slug: str, refs: list[PrRef],
           cache_dir: Path = Path(PR_ACTIVITY_DIR).expanduser()) -> dict[PrRef, dict | None]:
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
        repository = graphql_repository(slug, query) or {}
        default_branch = (repository.get("defaultBranchRef") or {}).get("name")
        for alias, ref in aliases.items():
            answer = repository.get(alias) or {}
            node = (pr_for_branch(answer.get("nodes") or [], ref, default_branch)
                    if isinstance(ref, str) else answer)
            found[ref] = node if isinstance(node, dict) and isinstance(node.get("number"), int) else None
    nodes = {node["number"]: node for node in found.values() if node}
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(nodes)))) as pool:
        activity = dict(zip(nodes, pool.map(lambda node: pr_activity(slug, node, cache_dir), nodes.values())))
    return {ref: summarize_pr(view_fields(node), *activity[node["number"]]) if node else None
            for ref, node in found.items()}


def parse_time(value):
    """An aware datetime; a time without a zone is UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


WORKER_MAIL = ("worker_done", "question", "escalation")
MAIL_GRACE_MINUTES = 5  # Orca types its own notice into an idle lead first


def unread_worker_mail(messages, runs):
    """Unread worker reports, questions, and escalations, keyed by the lead terminal they wait on."""
    leads = {run.get("id"): run.get("coordinator_handle") for run in runs}
    mail = {}
    for m in messages:
        run = m.get("run_id")
        if m.get("read") or m.get("type") not in WORKER_MAIL or m.get("to_handle") != f"run:{run}":
            continue
        if leads.get(run):
            mail.setdefault(leads[run], []).append(
                {"type": m["type"], "created_at": m.get("created_at"), "subject": m.get("subject")})
    return mail


def unread_since_turn(row, mail, now, grace_minutes=MAIL_GRACE_MINUTES):
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


def mail_reason(unread):
    noun = "message" if len(unread) == 1 else "messages"
    return f"{len(unread)} orchestration {noun} unread since its last turn began (oldest {unread[0]['created_at']})"


def final_message_at(row):
    """When the final message Jev read was written, or None."""
    if (row.get("jev") or {}).get("source") == "codex":
        return parse_time((row.get("codex") or [{}])[-1].get("last_assistant_at"))
    return parse_time((row.get("claude") or {}).get("final_at"))


def merged_after_final_message(row, pr):
    """The worktree's own PR merged after a final message that waited on someone else.

    That message was written before the merge it was waiting for, often a
    review or the user's own merge on GitHub, so the wait it reported is over.
    """
    if not (row["class"] == "stalled" and (row.get("jev") or {}).get("status") == "blocked_on_others"
            and pr.get("state") == "MERGED" and pr.get("head_matches")):
        return False
    said_at, merged_at = final_message_at(row), parse_time(pr.get("merged_at"))
    return said_at is not None and merged_at is not None and merged_at > said_at


def merged_with_nothing_left(row, pr):
    """A checkout no agent ran in whose own PR merged at HEAD.

    Orca's PR link is often missing or still says open after the merge, so the
    PR found by the lookup decides. A row an agent spoke in is not finished
    here even when Jev read it as done: Jev's read is fast and shallow, so that
    row waits in the review queue for a careful one.
    """
    return (row["class"] == "stalled" and row.get("reasons") == [NO_SESSION_REASON]
            and pr.get("state") == "MERGED" and bool(pr.get("head_matches")))


def open_pr_with_no_local_work(row, pr, git):
    """No current agent or local work remains beyond the published PR head.

    `git.unpushed` is relative to the branch's upstream, which can be the PR's
    base. An exact PR head match proves this HEAD was pushed to the PR.
    """
    return (not row.get("agents")
            and not any(term.get("agent") for term in row.get("terminals", []))
            and pr.get("state") == "OPEN" and pr.get("head_matches") is True
            and git.get("dirty") == 0)


def add_next_step_facts(rows: list[dict], git_lookup: Callable[[str], dict | None] = git_state,
                        pr_lookup: PrLookup = gh_prs) -> list[dict]:
    """Add git and PR facts to rows needing review or completed rows with no current agent.

    `pr_lookup(slug, refs)` reads every PR one repository's rows need at once
    and returns a summary or None per ref; repositories are read in parallel.
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
        git = git_by_row[id(row)]
        ref = (row["linked_pr"] or {}).get("number") or (git or {}).get("pr_number") or (git or {}).get("branch")
        pr = prs.get((git["github"], ref)) if git and git.get("github") and ref else None
        if pr is not None and git is not None:
            pr = {**pr, "head_matches": pr.get("head_oid") == git.get("head") if git.get("head") else None}
        row: dict[str, Any] = {**row, "git": git, "pr": pr}
        open_pr_cleanup = pr is not None and git is not None and open_pr_with_no_local_work(row, pr, git)
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
        result.append(row)
    return result


QUEUE_FIELDS = ("path", "repo", "environment", "class", "reasons", "waits_on", "comment",
                "jev", "pr", "git", "claude", "orca_message")
INBOX_FILE = "~/.cache/orca-briefs/triage-inbox.jsonl"
INBOX_KINDS = ("done", "ask")
VERDICTS_FILE = "~/.cache/orca-session-triage/verdicts.json"
VERDICT_CLASSES = ("waiting", "blocked", "in_progress", "done_unmerged", "finished")
READ_CLASSES = ("unsure", "stalled", "waiting")


def row_final_at(row):
    """When the newest final message a reader would class was written (Claude or Codex), or None."""
    codex = row.get("codex") or []
    said = [at for at in ((row.get("claude") or {}).get("final_at"),
                          codex[-1].get("last_assistant_at") if codex else None,
                          (row.get("orca_message") or {}).get("at")) if at]
    return max(said, key=lambda at: parse_time(at) or datetime.min.replace(tzinfo=timezone.utc), default=None)


def inbox_reports(path: Path) -> dict[tuple[str, str], dict[str, dict]]:
    """The newest `done` and `ask` each worktree sent to the triage inbox, by (repo, worktree name).

    An entry from before reports named their repository is keyed by ("", name).
    """
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return {}
    reports: dict[tuple[str, str], dict[str, dict]] = {}
    for raw in lines:
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if (not isinstance(item, dict) or item.get("kind") not in INBOX_KINDS or not item.get("worktree")
                or parse_time(item.get("ts")) is None):
            continue
        held = reports.setdefault((item.get("repo") or "", item["worktree"]), {})
        older = held.get(item["kind"])
        if older is None or parse_time(item["ts"]) >= parse_time(older["at"]):
            held[item["kind"]] = {"at": item["ts"], "host": item.get("host"),
                                  "text": (item.get("text") or "")[-JEV_MAX_CHARS:]}
    return reports


def with_inbox(rows: list[dict], reports: dict[tuple[str, str], dict[str, dict]]) -> list[dict]:
    """Rows carry `inbox`: the newest done and ask their worktree reported, when it reported any."""
    def newest(row: dict) -> dict:
        name = Path(row["path"]).name
        found = [reports.get((row.get("repo") or "", name)) or {}, reports.get(("", name)) or {}]
        return {kind: max(held, key=lambda report: parse_time(report["at"]))
                for kind in INBOX_KINDS if (held := [f[kind] for f in found if kind in f])}
    return [{**row, "inbox": inbox} if (inbox := newest(row)) else row for row in rows]


def newest_report(row: dict) -> dict | None:
    """Which came last of the final message and the inbox reports; the Orca comment has no time."""
    said = [("final_message", row_final_at(row)),
            *((f"inbox_{kind}", report["at"]) for kind, report in (row.get("inbox") or {}).items())]
    timed = [(source, at) for source, at in said if at and parse_time(at)]
    if not timed:
        return None
    source, at = max(timed, key=lambda pair: parse_time(pair[1]))
    return {"source": source, "at": at}


def read_key(row):
    """What a careful read saw: final message, PR, latest human review, tree, and inbox reports.
    None without a message.

    A verdict holds only while this key is unchanged.
    """
    final_at = row_final_at(row)
    if final_at is None:
        return None
    pr, git = row.get("pr") or {}, row.get("git") or {}
    inbox = [report["at"] for _, report in sorted((row.get("inbox") or {}).items())]
    return json.dumps([final_at, pr.get("state"), pr.get("number"), pr.get("head_oid"),
                       pr.get("latest_human_review"), pr.get("latest_human_activity"), git.get("dirty"),
                       *([inbox] if inbox else [])])


def row_identity(row):
    """Keep equal paths on different Orca servers distinct in the verdict store."""
    return f"{row['environment']}::{row['path']}" if row.get("environment") else row["path"]


def current_verdict(row, verdicts):
    """The stored verdict for what the row shows now, or None."""
    key = read_key(row)
    verdict = verdicts.get(row_identity(row)) if key is not None else None
    return verdict if isinstance(verdict, dict) and verdict.get("read") == key else None


def review_queue(rows, verdicts=None):
    """Rows a careful reader must class before anything closes.

    Jev reads a final message fast and shallow, so every row it judged from one
    (unsure, stalled, or waiting) is queued, and so is a stalled row whose own PR
    merged although its message did not say it was done. A row keeps the verdict
    orca-row-classify gave it while its message, PR, review, and tree stay as read, so
    only new or changed rows are read again.
    """
    verdicts = verdicts or {}

    def needs_reader(row):
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
             "final_at": row_final_at(row), "read": read_key(row)}
            for row in rows if needs_reader(row)]


def read_json_file(path):
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


def load_verdicts(store_path):
    """The verdict store as a dict; a damaged one is moved aside to `<name>.bad`, never overwritten."""
    store = Path(store_path).expanduser()
    if not store.exists():
        return {}
    data = read_json_file(store)
    if isinstance(data, dict):
        return {path: v for path, v in data.items() if isinstance(v, dict)}
    store.replace(store.with_name(store.name + ".bad"))
    return {}


def classed_queue(session):
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


def newer_verdicts(verdicts, incoming):
    """`verdicts` updated with each incoming verdict whose read is not older than the one it replaces.

    A session directory keeps its last classed queue, so an old session's read is folded in on every
    scan; it must not undo a later read of the same worktree that another session already stored.
    """
    def older(path, verdict):
        held = verdicts.get(path)
        return held is not None and str(verdict.get("queued_at") or "") < str(held.get("queued_at") or "")
    return {**verdicts, **{path: v for path, v in incoming.items() if not older(path, v)}}


def absorb_verdicts(store_path, write=True):
    """Fold every session's classed queue (the store's sibling directories) into the store and return it."""
    store = Path(store_path).expanduser()
    verdicts = load_verdicts(store)
    for queue in sorted(store.parent.glob("*/review-queue.json"), key=lambda p: p.stat().st_mtime):
        verdicts = newer_verdicts(verdicts, classed_queue(queue.parent))
    if write:
        write_json_atomically(store, verdicts)
    return verdicts


def with_verdicts(rows, verdicts):
    """Rows a careful reader classed carry that verdict while what it read still holds."""
    def marked(row):
        verdict = current_verdict(row, verdicts) if row["class"] in READ_CLASSES else None
        return {**row, "verdict": verdict} if verdict else row
    return [marked(row) for row in rows]


def read_codex(session_roots, since=None):
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


def orca_payload(orca: str, args: list[str], json_file: str | None = None, timeout: float | None = None) -> Any:
    if json_file:
        data = json.loads(Path(json_file).read_text())
    else:
        out = subprocess.run([orca, *args, "--json"], capture_output=True, text=True,
                             check=True, timeout=timeout or ORCA_TIMEOUT).stdout
        data = json.loads(out)
    return data.get("result", data) if isinstance(data, dict) else data


def orca_result(orca: str, args: list[str], key: str, json_file: str | None, timeout: float | None = None) -> Any:
    return orca_payload(orca, args, json_file, timeout)[key]


def endpoint_diagnostic(environment, environments):
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


def screen_targets(worktrees, terminals):
    """Tabs whose screens the scan reads: those of a worktree with a done agent, for its done cards, and
    those of another Mac's worktree Orca holds no agent message for, for the agent and reply they show."""
    done = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees
            if any(a.get("state") == "done" for a in w.get("agents") or [])}
    bare = {(w.get("_environment"), w.get("worktreeId")) for w in worktrees
            if w.get("_environment") and not orca_said(w)}
    return [t for t in terminals if t.get("handle") and (
        (key := (t.get("_environment"), t.get("worktreeId"))) in done or key in bare)]


def read_screens(orca, terms):
    """Each tab's rendered screen by (environment, handle), read from the Orca server that issued the
    handle; a tab whose read fails is left out."""
    def read(term):
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


def orca_runs(orca):
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


def read_mail(args):
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


def expand_roots(patterns):
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
                             "(default: 30)")
    parser.add_argument("--jev-key-file", default=JEV_KEY_FILE,
                        help="Jev (TypeSafe) API key file, read when TYPESAFE_API_KEY is unset "
                             "(default: %(default)s)")
    parser.add_argument("--review-out",
                        help="also write the rows a careful reader must class (every row Jev judged from a final "
                             "message without a verdict for it yet, and a stalled row whose own PR merged) to this "
                             "JSON file, for orca-row-classify; verdicts already written for any session's queue "
                             "beside the store are kept in --verdicts first")
    views = parser.add_mutually_exclusive_group()
    views.add_argument("--summary", action="store_true",
                       help="print class counts, review queue rows, and finished worktree/tab counts instead of JSON")
    views.add_argument("--rows", action="store_true",
                       help="print one line per row: name | class (the verdict's, else the scan's) | GitHub "
                            "repo#PR state | tab count | newest final message time | question, waits_on, "
                            "next_step, or the final message's start")
    views.add_argument("--cards", action="store_true",
                       help="print one line per done card (handle, agent, interrupted, whether its composer holds a "
                            "draft, Orca's copy of its last message, and the end of its screen), closable tab, and "
                            "meaningful tab, each led by the worktree's name and class as in --rows")
    views.add_argument("--row", metavar="NAME",
                       help="print one row as JSON with fixed keys: strings are never null, `final` is the newest "
                            "final message of Claude, Codex, or Orca, and `github` is owner/repo from origin; "
                            "NAME is the worktree's last path segment (ENVIRONMENT::name on another Mac), "
                            "its path, or its row identity")
    parser.add_argument("--from", dest="from_file", metavar="SCAN_JSON",
                        help="read the rows a previous scan printed as JSON instead of scanning again")
    parser.add_argument("--inbox", default=os.environ.get("TRIAGE_INBOX") or INBOX_FILE,
                        help="triage inbox whose newest done and ask per worktree go into the review queue "
                             "(default: $TRIAGE_INBOX or %(default)s)")
    parser.add_argument("--verdicts", default=VERDICTS_FILE,
                        help="where orca-row-classify verdicts are kept per worktree (default: %(default)s)")
    parser.add_argument("--pr-cache", default=PR_ACTIVITY_DIR,
                        help="where each PR's reviews and comments are kept between runs, one file per PR; "
                             "they are read again only when the PR's updatedAt moves (default: %(default)s)")
    args = parser.parse_args(argv)
    if args.from_file and args.review_out:
        parser.error("--review-out needs a live scan; drop --from")
    return args


def write_review_queue(path, rows, verdicts=None):
    """Write {"generated_at", "rows"} so a later check can tell whether this scan's queue was classed."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "rows": review_queue(rows, verdicts)}
    write_json_atomically(target, payload)


def print_summary(rows, verdicts):
    counts = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    print("classes: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    queue = review_queue(rows, verdicts)
    print(f"review queue: {len(queue)}")
    for row in queue:
        print(f"  {row['repo']}/{Path(row['path']).name} [{row['class']}]")
    finished = [row for row in rows if row["class"] == "finished"
                or (row.get("verdict") or {}).get("class") == "finished"]
    print(f"finished: {len(finished)} worktrees, {sum(len(row['terminals']) for row in finished)} tabs")
    for row in finished:
        print(f"  {row['repo']}/{Path(row['path']).name} ({len(row['terminals'])} tabs)")
    open_pr_cleanup = [row for row in rows if row.get("open_pr_cleanup")]
    print(f"open PR cleanup: {len(open_pr_cleanup)} worktrees")
    for row in open_pr_cleanup:
        print(f"  {row['repo']}/{Path(row['path']).name} [{row['class']}]")
    hooks = [(row, error) for row in rows for error in row.get("hook_errors") or []]
    print(f"hook errors: {len(hooks)}")
    for row, error in hooks:
        print(f"  {error['host']} {error['path']} ({row_name(row)} {error.get('tab') or '-'})")


def row_name(row: dict) -> str:
    name = Path(row["path"]).name
    return f"{row['environment']}::{name}" if row.get("environment") else name


def final_message(row: dict) -> dict:
    """{source, at, text} of the newest final message the row's agents or Orca hold; empty strings without one."""
    latest = latest_message({"claude": row.get("claude"), "codex": row.get("codex") or [],
                             "orca_message": row.get("orca_message")})
    if latest is None:
        return {"source": "", "at": "", "text": ""}
    source, text = latest
    at = {"claude": (row.get("claude") or {}).get("final_at"),
          "codex": ((row.get("codex") or [{}])[-1]).get("last_assistant_at"),
          "orca": (row.get("orca_message") or {}).get("at")}[source]
    return {"source": source, "at": at or "", "text": text or ""}


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


def row_view(row: dict) -> dict:
    """One row with fixed keys and no null strings, so a reader never has to know the scan's raw shapes."""
    verdict = row.get("verdict") or {}
    final = final_message(row)
    asked = {"claude": (row.get("claude") or {}).get("last_user"),
             "codex": ((row.get("codex") or [{}])[-1]).get("last_user")}.get(final["source"])
    return {
        "identity": row_identity(row), "name": row_name(row), "path": row["path"],
        "repo": row.get("repo") or "", "environment": row.get("environment") or "", "github": github_of(row),
        "class": verdict.get("class") or row["class"], "scan_class": row["class"],
        "reasons": row.get("reasons") if isinstance(row.get("reasons"), list) else [],
        "question": verdict.get("question") or "", "waits_on": verdict.get("waits_on") or row.get("waits_on") or "",
        "next_step": verdict.get("next_step") or "", "verdict": verdict or None,
        "final": final, "last_user": asked or "",
        "comment": row.get("comment") or "", "pr": row.get("pr"), "git": row.get("git"),
        "tabs": [{"handle": t.get("handle") or "", "agent": t.get("agent") or "", "title": t.get("title") or ""}
                 for t in row.get("terminals") or []],
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
    return f"{view['name']} | {klass} | {where} | tabs {len(view['tabs'])} | {view['final']['at'] or '-'} | {note}"


CLOSE_TABS = Path(__file__).absolute().with_name("close_tabs.py")


def card_lines(row: dict, scan_file: Path | None = None) -> list[str]:
    """A line per done card, closable tab, and meaningful tab; a scan saved before cards had screens reads as unknown.

    With the saved scan's path, a row with closable tabs ends with the close_tabs.py command that closes them
    once nothing moved after the scan.
    """
    lead = f"{row_name(row)} | {class_label((row.get('verdict') or {}).get('class') or row['class'], row['class'])}"
    lines = []
    for card in row.get("done_cards") or []:
        said = " ".join((card.get("last_message") or "").split())
        who = f"card {card.get('handle') or '-'} {card.get('agent') or '-'}" + (
            " interrupted" if card.get("interrupted") else "")
        draft = {True: "yes", False: "no"}.get(card.get("draft"), "unknown")
        excerpt = (said[:160] + ("…" if len(said) > 160 else "")) or "-"
        lines.append(f"{lead} | {who} | draft {draft} | {excerpt} | screen: "
                     + (" ⏎ ".join(card.get("screen_tail") or []) or "-"))
    lines.extend(f"{lead} | closable {handle}" for handle in row.get("closable") or [])
    lines.extend(f"{lead} | meaningful {handle}" for handle in row.get("meaningful_tabs") or [])
    if scan_file and row.get("closable"):
        lines.append(f"{lead} | close: " + shlex.join(["python3", str(CLOSE_TABS), "--from", str(scan_file),
                                                      *row["closable"]]))
    return lines


def saved_rows(path: str) -> list[dict]:
    rows = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(row, dict) and "path" in row for row in rows):
        raise ValueError(f"{path} is not the JSON list a scan prints")
    return rows


def print_row(rows: list[dict], key: str) -> int:
    # A row identity names one row even when another Mac has a checkout at the same path.
    found = ([row for row in rows if row_identity(row) == key]
             or [row for row in rows if key in (row["path"], row_name(row))])
    if len(found) != 1:
        names = ", ".join(row_identity(row) for row in found)
        print(f"--row {key}: " + (f"matches {names}; pass one of them" if found else "no row has this name"),
              file=sys.stderr)
        return 1
    json.dump(row_view(found[0]), sys.stdout, ensure_ascii=False, indent=2)
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
        return print_rows(args, with_verdicts(rows, verdicts), verdicts)
    # close_tabs.py keeps a tab whose worktree moved after this moment.
    scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    worktrees, terminals = collect_worktrees_and_terminals(args.orca, args.ps_json, args.terminals_json)
    since = time.time() - args.since_hours * 3600 if args.since_hours else None
    codex = read_codex(expand_roots(args.codex_sessions or DEFAULT_CODEX_SESSIONS), since)
    key = jev_key(os.environ, args.jev_key_file)
    if key is None:
        print(f"TYPESAFE_API_KEY is unset and {args.jev_key_file} is missing or unreadable: "
              "idle final messages come back unsure, so read them", file=sys.stderr)
    judge = jev_judge(key) if key else (lambda message: None)
    # Orca state read from files has no live tabs to read.
    screens = {} if args.ps_json else read_screens(args.orca, screen_targets(worktrees, terminals))
    rows = add_next_step_facts(build_inventory(worktrees, terminals, str(Path(args.claude_projects).expanduser()),
                                               codex, args.decision_marker,
                                               shell_idle_minutes=args.shell_idle_minutes, judge=judge,
                                               mail=read_mail(args), screens=screens),
                               pr_lookup=partial(gh_prs, cache_dir=Path(args.pr_cache).expanduser()))
    rows = [{**row, "scanned_at": scanned_at}
            for row in with_inbox(rows, inbox_reports(Path(args.inbox).expanduser()))]
    verdicts = absorb_verdicts(args.verdicts, write=bool(args.review_out))
    rows = with_verdicts(rows, verdicts)
    if args.review_out:
        write_review_queue(args.review_out, rows, verdicts)
    return print_rows(args, rows, verdicts)


def print_rows(args: argparse.Namespace, rows: list[dict], verdicts: dict) -> int:
    if args.row:
        return print_row(rows, args.row)
    if args.rows:
        for row in rows:
            print(row_line(row_view(row)))
    elif args.cards:
        scan_file = Path(args.from_file).expanduser().absolute() if args.from_file else None
        for row in rows:
            for line in card_lines(row, scan_file):
                print(line)
    elif args.summary:
        print_summary(rows, verdicts)
    else:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
