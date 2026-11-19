"""Bindings between inbox asks, hourly questions, and decision keys; shared asking status."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def references(question: dict, entries: list[dict], *,
               bindings: Mapping[str, str] | None = None) -> list[tuple[str, frozenset[int]]]:
    values = [question.get("decision") or "", *(question.get("covers") or [])]
    # A whole source id leaves numbered decision keys responsible for narrowing it.
    found = []
    for value in values:
        target = (bindings or {}).get(value)
        if not same_bound_ask(question, target, value):
            continue
        ref = handoff_ref(target, value, entries, (question,)) if target else reference(value, entries)
        if ref:
            found.append(ref)
    source = reference(question.get("ask_id") or "", entries)
    if source and source[1]:
        # A whole-ask key would widen an explicit part back to every question.
        found = [source, *(ref for ref in found if ref[0] != source[0])]
    elif source and not any(ref[0] == source[0] for ref in found):
        found.append(source)
    return list(dict.fromkeys(found))


def handoff_ref(ask_id: str | None, key: str, entries: list[dict],
                questions: Iterable[dict] = ()) -> tuple[str, frozenset[int]] | None:
    explicit = reference(ask_id, entries) if ask_id else None
    if ask_id and explicit is None:
        return None
    named = reference(key, entries)
    if explicit and not explicit[1]:
        if named and explicit[0] == named[0]:
            return named
        matched = {numbers for item in questions if key in (item.get("decision"), *(item.get("covers") or []))
                   and not item.get("withdrawn_at")
                   if (numbers := frozenset(number for ref in references(item, entries) if ref[0] == explicit[0]
                                            for number in ref[1]))}
        if len(matched) == 1:
            return explicit[0], matched.pop()
    return explicit or named


def registered_ask_id(question: dict, key: str) -> str | None:
    if question.get("decision") == key or question.get("covers") == [key]:
        return question.get("ask_id")
    return None


def matching_ref(target: str, source: str) -> bool:
    new, _, new_part = target.partition(":")
    old, _, old_part = source.partition(":")
    same = inbox_read.names(new, old) or inbox_read.names(old, new)
    return same and (not new_part or not old_part or bool(set(new_part.split("+")) & set(old_part.split("+"))))


def same_bound_ask(question: dict, ask_id: str | None, key: str = "") -> bool:
    if not ask_id:
        return True
    registered = question.get("ask_id")
    named = ASK_KEY.fullmatch(key)
    if not named:
        return not registered or matching_ref(ask_id, registered)
    named_id, part = named.groups()
    named_ref = f"{named_id}:{part}" if part else named_id
    if not registered or part and ":" not in registered and matching_ref(registered, named_id):
        return matching_ref(ask_id, named_ref)
    return matching_ref(ask_id, registered) or (question.get("decision") != key and matching_ref(ask_id, named_ref))


def handoff_target(key: str, ask_id: str | None, entries: list[dict],
                   questions: Iterable[dict]) -> tuple[str, frozenset[int]] | None:
    named = reference(key, entries)
    bindings = {ask_id} if ask_id else {source for question in questions if not question.get("withdrawn_at")
                                        if (source := registered_ask_id(question, key))
                                        and (not named or same_bound_ask(question, named[0]))}
    refs = {handoff_ref(source, key, entries, questions) for source in (bindings or {None})}
    return next(iter(refs)) if len(refs) == 1 else None


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


def active_keys(item: dict, bindings: Mapping[str, str | None]) -> list[str]:
    return [key for key in item["keys"] if key and same_bound_ask(item["record"], bindings.get(key), key)]


def asking(state: dict, decisions: Mapping[str, object] | None, bindings: Mapping[str, str | None], *,
           row: str | None = None) -> list[dict]:
    return [{**view(item), "keys": keys} for item in active(state, decisions)
            if (row is None or item["row"] == row) and (keys := active_keys(item, bindings))]


def for_key(state: dict, key: str, entries: list[dict], bindings: Mapping[str, str | None]) -> list[dict]:
    ref = reference(key, entries)
    return [{**view(item), "keys": keys} for item in active(state)
            if (keys := active_keys(item, bindings)) and same_bound_ask(item["record"], bindings.get(key), key)
            and (key in keys or ref and any(overlaps(ref, other) for other in references(item["record"], entries)))]


def same_workspace(by: str, cwd: str) -> bool:
    return by == cwd or (bool(by) and (by == Path(cwd).name or "::" not in by and Path(by).resolve() == Path(cwd).resolve()))
