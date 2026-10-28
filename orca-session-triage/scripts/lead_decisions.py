#!/usr/bin/env python3
"""Record the lead's answers to questions an hourly triage run handed off, and list the open ones.

The lead never types into a scheduled triage tab, so it appends each answer here with `record`.
Every hourly run reads the log, drops answered questions from its handoff, and follows the answers.
Records are only appended; a later record for the same decision replaces the earlier answer.
handoff.py appends each key it hands off to a second log; `handed_off` reads it, so a tool that
writes what a question decides (asana_pr.py) can hold the write while the key has no answer.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sys
import uuid

DEFAULT_LOG = Path("~/.cache/orca-session-triage/lead-decisions.jsonl")
HANDED_OFF_LOG = Path("~/.cache/orca-session-triage/handed-off.jsonl")


@dataclass(frozen=True)
class Decision:
    id: str
    decision: str
    answer: str
    decided_at: str

    @classmethod
    def parse(cls, raw: str, where: str) -> Decision:
        try:
            item = json.loads(raw)
            return cls(id=str(item["id"]), decision=str(item["decision"]), answer=str(item["answer"]),
                       decided_at=str(item["decided_at"]))
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"damaged decision record at {where}: {error}") from error


@dataclass(frozen=True)
class HandOff:
    decision: str
    handed_off_at: str

    @classmethod
    def parse(cls, raw: str, where: str) -> HandOff:
        try:
            item = json.loads(raw)
            return cls(decision=str(item["decision"]), handed_off_at=str(item["handed_off_at"]))
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"damaged handoff record at {where}: {error}") from error

    @classmethod
    def now(cls, decision: str) -> HandOff:
        return cls(decision=decision, handed_off_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def decided_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("--decided-at needs a timezone offset")
    return parsed.isoformat()


def lines(log: Path) -> Iterator[tuple[str, str]]:
    """Each non-empty line of an append-only log, with its place for error messages."""
    if not log.exists():
        return
    with log.open(encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        read = stream.read().splitlines()
    yield from ((raw, f"{log}:{number}") for number, raw in enumerate(read, start=1) if raw.strip())


def records(log: Path) -> Iterator[Decision]:
    yield from (Decision.parse(raw, where) for raw, where in lines(log))


def handed_off(log: Path) -> Iterator[HandOff]:
    yield from (HandOff.parse(raw, where) for raw, where in lines(log))


def latest(decisions: Iterable[Decision]) -> dict[str, Decision]:
    return {item.decision: item for item in decisions}


def append(log: Path, *entries: Decision | HandOff) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.writelines(json.dumps(asdict(entry), ensure_ascii=False) + "\n" for entry in entries)
        stream.flush()


def non_empty(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"empty answer file: {path}")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decisions-file", type=Path, default=DEFAULT_LOG, help="append-only JSONL log")
    commands = parser.add_subparsers(dest="action", required=True)
    record = commands.add_parser("record", help="append the lead's answer to one handed-off question")
    record.add_argument("--decision", required=True,
                        help="the key the hourly handoff gave the question, e.g. remove:ENVIRONMENT::PATH")
    record.add_argument("--answer-file", type=Path, required=True, help="UTF-8 file with the exact answer")
    record.add_argument("--decided-at", help="original ISO 8601 time when recording an earlier answer")
    commands.add_parser("show", help="print the latest answer for each decision")
    unanswered = commands.add_parser("unanswered", help="print the given decisions that have no answer")
    unanswered.add_argument("--decision", action="append", required=True, help="repeat once per question")
    args = parser.parse_args(argv)
    log = args.decisions_file.expanduser()
    try:
        if args.action == "record":
            decision = args.decision.strip()
            if not decision:
                raise ValueError("--decision is empty")
            entry = Decision(id=uuid.uuid4().hex, decision=decision, answer=non_empty(args.answer_file),
                             decided_at=decided_at(args.decided_at))
            append(log, entry)
            result: object = asdict(entry)
        elif args.action == "show":
            result = [asdict(item) for item in latest(records(log)).values()]
        else:
            answered = latest(records(log))
            result = [key for key in dict.fromkeys(args.decision) if key not in answered]
    except (OSError, ValueError) as error:
        print(f"lead decisions: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
