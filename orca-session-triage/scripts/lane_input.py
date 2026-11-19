from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import subprocess
from typing import Any

import hourly_runtime
import inbox_read
import orca_agent_status
import retro
import scan_sessions

INJECTED = re.compile(r"<(?P<tag>task-notification|system-reminder|environment_context|image|local-command-stdout"
                      r"|local-command-caveat)\b[^>]*>.*?</(?P=tag)>", re.S)
SENT_WITHIN = timedelta(minutes=2)


def user_prompt(row: dict[str, Any]) -> str:
    if row.get("isMeta") or row.get("isCompactSummary"):
        return ""
    if row.get("type") == "user":
        text = scan_sessions.user_text(row) or ""
    else:
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
            return ""
        text = scan_sessions.message_text(payload)
        if text.lstrip().startswith("# AGENTS.md instructions"):
            return ""
    return " ".join(INJECTED.sub("", text).split())


def lane_prompts(ask: dict[str, Any], after: datetime, panes: dict[str, dict[str, Any]],
                 handles: dict[str, str]) -> tuple[set[str], list[tuple[str, datetime]]]:
    lane, handle, repo = ask.get("worktree"), ask.get("handle"), ask.get("repo")
    suffix = Path(lane).parts if lane else ()
    chosen = [pane for pane in panes if handle and handles.get(pane) == handle] or [
        pane for pane, entry in panes.items()
        if suffix and (parts := Path((entry.get("worktreeId") or "").split("::", 1)[-1]).parts)[-len(suffix):] == suffix
        and (not repo or repo in parts)]
    if len(chosen) != 1:
        return set(), []
    pane = chosen[0]
    path = (panes[pane].get("providerSession") or {}).get("transcriptPath")
    if not path:
        return set(), []
    targets = {target for target in (handles.get(pane) or handle or lane,) if target}
    prompts = []
    for row in retro.transcript_rows(Path(path).expanduser()):
        if not isinstance(row, dict) or not (at := scan_sessions.parse_time(row.get("timestamp"))) or at <= after:
            continue
        if prompt := user_prompt(row):
            prompts.append((prompt, at.astimezone(timezone.utc)))
    return targets, prompts


def latest(asks: dict[str, dict[str, Any]], orca: str) -> dict[str, tuple[str, datetime]]:
    host = os.environ.get("TRIAGE_HOST") or socket.gethostname()
    local = {ask_id: (ask, at) for ask_id, ask in asks.items()
             if ask.get("host") == host and (at := inbox_read.asked_at(ask))}
    if not local:
        return {}
    try:
        status = Path(os.environ.get("TRIAGE_AGENT_STATUS_FILE") or orca_agent_status.DEFAULT_PATH).expanduser()
        panes = orca_agent_status.pane_entries(status)
    except (OSError, RuntimeError, orca_agent_status.AgentStatusError):
        return {}
    try:
        terminals = scan_sessions.orca_result(orca, ["terminal", "list", "--environment", "local"],
                                              "terminals", None, timeout=1)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        terminals = []
    handles = {hourly_runtime.pane_key(term): term["handle"] for term in terminals if term.get("handle")}
    candidates = {}
    for ask_id, (ask, at) in local.items():
        try:
            targets, prompts = lane_prompts(ask, at, panes, handles)
        except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
            continue
        if prompts:
            candidates[ask_id] = targets, prompts
    if not candidates:
        return {}
    try:
        with inbox_read.events_path().open("rb") as stream:
            for raw in stream:
                event = inbox_read.entry(raw)
                if event is None or event.get("type") != "send" or not isinstance(text := event.get("text"), str):
                    continue
                if not (sent_at := scan_sessions.parse_time(event.get("ts"))):
                    continue
                sent = " ".join(text.split())
                recipient = event.get("to") or event.get("lane")
                for ask_id, (targets, prompts) in candidates.items():
                    if isinstance(recipient, str) and recipient in targets:
                        candidates[ask_id] = targets, [(prompt, at) for prompt, at in prompts
                                                       if prompt != sent or abs(at - sent_at) > SENT_WITHIN]
    except (OSError, RuntimeError):
        return {}
    return {ask_id: max(prompts, key=lambda pair: pair[1]) for ask_id, (_, prompts) in candidates.items() if prompts}
