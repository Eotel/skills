#!/usr/bin/env python3
"""Record the lead's answers to questions an hourly triage run handed off, and list the open ones.

The lead never types into a scheduled triage tab, so it appends each answer here with `record`,
or several at once with `record-many` (JSON Lines of {"decision", "answer", "decided_at"?}: every
line is checked first, and nothing is written while one is wrong).
Every hourly run reads the log, drops answered questions from its handoff, and follows the answers.
Records are only appended; a later record for the same decision replaces the earlier answer.
handoff.py appends each key it hands off to a second log; `handed_off` reads it, so a tool that
writes what a question decides (asana_pr.py) can hold the write while the key has no answer.
`unanswered` without --decision lists every handed-off key with no answer and when it was last
handed off; for a key naming PRs (`merge:owner/repo#12+#14`) it adds each PR's state, updatedAt,
and whether it was updated after that hand-off, read with one GitHub query per repository.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import sys
from typing import Protocol
import uuid

DEFAULT_LOG = Path("~/.cache/orca-session-triage/lead-decisions.jsonl")
HANDED_OFF_LOG = Path("~/.cache/orca-session-triage/handed-off.jsonl")
# A PR a decision key names: `owner/repo#12`, or several of one repository as `owner/repo#12+#14`.
PR_REFS = re.compile(r"(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+)#(?P<numbers>\d+(?:\+#?\d+)*)")
ANSWER_KEYS = frozenset(("decision", "answer", "decided_at"))


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


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int


@dataclass(frozen=True)
class PrState:
    state: str | None
    updated_at: str | None


class PrStates(Protocol):
    """The state and last update of some PRs of one repository, by number; None when it cannot be read."""

    def __call__(self, repo: str, numbers: list[int], /) -> Mapping[int, PrState] | None: ...


@dataclass(frozen=True)
class BadLine:
    number: int
    reason: str


def decided_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"the time {value} needs a timezone offset")
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


def answer_line(raw: str) -> Decision:
    """A `record-many` line as the record to append; ValueError says what is wrong with it."""
    try:
        item = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"not JSON: {error}") from error
    if not isinstance(item, dict):
        raise ValueError("not a JSON object")
    unknown = sorted(set(item) - ANSWER_KEYS)
    if unknown:
        raise ValueError("unknown keys: " + ", ".join(unknown))
    texts = {key: item.get(key) for key in ("decision", "answer")}
    empty = [key for key, value in texts.items() if not isinstance(value, str) or not value.strip()]
    if empty:
        raise ValueError(" and ".join(empty) + " must be non-empty text")
    when = item.get("decided_at")
    if when is not None and not isinstance(when, str):
        raise ValueError("decided_at must be an ISO 8601 time with its timezone offset")
    return Decision(id=uuid.uuid4().hex, decision=str(texts["decision"]).strip(),
                    answer=str(texts["answer"]).strip(), decided_at=decided_at(when))


def checked_line(number: int, raw: str) -> Decision | BadLine:
    try:
        return answer_line(raw)
    except ValueError as error:
        return BadLine(number, str(error))


def answers(text: str) -> tuple[list[Decision], list[BadLine]]:
    """The records JSON Lines give, and every line that cannot be one (blank lines are skipped)."""
    checked = [checked_line(number, raw) for number, raw in enumerate(text.splitlines(), start=1) if raw.strip()]
    return ([item for item in checked if isinstance(item, Decision)],
            [item for item in checked if isinstance(item, BadLine)])


def pull_requests(key: str) -> list[PullRequest]:
    return [PullRequest(match["repo"], int(number)) for match in PR_REFS.finditer(key)
            for number in re.findall(r"\d+", match["numbers"])]


def github_pr_states(repo: str, numbers: list[int]) -> Mapping[int, PrState] | None:
    """PrStates read from GitHub with scan_sessions' query, one GraphQL query per GRAPHQL_CHUNK PRs.

    A repository gh cannot read gives None, and scan_sessions names it on stderr; a number GitHub has no
    PR for (an issue, or a typo in the key) is left out and named on stderr.
    """
    # Imported here: tools that import this module only to read its logs (asana_pr.py) do not load the scan.
    import scan_sessions

    owner, _, name = repo.partition("/")
    refs: list[scan_sessions.PrRef] = list(numbers)
    queries = [scan_sessions.pr_query(owner, name, refs[start:start + scan_sessions.GRAPHQL_CHUNK])
               for start in range(0, len(refs), scan_sessions.GRAPHQL_CHUNK)]
    read = [(scan_sessions.graphql_repository(repo, query), aliases) for query, aliases in queries]
    if any(repository is None for repository, _ in read):
        return None
    found = {int(ref): PrState(node.get("state"), node.get("updatedAt"))
             for repository, aliases in read for alias, ref in aliases.items()
             if isinstance(node := (repository or {}).get(alias), dict)}
    missing = [f"#{number}" for number in numbers if number not in found]
    if missing:
        print(f"GitHub has no PR {' '.join(missing)} in {repo}, so its fields are null", file=sys.stderr)
    return found


def moment(value: str | None) -> datetime | None:
    """An aware time from ISO 8601 (GitHub's trailing Z included); one without a zone is UTC."""
    try:
        parsed = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def pr_view(pr: PullRequest, states: Mapping[int, PrState] | None, handed_off_at: str) -> dict[str, object]:
    found = (states or {}).get(pr.number)
    state, updated_at = (found.state, found.updated_at) if found else (None, None)
    updated, handed = moment(updated_at), moment(handed_off_at)
    return {"repo": pr.repo, "number": pr.number, "state": state, "updatedAt": updated_at,
            "updated_after_handoff": updated > handed if updated and handed else None}


def open_handoffs(handed: Iterable[HandOff], answered: Mapping[str, Decision],
                  read_prs: PrStates) -> list[dict[str, object]]:
    """Every handed-off key without an answer, in the order first handed off, with its last hand-off time
    and the PRs it names."""
    last = {item.decision: item for item in handed if item.decision not in answered}
    named = {key: pull_requests(key) for key in last}
    prs = list(dict.fromkeys(pr for found in named.values() for pr in found))
    wanted = {repo: [pr.number for pr in prs if pr.repo == repo] for repo in dict.fromkeys(pr.repo for pr in prs)}
    states = {repo: read_prs(repo, numbers) for repo, numbers in wanted.items()}
    return [{"decision": key, "handed_off_at": item.handed_off_at,
             "prs": [pr_view(pr, states[pr.repo], item.handed_off_at) for pr in named[key]]}
            for key, item in last.items()]


def main(argv: list[str] | None = None, read_prs: PrStates = github_pr_states) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--decisions-file", type=Path, default=DEFAULT_LOG, help="append-only JSONL log")
    commands = parser.add_subparsers(dest="action", required=True)
    record = commands.add_parser("record", help="append the lead's answer to one handed-off question")
    record.add_argument("--decision", required=True,
                        help="the key the hourly handoff gave the question, e.g. remove:ENVIRONMENT::PATH")
    record.add_argument("--answer-file", type=Path, required=True, help="UTF-8 file with the exact answer")
    record.add_argument("--decided-at", help="original ISO 8601 time when recording an earlier answer")
    many = commands.add_parser(
        "record-many", help='append several answers from JSON Lines of {"decision": KEY, "answer": TEXT, '
                            '"decided_at": ISO time with offset (optional)}; a wrong line writes nothing')
    many.add_argument("--file", type=Path, help="UTF-8 JSON Lines file (default: standard input)")
    commands.add_parser("show", help="print the latest answer for each decision")
    unanswered = commands.add_parser(
        "unanswered", help="print the given decisions that have no answer; without --decision, every handed-off "
                           "key with no answer, when it was last handed off, and the state of the PRs it names")
    unanswered.add_argument("--decision", action="append", help="repeat once per question")
    unanswered.add_argument("--handed-off-file", type=Path, default=HANDED_OFF_LOG,
                            help="the keys handoff.py handed off (default: %(default)s)")
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
        elif args.action == "record-many":
            given, bad = answers(args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read())
            if bad or not given:
                raise ValueError("nothing was recorded" + ("".join(f"\nline {item.number}: {item.reason}"
                                                                   for item in bad) or ": no answers were given"))
            append(log, *given)
            result = [asdict(entry) for entry in given]
        elif args.action == "show":
            result = [asdict(item) for item in latest(records(log)).values()]
        elif args.decision:
            answered = latest(records(log))
            result = [key for key in dict.fromkeys(args.decision) if key not in answered]
        else:
            result = open_handoffs(handed_off(args.handed_off_file.expanduser()), latest(records(log)), read_prs)
    except (OSError, ValueError) as error:
        print(f"lead decisions: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
