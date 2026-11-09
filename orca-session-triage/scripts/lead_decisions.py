#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Record the lead's answers to questions an hourly triage run handed off, and list the open ones.

The lead never types into a scheduled triage tab, so it appends each answer here with `record`,
or several at once with `record-many` (JSON Lines of {"decision", "answer", "decided_at"?}: every
line is checked first, and nothing is written while one is wrong).
Every hourly run reads the log, drops answered questions from its handoff, and follows the answers.
Records are only appended; a later record for the same decision replaces the earlier answer, and
`show --history` also prints the answers it replaced, with their times. `record` and `record-many`
warn on stderr, and still record, when a text that only says the answer was passed on
(「中継済み（…）」) replaces an answer.
handoff.py appends each key it hands off to a second log; `handed_off` reads it, so a tool that
writes what a question decides (asana_pr.py) can hold the write while the key has no answer.
`unanswered` prints open keys and settled keys with their answer records. Open keys carry their
first and last hand-off times and count; a bound inbox ask's answer also settles a key.
For a key naming PRs (`merge:owner/repo#12+#14`) it adds each PR's state, updatedAt, whether it
was updated after that hand-off, named inbox reports, and values from --asana-status when given.
GitHub is read with one query per repository. `show --match` and `--since-last-handoff` print
one line per matching answer, newest first in JST, marking remove keys whose paths still exist.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import re
import sys
from typing import Protocol
import uuid

import inbox_read

DEFAULT_LOG = Path("~/.cache/orca-session-triage/lead-decisions.jsonl")
HANDED_OFF_LOG = Path("~/.cache/orca-session-triage/handed-off.jsonl")
# A PR a decision key names: `owner/repo#12`, or several of one repository as `owner/repo#12+#14`.
PR_REFS = re.compile(r"(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+)#(?P<numbers>\d+(?:\+#?\d+)*)")
ANSWER_KEYS = frozenset(("decision", "answer", "decided_at"))
JST = timezone(timedelta(hours=9))
EXCERPT = 160
# A record that only says an answer was passed on decides nothing: on 2026-11-06 「中継済み（12:2x send
# montly-report）」 hid 「1,000 万円のまま」, recorded under the same key the hour before.
DELIVERY_ONLY = re.compile(r"(?:(?:中継|送達|送信|転送)済み|中継した|送った|伝えた)(?:[（(][^）)]*[）)])?[。.]?")


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
    ask_id: str | None = None

    @classmethod
    def parse(cls, raw: str, where: str) -> HandOff:
        try:
            item = json.loads(raw)
            return cls(decision=str(item["decision"]), handed_off_at=str(item["handed_off_at"]),
                       ask_id=item.get("ask_id"))
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"damaged handoff record at {where}: {error}") from error

    @classmethod
    def now(cls, decision: str, ask_id: str | None = None) -> HandOff:
        return cls(decision=decision, handed_off_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   ask_id=ask_id)


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int


@dataclass(frozen=True)
class PrState:
    state: str | None
    updated_at: str | None
    head: str | None = None


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


def history(decisions: Iterable[Decision]) -> dict[str, list[Decision]]:
    """Every record of each key in log order: the last is its answer, the earlier ones the answers it replaced."""
    found: dict[str, list[Decision]] = {}
    for item in decisions:
        found.setdefault(item.decision, []).append(item)
    return found


def delivery_only(answer: str) -> bool:
    return DELIVERY_ONLY.fullmatch(" ".join(answer.split())) is not None


def substance(items: list[Decision]) -> Decision:
    """A key's newest record that decides something, past later ones that only say it was passed on."""
    return next((item for item in reversed(items) if not delivery_only(item.answer)), items[-1])


def hidden_answers(known: dict[str, list[Decision]], entries: Iterable[Decision]) -> list[str]:
    """A warning for each entry that only says an answer was passed on, replacing that answer of its key."""
    warnings = []
    for entry in entries:
        earlier = known.setdefault(entry.decision, [])
        if delivery_only(entry.answer) and earlier and not delivery_only((answer := substance(earlier)).answer):
            warnings.append(f"lead decisions: warning: {entry.decision}: {entry.answer!r} only says the answer was "
                            f"passed on, and replaces the answer of {answer.decided_at}: "
                            f"{' '.join(answer.answer.split())[:EXCERPT]} (`show --history` still prints it)")
        earlier.append(entry)
    return warnings


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


def github_pr_states(repo: str, numbers: list[int], *, include_issues: bool = False) -> Mapping[int, PrState] | None:
    """PrStates read from GitHub with scan_sessions' query, one GraphQL query per GRAPHQL_CHUNK PRs.

    A repository gh cannot read gives None, and scan_sessions names it on stderr; a number GitHub has no
    PR for (an issue, or a typo in the key) is left out and named on stderr.
    """
    # Imported here: tools that import this module only to read its logs (asana_pr.py) do not load the scan.
    import scan_sessions

    owner, _, name = repo.partition("/")
    refs: list[scan_sessions.PrRef] = [scan_sessions.Item(number) if include_issues else number for number in numbers]
    queries = [scan_sessions.pr_query(owner, name, refs[start:start + scan_sessions.GRAPHQL_CHUNK])
               for start in range(0, len(refs), scan_sessions.GRAPHQL_CHUNK)]
    read = [(scan_sessions.graphql_repository(repo, query), aliases) for query, aliases in queries]
    if any(repository is None for repository, _ in read):
        return None
    found = {(ref.number if isinstance(ref, scan_sessions.Item) else int(ref)):
             PrState(node.get("state"), node.get("updatedAt"), node.get("headRefOid"))
             for repository, aliases in read for alias, ref in aliases.items()
             if isinstance(ref, (int, scan_sessions.Item)) and isinstance(node := (repository or {}).get(alias), dict)}
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


def decision_reports(key: str, prs: list[PullRequest], handed_off_at: str | None,
                     entries: list[dict]) -> dict[str, list[dict]]:
    names = [key, *(f"{pr.repo}#{pr.number}" for pr in prs),
             *(f"https://github.com/{pr.repo}/pull/{pr.number}" for pr in prs)]
    pattern = re.compile(r"(?<![A-Za-z0-9_/.-])(?:" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_/#])")
    reports: dict[str, list[dict]] = {"before": [], "after": []}
    handed = moment(handed_off_at)
    for item in entries:
        text = str(item.get("text") or "")
        if not pattern.search(text):
            continue
        when = moment(item.get("ts"))
        side = "after" if when and handed and when >= handed else "before"
        reports[side].append({**{field: item.get(field) for field in ("id", "ts", "kind", "worktree")},
                              "text": " ".join(text.split())[:EXCERPT]})
    return reports


def ask_answers(path: Path) -> dict[str, dict]:
    """The same answered ids open-asks uses, with their complete answer records."""
    ids = inbox_read.answered_ids(path)
    found = {}
    if ids:
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] in ids:
                found[item["id"]] = item
    return found


def ask_answer(ask_id: str, answers: Mapping[str, dict], asks: Mapping[str, dict]) -> dict | None:
    """The answer covering an ask as open-asks reads it: a record for `<id>` (for `<id>:<n>`, also one for the whole
    ask), or one for every question of a structured ask."""
    whole, _, number = ask_id.partition(":")
    if found := answers.get(ask_id) or (answers.get(whole) if number else None):
        return found
    questions = inbox_read.questions_of(asks.get(ask_id) or {})
    parts = [answers.get(f"{ask_id}:{index}") for index in range(1, len(questions) + 1)]
    return {"id": ask_id, "answers": parts} if questions and all(parts) else None


def open_handoffs(handed: Iterable[HandOff], answered: Mapping[str, Decision], read_prs: PrStates,
                  answers: Mapping[str, dict], entries: list[dict], asana: Mapping[str, object],
                  decisions: list[str] | None = None) -> dict[str, list[dict]]:
    """Open keys with their hand-off history and PR facts, and settled keys with the answering record."""
    history: dict[str, list[HandOff]] = {}
    for item in handed:
        history.setdefault(item.decision, []).append(item)
    keys = list(dict.fromkeys(decisions)) if decisions else list(history)
    asks = {str(item["id"]): item for item in entries if item.get("kind") == "ask" and item.get("id")}
    settled = []
    last = {}
    for key in keys:
        item = history[key][-1] if key in history else None
        answer = (asdict(answered[key]) if key in answered else
                  next((found for entry in reversed(history.get(key, []))
                        if entry.ask_id and (found := ask_answer(entry.ask_id, answers, asks))), None))
        if answer is not None:
            settled.append({"decision": key, "answer": answer})
        else:
            last[key] = item
    named = {key: pull_requests(key) for key in last}
    prs = list(dict.fromkeys(pr for found in named.values() for pr in found))
    wanted = {repo: [pr.number for pr in prs if pr.repo == repo] for repo in dict.fromkeys(pr.repo for pr in prs)}
    states = {repo: read_prs(repo, numbers) for repo, numbers in wanted.items()}
    unanswered = []
    for key, item in last.items():
        at = item.handed_off_at if item else None
        views = [pr_view(pr, states[pr.repo], at or "") for pr in named[key]]
        for pr, view in zip(named[key], views):
            url = f"https://github.com/{pr.repo}/pull/{pr.number}"
            if url in asana:
                view["asana"] = asana[url]
        row = {"decision": key, "handed_off_at": at,
               "first_handed_off_at": history[key][0].handed_off_at if item else None,
               "handoff_count": len(history.get(key, [])), "prs": views}
        if named[key]:
            row["reports"] = decision_reports(key, named[key], at, entries)
        unanswered.append(row)
    return {"unanswered": unanswered, "settled": settled}


def show_answers(items: Iterable[Decision], words: list[str], since: datetime | None,
                 replaced: frozenset[str] = frozenset()) -> list[str]:
    """One line per matching answer, newest first, with times in JST, marking the records `replaced` names."""
    selected = []
    for item in items:
        when = moment(item.decided_at)
        if words and not any(word.casefold() in item.decision.casefold() or word.casefold() in item.answer.casefold()
                             for word in words):
            continue
        if since is None or (when is not None and when >= since):
            selected.append((when, item))
    selected.sort(key=lambda pair: pair[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    shown = []
    for when, item in selected:
        stamp = when.astimezone(JST).isoformat(timespec="seconds") if when else item.decided_at
        path = item.decision.removeprefix("remove:").split("::", 1)[-1]
        pending = " 未実行" if item.decision.startswith("remove:") and Path(path).expanduser().exists() else ""
        pending += " 置き換え済み" if item.id in replaced else ""
        shown.append(f"{stamp} {item.decision}{pending}  {' '.join(item.answer.split())[:EXCERPT]}")
    return shown


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
    show = commands.add_parser("show", help="print the latest answer for each decision")
    show.add_argument("--match", action="append", default=[], help="word in the key or answer, ignoring case; repeat per word")
    show.add_argument("--since-last-handoff", action="store_true",
                      help="answers since the newest saved handoff, newest first in JST")
    show.add_argument("--history", action="store_true",
                      help="also print the answers a later record of the same key replaced, with their times: "
                           "under `replaced` in the JSON, marked 置き換え済み in the lines")
    # Imported here so log readers need not load the handoff or scan modules.
    import handoff
    show.add_argument("--handoffs-dir", type=Path, default=handoff.HANDOFFS,
                      help="saved handoffs for --since-last-handoff (default: %(default)s)")
    unanswered = commands.add_parser(
        "unanswered", help="print unanswered keys with hand-off history and PR facts, and settled keys with answers; "
                           "without --decision, include every handed-off key")
    unanswered.add_argument("--decision", action="append", help="repeat once per question")
    unanswered.add_argument("--handed-off-file", type=Path, default=HANDED_OFF_LOG,
                            help="the keys handoff.py handed off (default: %(default)s)")
    unanswered.add_argument("--asana-status", type=Path, help="OUT_JSON already written by asana99_status.py")
    unanswered.add_argument("--summary", action="store_true",
                            help="one line per unanswered key, followed by the settled count")
    args = parser.parse_args(argv)
    log = args.decisions_file.expanduser()
    warnings: list[str] = []
    try:
        if args.action == "record":
            decision = args.decision.strip()
            if not decision:
                raise ValueError("--decision is empty")
            entry = Decision(id=uuid.uuid4().hex, decision=decision, answer=non_empty(args.answer_file),
                             decided_at=decided_at(args.decided_at))
            warnings = hidden_answers(history(records(log)), [entry])
            append(log, entry)
            result: object = asdict(entry)
        elif args.action == "record-many":
            given, bad = answers(args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read())
            if bad or not given:
                raise ValueError("nothing was recorded" + ("".join(f"\nline {item.number}: {item.reason}"
                                                                   for item in bad) or ": no answers were given"))
            warnings = hidden_answers(history(records(log)), given)
            append(log, *given)
            result = [asdict(entry) for entry in given]
        elif args.action == "show":
            keys = history(records(log))
            items = [item for found in keys.values() for item in (found if args.history else found[-1:])]
            if args.match or args.since_last_handoff:
                since = None
                if args.since_last_handoff:
                    _, saved = handoff.SavedHandoff.newest(args.handoffs_dir.expanduser())
                    since = moment(saved.sent_at)
                    if since is None:
                        raise ValueError(f"invalid handoff sent_at: {saved.sent_at}")
                replaced = frozenset(item.id for found in keys.values() for item in found[:-1])
                print("\n".join(show_answers(items, args.match, since, replaced)))
                return 0
            result = [{**asdict(found[-1]), "replaced": [asdict(item) for item in found[:-1]]} if args.history
                      else asdict(found[-1]) for found in keys.values()]
        else:
            inbox = inbox_read.paths()[0].expanduser()
            entries = inbox_read.inbox_entries(inbox) if inbox.exists() else []
            asana = ({item["url"]: item["asana"] for item in
                      json.loads(args.asana_status.expanduser().read_text(encoding="utf-8"))} if args.asana_status else {})
            result = open_handoffs(handed_off(args.handed_off_file.expanduser()), latest(records(log)), read_prs,
                                   ask_answers(inbox_read.answers_path().expanduser()), entries, asana, args.decision)
            if args.summary:
                for item in result["unanswered"]:
                    print(item["decision"])
                print(f"settled: {len(result['settled'])}")
                return 0
    except (OSError, ValueError, LookupError) as error:
        print(f"lead decisions: {error}", file=sys.stderr)
        return 1
    if warnings:
        print(*warnings, sep="\n", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
