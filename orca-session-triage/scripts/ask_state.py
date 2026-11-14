"""Bindings between inbox asks, hourly questions, and decision keys; shared asking status."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
import re

import inbox_read


ASK_KEY = re.compile(r"(?:ask|answer):.*::(?:ask-)?([^:]+)(?::(\d+(?:\+\d+)*))?$")


def reference(value: str, entries: list[dict]) -> tuple[str, frozenset[int]] | None:
    """Resolve a full or abbreviated ask id, optionally wrapped in an hourly decision key."""
    match = ASK_KEY.fullmatch(value)
    wanted, _, part = value.partition(":") if match is None else (match[1], ":", match[2] or "")
    if part and not re.fullmatch(r"\d+(?:\+\d+)*", part):
        return None
    found = [str(item["id"]) for item in entries if item.get("kind") == "ask" and item.get("id")
             and inbox_read.names(str(item["id"]), wanted)]
    if len(set(found)) != 1:
        return None
    return found[0], frozenset(int(number) for number in part.split("+")) if part else frozenset()


def references(question: dict, entries: list[dict]) -> list[tuple[str, frozenset[int]]]:
    values = [question.get("decision") or "", *(question.get("covers") or [])]
    # ask_id identifies the source; a numbered decision narrows that source to its questions.
    found = [ref for value in values if (ref := reference(value, entries))]
    if not found and question.get("ask_id") and (ref := reference(question["ask_id"], entries)):
        found.append(ref)
    return list(dict.fromkeys(found))


def covered(ref: tuple[str, frozenset[int]], ids: list[str], entries: list[dict]) -> bool:
    """Whether the supplied answers cover this reference, including all numbered parts of a whole ask."""
    ask_id, numbers = ref
    if ask_id in ids:
        return True
    if not numbers:
        ask = next(item for item in entries if item.get("id") == ask_id)
        numbers = frozenset(range(1, len(inbox_read.questions_of(ask)) + 1))
    return bool(numbers) and all(f"{ask_id}:{number}" in ids for number in numbers)


def overlaps(left: tuple[str, frozenset[int]], right: tuple[str, frozenset[int]]) -> bool:
    return left[0] == right[0] and (not left[1] or not right[1] or bool(left[1] & right[1]))


def active(state: dict, decisions: Mapping[str, object] | None = None) -> list[dict]:
    """Open hourly questions with the workspace, time, row and every decision key being asked."""
    return [{"id": key, "by": item.get("asked_in") or "", "at": item.get("asked_at"),
             "row": item.get("row"), "keys": list(dict.fromkeys([item.get("decision"), *(item.get("covers") or [])])),
             "question": item.get("question") or "", "session": item.get("session"), "record": item}
            for key, item in state.get("questions", {}).items()
            if item.get("answer") is None and not item.get("withdrawn_at")
            and not (item.get("covers") and decisions and all(
                decision in decisions and datetime.fromisoformat(getattr(decisions[decision], "decided_at"))
                > datetime.fromisoformat(item["asked_at"])
                for decision in item["covers"]))]


def view(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "record"}


def for_row(state: dict, row: str, decisions: Mapping[str, object] | None = None) -> list[dict]:
    return [view(item) for item in active(state, decisions) if item["row"] == row]


def for_key(state: dict, key: str, entries: list[dict]) -> list[dict]:
    ref = reference(key, entries)
    return [view(item) for item in active(state) if key in item["keys"] or ref and any(
        other[0] == ref[0] and (not other[1] or not ref[1] or bool(other[1] & ref[1]))
        for other in references(item["record"], entries))]


def same_workspace(by: str, cwd: str) -> bool:
    return by == cwd or (bool(by) and (by == Path(cwd).name or "::" not in by and Path(by).resolve() == Path(cwd).resolve()))
