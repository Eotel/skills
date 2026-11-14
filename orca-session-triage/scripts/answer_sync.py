"""Record an answer under the inbox ids and decision keys that name the same ask."""

from __future__ import annotations

from collections.abc import Mapping
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
    keys = {item.decision for report in reports for item in known
            if item.ask_id == report["id"] or re.search(r"(?<![A-Za-z0-9_/.-])" + re.escape(item.decision)
                + r"(?![A-Za-z0-9_/.-])", str(report.get("text") or ""))}
    for path in saved.glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        if any(str(report.get("text") or "").strip() == str(item.get("text") or "").strip() for report in reports):
            keys.update(item.get("decisions") or [])
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
        # Previously answered parts may complete a question when this delivery answers its last part.
        recorded = inbox_read.answer_records(inbox_read.answers_path())
        records = {key: value for key, value in recorded.items() if value.get("how") in ("send", "send --decision")}
        for key in ids:
            if key in records and records[key].get("note") != answer:
                raise ValueError(f"ask {key} already has a different delivered answer; inspect it before replacing it")
        if ids and all(key in records for key in ids):
            when = max(records[key]["ts"] for key in ids)
        records.update({key: {"id": key, "note": answer, "ts": when} for key in ids})
        touched = [ref for value in ids if (ref := ask_state.reference(value, entries))]
        keys = dict(decisions or {})
        known = {item.decision for item in lead_decisions.handed_off(handed_off_file)}
        known.update(lead_decisions.latest(lead_decisions.records(decisions_file)))
        known.update(key for item in data["questions"].values()
                     for key in [item.get("decision") or "", *(item.get("covers") or [])])
        for key in known:
            if ((ref := ask_state.reference(key, entries)) and any(ask_state.overlaps(ref, given) for given in touched)
                    and ask_state.covered(ref, list(records), entries)):
                keys.setdefault(key, answer_text(ref, records, entries))
        changed = []
        for key, item in data["questions"].items():
            if item.get("withdrawn_at"):
                continue
            refs = ask_state.references(item, entries)
            covered = (refs and any(ask_state.overlaps(ref, given) for ref in refs for given in touched)
                       and all(ask_state.covered(ref, list(records), entries) for ref in refs))
            key_answers = [keys[value] for value in item.get("covers") or [] if value in keys]
            if not covered and not (key_answers and len(key_answers) == len(item["covers"])):
                continue
            parts = list(dict.fromkeys(answer_text(ref, records, entries) for ref in refs)) if covered else list(dict.fromkeys(key_answers))
            text = "\n".join(parts) or answer
            changed.append(hourly_state.record_answer(data, hourly_state.Answering(id=key, answer=text, answered_at=when)))
        latest = lead_decisions.latest(lead_decisions.records(decisions_file))
        new = [lead_decisions.Decision(id=uuid.uuid4().hex, decision=key, answer=text, decided_at=when)
               for key, text in keys.items() if key not in latest or latest[key].answer != text]
        if new:
            lead_decisions.append(decisions_file, *new)
        if changed:
            hourly_state.save(state_file, data)
        if ids:
            existing = {key for key, value in recorded.items() if value.get("how") in ("send", "send --decision")}
            inbox_read.record_answers(inbox_read.answers_path(), [key for key in ids if key not in existing],
                                      how, answer, at=when)
    return {"questions": [item["id"] for item in changed], "decisions": list(keys), "at": when}
