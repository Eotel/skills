from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import socket
import subprocess
from typing import Any
import unicodedata

import hourly_runtime
import inbox_read
import orca_agent_status
import retro
import scan_sessions

INJECTED = re.compile(r"<(?P<tag>task-notification|system-reminder|environment_context|image|local-command-stdout"
                      r"|local-command-caveat)\b[^>]*>.*?</(?P=tag)>", re.S)
SENT_WITHIN = timedelta(minutes=2)
QUEUED_WITHIN = timedelta(hours=1)
CLAUDE_SHIP = re.compile(r"\s*<command-message>ship</command-message>\s*<command-name>/ship</command-name>\s*"
                         r"<command-args>(.*?)</command-args>\s*", re.S)
OPTION_CODE = re.compile(r"^([a-z0-9])\s*:\s*\S")


def user_prompt(row: dict[str, Any]) -> str:
    if row.get("isMeta") or row.get("isCompactSummary") or row.get("isSidechain"):
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
    clean = INJECTED.sub("", text)
    if command := CLAUDE_SHIP.fullmatch(clean):
        clean = f"/ship {command[1]}"
    return " ".join(clean.split())


def lane_prompts(ask: dict[str, Any], after: datetime, panes: dict[str, dict[str, Any]],
                 handles: dict[str, str], *, require_handle: bool = False) -> tuple[set[str], list[tuple[str, datetime]]]:
    lane, handle, repo = ask.get("worktree"), ask.get("handle"), ask.get("repo")
    suffix = Path(lane).parts if lane else ()
    chosen = [pane for pane, terminal in handles.items() if terminal == handle]
    if not chosen and not require_handle:
        chosen = [pane for pane, entry in panes.items()
                  if suffix and (parts := Path((entry.get("worktreeId") or "").split("::", 1)[-1]).parts)[-len(suffix):] == suffix
                  and (not repo or repo in parts)]
    if len(chosen) != 1 or chosen[0] not in panes:
        return set(), []
    pane = chosen[0]
    path = (panes[pane].get("providerSession") or {}).get("transcriptPath")
    if not path:
        return set(), []
    targets = {target for target in (handles.get(pane) or handle or lane,) if target}
    prompts = []
    for row in retro.transcript_rows(Path(path).expanduser()):
        if (not isinstance(row, dict) or not (at := scan_sessions.parse_time(row.get("timestamp")))
                or at <= after - QUEUED_WITHIN):
            continue
        if prompt := user_prompt(row):
            prompts.append((prompt, at.astimezone(timezone.utc)))
    return targets, prompts


def direct_prompts(asks: dict[str, dict[str, Any]], orca: str, *,
                   require_handle: bool = False) -> dict[str, list[tuple[str, datetime]]]:
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
        terminals = scan_sessions.orca_result(orca, ["terminal", "list", "--environment", "local", "--limit", "10000"],
                                              "terminals", None, timeout=5)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        terminals = []
    handles = {hourly_runtime.pane_key(term): term["handle"] for term in terminals if term.get("handle")}
    candidates = {}
    for ask_id, (ask, at) in local.items():
        try:
            targets, prompts = lane_prompts(ask, at, panes, handles, require_handle=require_handle)
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
                stages = event.get("delivery")
                queued = isinstance(stages, list) and "input_accepted" in stages and "turn_started" not in stages
                for targets, prompts in candidates.values():
                    if isinstance(recipient, str) and recipient in targets:
                        matches = [(abs(at - sent_at), index) for index, (prompt, at) in enumerate(prompts)
                                   if prompt == sent and (abs(at - sent_at) <= SENT_WITHIN
                                                          or queued and timedelta() < at - sent_at <= QUEUED_WITHIN)]
                        if require_handle and len(matches) > 1:
                            for index in sorted((index for _, index in matches), reverse=True):
                                prompts.pop(index)
                        elif matches:
                            prompts.pop(min(matches)[1])
    except (OSError, RuntimeError):
        return {}
    return {ask_id: direct for ask_id, (_, prompts) in candidates.items()
            if (direct := sorted((pair for pair in prompts if pair[1] > local[ask_id][1]), key=lambda pair: pair[1]))}


def normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def option_key(value: str) -> str:
    label = normalized(value)
    if code := OPTION_CODE.match(label):
        return code[1]
    return label.removesuffix("(推奨)").removesuffix("(recommended)").strip()


def option_labels(question: dict) -> list[str]:
    return [label for option in question.get("options") or []
            if isinstance(option, dict) and isinstance(label := option.get("label"), str) and label.strip()]


def inferred_answers(entries: list[dict], answers: dict[str, dict], orca: str,
                     wanted: set[str] | None = None) -> dict[str, dict]:
    pending = {ask.report["id"]: ask for ask in inbox_read.open_asks(entries, frozenset(answers))
               if ask.report.get("handle") and ask.open_questions and (wanted is None or ask.report["id"] in wanted)}
    by_handle = {ask.report["handle"]: ask.report for ask in reversed(list(pending.values()))}
    prompts = direct_prompts(by_handle, orca, require_handle=True)
    matches: dict[tuple[str, datetime, str], list[tuple[str, str]]] = {}
    for ask in inbox_read.open_asks(entries, frozenset()):
        ask_id, handle = ask.report["id"], ask.report.get("handle")
        if not ask.at or handle not in prompts or by_handle[handle]["host"] != ask.report.get("host"):
            continue
        for number, question in enumerate(inbox_read.questions_of(ask.report), 1):
            if not isinstance(question, dict) or question.get("multiSelect"):
                continue
            answer_id = f"{ask_id}:{number}"
            closed = min((at for key in (ask_id, answer_id) if (record := answers.get(key))
                          and (at := inbox_read.asked_at(record))), default=None)
            labels = option_labels(question)
            for prompt, at in prompts[handle]:
                if at <= ask.at or (closed and closed < at):
                    continue
                choice = normalized(prompt.removeprefix("/ship "))
                selected = [label for label in labels if normalized(label) == choice]
                for label in (selected or [label for label in labels if option_key(label) == choice]):
                    key = (handle, at, prompt)
                    matches.setdefault(key, []).append((answer_id, label))
    choices: dict[str, dict[str, datetime]] = {}
    available = {f"{ask_id}:{number}" for ask_id, ask in pending.items() for number in ask.open_questions}
    for (_, at, _), matches_for_input in matches.items():
        if len(matches_for_input) == 1:
            answer_id, label = matches_for_input[0]
            if answer_id in available:
                choices.setdefault(answer_id, {}).setdefault(label, at)
    return {answer_id: {"id": answer_id, "ts": at.isoformat(), "how": inbox_read.TRANSCRIPT_ANSWER,
                        "sender": {"worktree": pending[answer_id.partition(":")[0]].report.get("worktree"), "session": None},
                        "note": label}
            for answer_id, selected in choices.items() if len(selected) == 1 for label, at in selected.items()}


def reconcile_answers(entries: list[dict], path: Path, orca: str) -> None:
    write_inferred_answers(inferred_answers(entries, inbox_read.answer_records(path), orca), path)


def write_inferred_answers(detected: dict[str, dict], path: Path) -> None:
    if not detected:
        return
    path = path.resolve()
    with retro.locked(path.with_name(path.name + ".lane-answer.lock")):
        answered = inbox_read.answer_records(path)
        used = {((record.get("sender") or {}).get("worktree"), record.get("ts"), record.get("note"))
                for record in answered.values() if record.get("how") == inbox_read.TRANSCRIPT_ANSWER}
        for answer_id, record in detected.items():
            source = (record["sender"]["worktree"], record["ts"], record["note"])
            if source not in used and answer_id not in answered and answer_id.partition(":")[0] not in answered:
                inbox_read.record_answers(path, [answer_id], record["how"], record["note"],
                                          at=record["ts"], sender=record["sender"])
                used.add(source)
