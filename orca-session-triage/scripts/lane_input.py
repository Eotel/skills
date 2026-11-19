from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
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


def delivery_events(path: Path, recipients: set[str]) -> dict[str, list[tuple[datetime, str, bool]]]:
    pending: dict[str, tuple[str, datetime, str]] = {}
    deliveries: dict[str, list[tuple[datetime, str, bool]]] = {}
    journal = path.with_name(path.name + ".delivery")
    for source in (journal, path):
        if not source.is_file():
            continue
        for event in inbox_read.inbox_entries(source):
            if not event:
                continue
            if event.get("type") == "send-refused" and isinstance(intent := event.get("intent_id"), str):
                pending.pop(intent, None)
                continue
            if event.get("type") not in ("send", "send-intent"):
                continue
            recipient = event.get("to") or event.get("lane")
            text = event.get("text")
            if (not isinstance(recipient, str) or recipient not in recipients or not isinstance(text, str)
                    or not (at := inbox_read.asked_at(event))):
                continue
            sent = " ".join(text.split())
            if event["type"] == "send-intent":
                if isinstance(intent := event.get("id"), str):
                    pending[intent] = recipient, at, sent
                continue
            stages = event.get("delivery")
            queued = isinstance(stages, list) and "input_accepted" in stages and "turn_started" not in stages
            if isinstance(intent := event.get("intent_id"), str):
                planned = pending.pop(intent, None)
                if queued and planned and planned[0] == recipient and planned[2] == sent:
                    at = planned[1]
            deliveries.setdefault(recipient, []).append((at, sent, queued))
    if not journal.is_file() and not path.is_file():
        raise FileNotFoundError(path)
    for recipient, at, sent in pending.values():
        deliveries.setdefault(recipient, []).append((at, sent, True))
    return deliveries


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
        deliveries = delivery_events(inbox_read.events_path(), {target for targets, _ in candidates.values() for target in targets})
    except (OSError, RuntimeError):
        return {}
    for targets, prompts in candidates.values():
        for recipient in targets:
            for sent_at, sent, queued in deliveries.get(recipient, []):
                matches = [(abs(at - sent_at), index) for index, (prompt, at) in enumerate(prompts)
                           if prompt == sent and (abs(at - sent_at) <= SENT_WITHIN
                                                  or queued and timedelta() < at - sent_at <= QUEUED_WITHIN)]
                if require_handle and len(matches) > 1:
                    for index in sorted((index for _, index in matches), reverse=True):
                        prompts.pop(index)
                elif matches:
                    prompts.pop(min(matches)[1])
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


def inbox_version(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns


def candidate_answers(inbox: Path, path: Path, orca: str,
                      wanted: set[str] | None = None) -> dict[str, list[dict]]:
    closed_at: dict[str, datetime | None] = {}
    version = inbox_version(inbox)
    entries = inbox_read.inbox_entries(inbox) if version is not None else []
    history = inbox_read.open_asks(entries, frozenset())
    answer_history = list(inbox_read.answer_entries(path))
    local_host = os.environ.get("TRIAGE_HOST") or socket.gethostname()
    exportable = {ask.report["id"] for ask in history if ask.report.get("host") == local_host}
    saved: dict[str, list[dict]] = {}
    for record in answer_history:
        if wanted and record.get("how") == inbox_read.TRANSCRIPT_ANSWER:
            ask_id = record["id"].partition(":")[0]
            if ask_id in wanted and ask_id in exportable:
                if isinstance(record.get("input"), str):
                    saved.setdefault(record["id"], []).append(record)
                else:
                    continue
        previous = closed_at.setdefault(record["id"], None)
        if (at := inbox_read.asked_at(record)) and (previous is None or at < previous):
            closed_at[record["id"]] = at
    for ref, at in inbox_read.withdrawal_times(entries).items():
        if (previous := closed_at.get(ref)) is None or at < previous:
            closed_at[ref] = at
    pending = {ask.report["id"]: ask for ask in inbox_read.open_asks(entries, frozenset(closed_at))
               if ask.report.get("handle") and ask.open_questions and (wanted is None or ask.report["id"] in wanted)}
    by_handle = {ask.report["handle"]: ask.report for ask in reversed(list(pending.values()))}
    prompts = direct_prompts(by_handle, orca, require_handle=True)
    matches: dict[tuple[str, datetime, str], list[tuple[str, str]]] = {}
    multi_select = set()
    for ask in history:
        ask_id, handle = ask.report["id"], ask.report.get("handle")
        if not ask.at or handle not in prompts or by_handle[handle]["host"] != ask.report.get("host"):
            continue
        for number, question in enumerate(inbox_read.questions_of(ask.report), 1):
            if not isinstance(question, dict):
                continue
            answer_id = f"{ask_id}:{number}"
            if question.get("multiSelect"):
                multi_select.add(answer_id)
            closed = min((at for key in (ask_id, answer_id) if (at := closed_at.get(key))), default=None)
            labels = option_labels(question)
            for prompt, at in prompts[handle]:
                if at <= ask.at or (closed and closed < at):
                    continue
                choice = normalized(prompt.removeprefix("/ship "))
                selected = [label for label in labels if normalized(label) == choice]
                for label in (selected or [label for label in labels if option_key(label) == choice]):
                    key = (handle, at, prompt)
                    matches.setdefault(key, []).append((answer_id, label))
    choices: dict[str, list[dict]] = {}
    available = {f"{ask_id}:{number}" for ask_id, ask in pending.items() for number in ask.open_questions}
    for (_, at, prompt), matches_for_input in matches.items():
        if len(matches_for_input) == 1:
            answer_id, label = matches_for_input[0]
            if answer_id in available and answer_id not in multi_select:
                choices.setdefault(answer_id, []).append({
                    "id": answer_id, "ts": at.isoformat(), "how": inbox_read.TRANSCRIPT_ANSWER, "note": label, "input": prompt,
                    "sender": {"worktree": pending[answer_id.partition(":")[0]].report.get("worktree"), "session": None},
                })
    if (choices or saved) and (version != inbox_version(inbox) or answer_history != list(inbox_read.answer_entries(path))):
        return {}
    return choices | saved


def unambiguous_answers(choices: dict[str, list[dict]]) -> dict[str, dict]:
    return {answer_id: min(records, key=lambda record: datetime.fromisoformat(record["ts"]))
            for answer_id, records in choices.items() if len({record["note"] for record in records}) == 1}


def export_candidates(inbox: Path, path: Path, orca: str, wanted: set[str]) -> dict[str, list[dict]]:
    def scopes() -> dict[str, str]:
        entries = inbox_read.inbox_entries(inbox) if inbox.is_file() else []
        asks = [row for row in entries if row.get("kind") == "ask" and row.get("id")]
        signatures = {}
        for ask in asks:
            if ask["id"] not in wanted:
                continue
            lane = (ask.get("host"), ask.get("handle"))
            nearby = [row for row in asks if (row.get("host"), row.get("handle")) == lane]
            ids = {row["id"] for row in nearby}
            updates = [row for row in entries if any(isinstance(ref, str) and ref.partition(":")[0] in ids
                                                      for ref in row.get("withdraws") or [])]
            signatures[ask["id"]] = sha256(json.dumps([nearby, updates], sort_keys=True).encode()).hexdigest()
        return signatures

    cache = path.resolve().with_name(path.name + ".lane-candidates.json")
    with retro.locked(cache.with_suffix(".lock")):
        versions = scopes()
        detected = candidate_answers(inbox, path, orca, wanted)
        if versions != scopes():
            return {}
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        saved: dict[str, list[dict]] = data.get("candidates", {})
        stored_versions: dict[str, str] = data.get("versions", {})
        saved = {key: rows for key, rows in saved.items()
                 if (ask := key.partition(":")[0]) not in wanted or stored_versions.get(ask) == versions.get(ask)}
        for answer_id, records in detected.items():
            combined = {(row["ts"], row["input"]): row for row in [*saved.get(answer_id, ()), *records]}
            saved[answer_id] = list(combined.values())
        if detected:
            retro.atomic_text(cache, json.dumps({"versions": stored_versions | versions, "candidates": saved},
                                                ensure_ascii=False) + "\n")
        return {answer_id: records for answer_id, records in saved.items() if answer_id.partition(":")[0] in versions}


def inferred_answers(inbox: Path, path: Path, orca: str, wanted: set[str] | None = None) -> dict[str, dict]:
    return unambiguous_answers(candidate_answers(inbox, path, orca, wanted))


def reconcile_answers(inbox: Path, path: Path, orca: str) -> None:
    if inferred_answers(inbox, path, orca):
        write_inferred_answers(lambda: inferred_answers(inbox, path, orca), path)


def write_inferred_answers(detected: dict[str, dict] | Callable[[], dict[str, dict]], path: Path) -> None:
    if not detected:
        return
    path = path.resolve()
    with retro.locked(path.with_name(path.name + ".lane-answer.lock")):
        records = detected if isinstance(detected, dict) else detected()
        if not records:
            return
        answered = inbox_read.answer_records(path)
        used = {((record.get("sender") or {}).get("worktree"), record.get("ts"), record.get("note"))
                for record in answered.values() if record.get("how") == inbox_read.TRANSCRIPT_ANSWER}
        for answer_id, record in records.items():
            source = (record["sender"]["worktree"], record["ts"], record["note"])
            if source not in used and answer_id not in answered and answer_id.partition(":")[0] not in answered:
                inbox_read.record_answers(path, [answer_id], record["how"], record["note"],
                                          at=record["ts"], sender=record["sender"], transcript_input=record.get("input"))
                used.add(source)
