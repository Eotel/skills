"""Record an answer under the inbox ids and decision keys that name the same ask."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import uuid

import ask_state
import hourly_state
import inbox_read
import lead_decisions


def handoff_keys(ids: list[str], entries: list[dict], handed_off: Path, saved: Path) -> set[str]:
    """Keys in the exact saved handoff, or named literally in an older/imported handoff's body."""
    reports = [item for item in entries if item.get("id") in {value.partition(":")[0] for value in ids}
               and inbox_read.is_handoff(item)]
    if not reports:
        return set()
    known = list(lead_decisions.handed_off(handed_off))
    exact = {}
    for path in sorted(saved.glob("*.json"), reverse=True):
        item = json.loads(path.read_text(encoding="utf-8"))
        exact.setdefault(str(item.get("text") or "").strip(), item.get("decisions") or [])
    keys = set()
    for report in reports:
        text = str(report.get("text") or "").strip()
        if text in exact:
            keys.update(exact[text])
        else:
            keys.update(item.decision for item in known if item.ask_id == report["id"] or re.search(
                r"(?<![A-Za-z0-9_/.-])" + re.escape(item.decision) + r"(?![A-Za-z0-9_/.-])", text))
    return keys


def answer_text(ref: tuple[str, frozenset[int]], records: dict[str, dict], entries: list[dict]) -> str:
    ask_id, numbers = ref
    if ask_id in records:
        return str(records[ask_id].get("note") or "")
    if not numbers:
        ask = next(item for item in entries if item.get("id") == ask_id)
        numbers = frozenset(range(1, len(inbox_read.questions_of(ask)) + 1))
    return "\n".join(dict.fromkeys(str(records[f"{ask_id}:{number}"].get("note") or "")
                                  for number in sorted(numbers)))


@dataclass(frozen=True)
class InboxAnswer:
    ids: list[str]
    how: str
    answer: str
    at: str


def hourly_answers(items: list[tuple[int | None, dict]]) -> list[InboxAnswer]:
    entries = hourly_state.inbox_entries()
    if not entries:
        return []
    recorded = inbox_read.answer_records(inbox_read.answers_path())
    planned = []
    for number, item in items:
        if item.get("withdrawn_at") or not item.get("answer"):
            continue
        ids = []
        for ask_id, parts in ask_state.references(item, entries):
            ids.extend([f"{ask_id}:{number}" for number in sorted(parts)] if parts else [ask_id])
        if not ids:
            continue
        try:
            checked = inbox_read.checked_answer_ids(",".join(dict.fromkeys(ids)), entries)
        except ValueError as error:
            if number is None:
                raise
            raise ValueError(f"nothing was recorded\nline {number}: {error}") from error
        replacement = (item.get("replaced") or [{}])[-1].get("replaced_at")
        grouped: dict[tuple[str, str], list[str]] = {}
        for key in checked:
            previous = recorded.get(key)
            if previous is None or previous.get("how") not in inbox_read.SUBSTANTIVE_ANSWERS:
                how, at = inbox_read.HOURLY_ANSWER, item["answered_at"]
            elif (replacement and previous.get("note") != item["answer"] and previous.get("ts")
                  and datetime.fromisoformat(previous["ts"]) <= datetime.fromisoformat(replacement)):
                how, at = inbox_read.HOURLY_CORRECTION, replacement
            else:
                continue
            grouped.setdefault((how, at), []).append(key)
            recorded[key] = {"note": item["answer"], "how": how, "ts": at}
        planned.extend(InboxAnswer(keys, how, item["answer"], at) for (how, at), keys in grouped.items())
    return planned


def record(ids: list[str], answer: str, how: str, *, at: str | None = None,
           decisions: Mapping[str, str] | None = None, state_file: Path | None = None,
           decisions_file: Path | None = None, handed_off_file: Path | None = None) -> dict:
    """Validate related hourly answers first, then persist their shared text and timestamp.

    Repeating a completed delivery is safe. Each store keeps its existing lock; a failed write
    raises so the sender can report that delivery succeeded but recording needs repair.
    """
    state_file = (state_file or Path(hourly_state.DEFAULT_STATE)).expanduser()
    decisions_file = (decisions_file or Path(os.environ.get("TRIAGE_LEAD_DECISIONS") or lead_decisions.DEFAULT_LOG)).expanduser()
    handed_off_file = (handed_off_file or Path(os.environ.get("TRIAGE_HANDED_OFF") or lead_decisions.HANDED_OFF_LOG)).expanduser()
    when = at or hourly_state.stamp()
    hourly_state.recorded_at(when)
    entries = inbox_read.inbox_entries(inbox_read.paths()[0]) if ids else []
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.with_name(state_file.name + ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = hourly_state.read(state_file)
        log = inbox_read.answers_path()
        recorded = inbox_read.answer_records(log)
        delivered = inbox_read.delivered_records(log)
        records = {key: value for key, value in recorded.items() if value.get("how") in inbox_read.SUBSTANTIVE_ANSWERS}
        sender = inbox_read.answer_sender()
        new_delivery = []
        for key in ids:
            previous = delivered.get(key)
            saved = records.get(key, {})
            corrected = saved.get("how") == inbox_read.HOURLY_CORRECTION
            if previous and previous.get("note") == answer:
                continue
            if previous and not (corrected and saved.get("note") == answer):
                raise ValueError(f"ask {key} already has a different delivered answer; inspect it before replacing it")
            new_delivery.append(key)
        if ids and not new_delivery:
            when = max((delivered[key]["ts"] for key in ids), key=datetime.fromisoformat)
            if any(records.get(key, {}).get("how") == inbox_read.HOURLY_CORRECTION
                   and records[key].get("note") != answer for key in ids):
                return {"questions": [], "decisions": [], "at": when}
        if how in inbox_read.DELIVERY_ANSWERS:
            incoming = [key for key in new_delivery if records.get(key, {}).get("how") != inbox_read.HOURLY_CORRECTION
                        and not (sender["session"] and records.get(key, {}).get("sender") == sender)]
            incoming_refs = [ref for key in incoming if (ref := ask_state.reference(key, entries))]
            relevant = any(ask_state.overlaps(ref, given) for item in data["questions"].values()
                           if not item.get("withdrawn_at") for ref in ask_state.references(item, entries)
                           for given in incoming_refs)
            text = hourly_state.stamped_answer(answer, when) if relevant else answer
            records.update({key: {"id": key, "note": text, "ts": when} for key in incoming})
        touched = [ref for value in ids if (ref := ask_state.reference(value, entries))]
        keys = dict(decisions or {})
        known = {item.decision for item in lead_decisions.handed_off(handed_off_file)}
        latest = lead_decisions.latest(lead_decisions.records(decisions_file))
        known.update(latest)
        known.update(key for item in data["questions"].values()
                     for key in [item.get("decision") or "", *(item.get("covers") or [])])
        for key in known:
            if ((ref := ask_state.reference(key, entries)) and any(ask_state.overlaps(ref, given) for given in touched)
                    and ask_state.covered(ref, list(records), entries)):
                keys.setdefault(key, answer_text(ref, records, entries))
        for key, text in keys.items():
            if key in latest and latest[key].answer != text and (datetime.fromisoformat(latest[key].decided_at)
                                                                > datetime.fromisoformat(when)):
                raise ValueError(f"{key} has a newer lead decision; the delivered answer was not replayed over it")
        changed = []
        for key, item in data["questions"].items():
            if item.get("withdrawn_at"):
                continue
            refs = ask_state.references(item, entries)
            matched = any(ask_state.overlaps(ref, given) for ref in refs for given in touched)
            covered = matched and all(ask_state.covered(ref, list(records), entries) for ref in refs)
            bindings = item.get("covers") or ([item["decision"]] if item.get("decision") in keys else [])
            source = ask_state.reference(item.get("ask_id") or "", entries)
            if source and source[1]:
                # Completing a whole-ask key must not replace a selected part with combined answers.
                bindings = [value for value in bindings if (ref := ask_state.reference(value, entries)) is None
                            or ref[0] != source[0] or ref[1] and ref[1] <= source[1]]
            if (matched and sender["session"] and sender == item.get("answered_by") and item.get("answer") is not None
                    and how in inbox_read.DELIVERY_ANSWERS):
                continue
            if bindings:
                available = {value: latest[value].answer for value in bindings if value in latest
                             and (datetime.fromisoformat(latest[value].decided_at)
                                  >= datetime.fromisoformat(item["asked_at"]))}
                available.update(keys)
                if not all(value in available for value in bindings) or not any(value in keys for value in bindings):
                    continue
                parts = list(dict.fromkeys(available[value] for value in bindings))
            elif covered:
                parts = list(dict.fromkeys(answer_text(ref, records, entries) for ref in refs))
            else:
                continue
            text = "\n".join(parts) or answer
            changed.append(hourly_state.record_answer(data, hourly_state.Answering(id=key, answer=text, answered_at=when),
                                                      prechecked=bool(refs)))
        new = [lead_decisions.Decision(id=uuid.uuid4().hex, decision=key, answer=text, decided_at=when)
               for key, text in keys.items() if key not in latest or latest[key].answer != text]
        if new:
            lead_decisions.append(decisions_file, *new)
        if changed:
            hourly_state.save(state_file, data)
        if new_delivery:
            inbox_read.record_answers(log, new_delivery, how, answer, at=when)
    return {"questions": [item["id"] for item in changed], "decisions": list(keys), "at": when}
