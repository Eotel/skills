#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Read new triage inbox lines by append position, then acknowledge handled lines.

`read` does not advance the cursor. After handling its output, pass the printed
byte position to `ack`; only a read without --kind, --source, --id, --since, or --text
prints it, so an entry a filter hid is never acknowledged. `read --text WORD` searches every
entry's whole text, before or after the cursor, and prints each match's header with the lines
holding WORD. `initialize` starts a new cursor at the current end only after the lead has
handled all existing inbox entries.
`watch` is the lead's Monitor: it prints every new ask and done, advances its own
cursor, and names each ask left unanswered for 15 minutes.

Being read and being answered differ. `answered` records an ask (or one of its
questions, `<ask id>:<n>`) as answered in ~/.cache/orca-briefs/ask-answers.jsonl
($TRIAGE_ASK_ANSWERS), as `send --decision --ask` does; `open-asks` lists the asks
not answered yet, oldest first, after recording answered each board adoption notice
(「採用 triage の採用に担当を立ててください。」) whose proposals adoption-triage's ledger
already has a lane for, and `questions` prints their structured questions
(`triage-notify --kind ask --questions`) four at a time as AskUserQuestion input.
`answered --before` marks only the asks an answer proves answered, every open question
of them (answer_evidence.py: the lead's decisions, AskUserQuestion answers, delivered
answers), and names the rest with the related answers to check, without marking them.

Being asked is a third state. `claim <id>[:<n>] --by <worktree>` records, before the
worktree asks the user, that it is asking those asks (or questions) now, in
~/.cache/orca-briefs/ask-claims.jsonl ($TRIAGE_ASK_CLAIMS), apart from the answers. `open-asks`
marks a claimed ask 伺い中 with who and since when, `watch` prints each claim once and
leaves its ask out of the reminders, and `questions` leaves claimed questions out of
the batch and lists them under `claimed`, so another tab does not ask them again.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import fcntl
from itertools import takewhile
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from typing import Literal, NamedTuple, cast

import answer_evidence
import hourly_runtime


ReportKind = Literal["ask", "done", "info"]
REPORT_KINDS: frozenset[str] = frozenset(("ask", "done", "info"))
WATCHED_KINDS = frozenset(("ask", "done"))
ANSWERS = Path.home() / ".cache/orca-briefs/ask-answers.jsonl"
CLAIMS = Path.home() / ".cache/orca-briefs/ask-claims.jsonl"
REMIND_AFTER_MINUTES = 15
ID_PREFIX_MIN = 8
# AskUserQuestion takes one to four questions per call.
QUESTIONS_PER_BATCH = 4
RECOMMENDED_MARK = "（推奨）"
# A source no entry has names at most this many near ones, alike at least this much (difflib's ratio).
NEAR_SOURCES = 5
NEAR_LIKENESS = 0.6
# orca-board's adoption notice: its first line, and each proposal it asks a lane for.
NOTICE = "採用 triage の採用に担当を立ててください。"
NOTICE_ID = re.compile(r"^案の ID: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.MULTILINE)
LEDGER = "~/.agents/skills/adoption-triage/scripts/ledger.py"


@dataclass(frozen=True)
class OpenAsk:
    report: dict
    # Numbers of the questions still unanswered; empty for an ask without structured questions.
    open_questions: tuple[int, ...]
    at: datetime | None


@dataclass(frozen=True)
class Claim:
    """A worktree asking the user an ask (`<id>`) or one of its questions (`<id>:<n>`) now."""
    id: str
    by: str
    at: str
    inactive_minutes: int | None = None

    def view(self) -> dict:
        return {"by": self.by, "at": self.at,
                **({"inactive_minutes": self.inactive_minutes} if self.inactive_minutes is not None else {})}


@dataclass(frozen=True)
class PendingQuestion:
    id: str
    report: dict
    question: dict


@dataclass(frozen=True)
class Sender:
    """Where a report came from, as triage-notify records it: the worktree's name and the Mac."""
    worktree: str
    host: str

    @classmethod
    def of(cls, workspace: Path) -> Sender:
        """This Mac's sender for a workspace, named as triage-notify names it ($TRIAGE_HOST or the hostname)."""
        return cls(worktree=workspace.name, host=os.environ.get("TRIAGE_HOST") or socket.gethostname())

    def sent(self, item: dict) -> bool:
        """Whether this sender wrote the report; an entry that records no host matches on its worktree."""
        return item.get("worktree") == self.worktree and item.get("host") in (None, "", self.host)


class Placed(NamedTuple):
    """An inbox entry with the offset after it and whether the cursor has passed it."""
    end: int
    acknowledged: bool
    item: dict


@dataclass(frozen=True)
class SourceFilter:
    """`read --source`: the value given, and the sources it names."""
    given: str
    names: frozenset[str]

    @classmethod
    def of(cls, given: str, entries: list[dict]) -> SourceFilter:
        """A worktree name alone (as backlog-watch prints it) also names its repo/worktree when one repo has it."""
        repos = {source_of(item) for item in entries if item.get("repo") and item.get("worktree") == given}
        return cls(given, frozenset((given, *repos)) if len(repos) == 1 else frozenset((given,)))


@dataclass(frozen=True)
class ReadOptions:
    brief: bool = False
    kinds: frozenset[ReportKind] | None = None
    source: SourceFilter | None = None
    summary: bool = False
    ids: frozenset[str] | None = None
    since: datetime | None = None
    text: str | None = None

    @property
    def filtered(self) -> bool:
        return (self.kinds is not None or self.source is not None or self.ids is not None
                or self.since is not None or self.text is not None)


def report_kinds(value: str) -> frozenset[ReportKind]:
    kinds = frozenset(kind.strip() for kind in value.split(","))
    if "" in kinds or not kinds <= REPORT_KINDS:
        raise argparse.ArgumentTypeError("--kind must contain ask, done, or info")
    return cast(frozenset[ReportKind], kinds)


def entry_ids(value: str) -> frozenset[str]:
    ids = frozenset(part.strip() for part in value.split(","))
    if "" in ids:
        raise argparse.ArgumentTypeError("--id must be comma-separated entry ids")
    return ids


def since_time(value: str) -> datetime:
    """An ISO time or the most recent local HH:MM, as `inbox --since` accepts."""
    now = datetime.now().astimezone()
    try:
        if len(value) == 5 and value[2] == ":":
            hour, minute = (int(part) for part in value.split(":"))
            when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return when - timedelta(days=1) if when > now else when
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return when if when.tzinfo else when.astimezone()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--since must be HH:MM or an ISO time") from exc


def paths() -> tuple[Path, Path]:
    inbox = Path(os.environ.get("TRIAGE_INBOX") or Path.home() / ".cache/orca-briefs/triage-inbox.jsonl")
    cursor = Path(os.environ.get("TRIAGE_READ_OFFSET") or Path.home() / ".cache/orca-session-triage/inbox-read.offset")
    return inbox, cursor


def answers_path() -> Path:
    return Path(os.environ.get("TRIAGE_ASK_ANSWERS") or ANSWERS)


def claims_path() -> Path:
    return Path(os.environ.get("TRIAGE_ASK_CLAIMS") or CLAIMS)


def read_claims(path: Path) -> dict[str, Claim]:
    """The latest claim on each ask or question id."""
    return parsed_claims(path.read_text(encoding="utf-8")) if path.exists() else {}


def parsed_claims(text: str) -> dict[str, Claim]:
    found: dict[str, Claim] = {}
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and all(isinstance(record.get(key), str) for key in ("id", "by", "ts")):
            found[record["id"]] = Claim(record["id"], record["by"], record["ts"])
    return found


def question_claim(claims: dict[str, Claim], ask_id: str, number: int) -> Claim | None:
    return claims.get(f"{ask_id}:{number}") or claims.get(ask_id)


def ask_claim(ask: OpenAsk, claims: dict[str, Claim]) -> Claim | None:
    """The claim on every one of the ask's open questions (a question's own claim before the ask's), or on the
    ask itself when none is open."""
    ask_id = ask.report["id"]
    if not ask.open_questions:
        return claims.get(ask_id)
    found = [question_claim(claims, ask_id, number) for number in ask.open_questions]
    return found[0] if all(found) else None


def complete_end(data: bytes) -> int:
    return data.rfind(b"\n") + 1


def pending_bytes(inbox: Path, cursor: Path) -> tuple[int, bytes, int]:
    start = int(cursor.read_text().strip()) if cursor.exists() else 0
    with inbox.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if start > size:
            raise ValueError("inbox is shorter than the saved read position")
        stream.seek(start)
        data = stream.read()
    return start, data, complete_end(data)


def source_of(item: dict) -> str:
    worktree = str(item.get("worktree") or "-")
    return f"{item['repo']}/{worktree}" if item.get("repo") else worktree


def asked_questions(item: dict) -> list[dict]:
    return [question for question in questions_of(item) if isinstance(question, dict)]


def summary_line(item: dict) -> str:
    """`<ts> <kind> <host> <source> id=<id>  <first 160 characters>`, as `read --summary` prints an entry,
    then the headers of an ask's structured questions."""
    label = " ".join(str(item.get(key) or "-") for key in ("ts", "kind", "host"))
    location = source_of(item) + (f" id={item['id']}" if item.get("id") else "")
    compact = " ".join((item.get("text") or "").split())
    asked = asked_questions(item)
    headers = f"  [質問 {len(asked)} 件: {'・'.join(str(q.get('header')) for q in asked)}]" if asked else ""
    return f"{label} {location}  {compact[:160]}" + ("…" if len(compact) > 160 else "") + headers


def full_text(item: dict) -> str:
    """An entry's text, then each structured question with the id that answers it (`send --ask <id>:<n>`)."""
    lines = [item.get("text") or ""]
    for number, question in enumerate(questions_of(item), 1):
        if not isinstance(question, dict):
            continue
        label = f"Q{number} id={item.get('id')}:{number} [{question.get('header')}]"
        asked = f"{question.get('question')}（推奨: {question.get('recommended')}）"
        # The context (what the work is and what choosing does) comes before the question it explains.
        lines.extend((f"{label} {question['context']}", f"  {asked}") if question.get("context")
                     else (f"{label} {asked}",))
        lines.extend(f"  - {option.get('label')}: {option.get('description')}"
                     for option in question.get("options") or [] if isinstance(option, dict))
        if question.get("issue"):
            lines.append(f"  関わる issue: {question['issue']}")
    return "\n".join(lines)


def entry_header(item: dict) -> str:
    label = " ".join(str(item.get(key) or "-") for key in ("ts", "kind", "host"))
    return f"{label} {source_of(item)}" + (f" id={item['id']}" if item.get("id") else "")


def near_sources(entries: list[dict], wanted: str) -> list[str]:
    """The inbox's sources most like `wanted` by repo/worktree or by worktree alone, those holding it first."""
    worktrees = {source_of(item): str(item.get("worktree") or "") for item in entries}
    likeness = {source: max(SequenceMatcher(None, wanted, name).ratio() for name in (source, worktree))
                for source, worktree in worktrees.items()}
    near = [source for source, alike in likeness.items() if wanted in source or alike >= NEAR_LIKENESS]
    return sorted(near, key=lambda source: (wanted not in source, -likeness[source], source))[:NEAR_SOURCES]


def shown_by_filters(item: dict, options: ReadOptions) -> bool:
    return ((options.kinds is None or item.get("kind") in options.kinds)
            and (options.source is None or source_of(item) in options.source.names)
            and (options.since is None or (when := asked_at(item)) is not None and when >= options.since))


def read(inbox: Path, cursor: Path, options: ReadOptions) -> None:
    """Print entries after the cursor, or since a time regardless of the cursor."""
    if options.since is None:
        start, data, complete = pending_bytes(inbox, cursor)
    else:
        start, data = 0, inbox.read_bytes()
        complete = complete_end(data)
    end = start + complete
    shown = 0
    for raw in data[:end - start].splitlines():
        try:
            item = json.loads(raw)
            if not shown_by_filters(item, options):
                continue
            if options.summary:
                print(summary_line(item))
                shown += 1
                continue
            body = full_text(item)
            if options.brief and item.get("kind") == "info":
                compact = " ".join(body.split())
                body = compact[:160] + ("…" if len(compact) > 160 else "")
            print(f"{entry_header(item)}\n{body}\n")
            shown += 1
        except (ValueError, TypeError, AttributeError):
            if not options.filtered:
                print(raw.decode("utf-8", "replace"))
    if not shown and (options.kinds is not None or options.source is not None):
        earlier = [item for raw in inbox.read_bytes()[:start].splitlines()
                   if (item := entry(raw)) is not None and shown_by_filters(item, options)]
        latest = max(reversed(earlier), key=lambda item: asked_at(item) or datetime.min.replace(tzinfo=timezone.utc),
                     default={})
        print(f"inbox-read: no matching entries after the cursor; before the cursor: {len(earlier)} matching entries; "
              f"latest id={latest.get('id') or '-'} ts={latest.get('ts') or '-'}", file=sys.stderr)
        entries = inbox_entries(inbox) if options.source is not None else []
        if options.source is not None and not any(source_of(item) in options.source.names for item in entries):
            print(f"inbox-read: no entry has source {options.source.given}; near sources: "
                  f"{', '.join(near_sources(entries, options.source.given)) or 'none'}", file=sys.stderr)
    if options.filtered:
        print("inbox-read: a filtered read prints no NEXT_OFFSET; read without --kind, --source, --id, --since, "
              "or --text to acknowledge", file=sys.stderr)
    else:
        print(f"NEXT_OFFSET={end}")


def names(entry_id: str, wanted: str) -> bool:
    """An exact id, or a prefix of at least ID_PREFIX_MIN characters (a handoff's "id c8f79c0f")."""
    return entry_id == wanted or len(wanted) >= ID_PREFIX_MIN and entry_id.startswith(wanted)


def placed_entries(inbox: Path, cursor: Path, options: ReadOptions) -> list[Placed]:
    """Every complete entry the filters show, wherever the cursor stands, in inbox order."""
    acknowledged = int(cursor.read_text().strip()) if cursor.exists() else 0
    data = inbox.read_bytes()
    if acknowledged > len(data):
        raise ValueError("inbox is shorter than the saved read position")
    placed, end = [], 0
    for raw in data[:complete_end(data)].splitlines(keepends=True):
        end += len(raw)
        if (item := entry(raw)) is not None and shown_by_filters(item, options):
            placed.append(Placed(end, end <= acknowledged, item))
    return placed


def read_text(inbox: Path, cursor: Path, options: ReadOptions) -> None:
    """Print each entry whose whole text (`full_text`) holds options.text, ignoring case, wherever the cursor
    stands: its header, acknowledged ones marked 既読, then every line holding it."""
    wanted, found = (options.text or "").casefold(), False
    for _, acknowledged, item in placed_entries(inbox, cursor, options):
        hits = [line for line in full_text(item).splitlines() if wanted in line.casefold()]
        if hits:
            found = True
            print(f"{entry_header(item)}{' 既読' if acknowledged else ''}\n" + "\n".join(hits) + "\n")
    if not found:
        print(f"inbox-read: no entry holds {options.text}", file=sys.stderr)


def read_ids(inbox: Path, cursor: Path, options: ReadOptions) -> None:
    """Print in full, in inbox order, the entries the ids name, those already acknowledged marked 既読.

    Nothing is printed when an id names no entry or several: the candidates are named instead.
    """
    entries = [placed for placed in placed_entries(inbox, cursor, options) if placed.item.get("id")]
    wanted = sorted(options.ids or ())
    hits = {value: [placed for placed in entries if names(str(placed.item["id"]), value)] for value in wanted}
    several = {value: found for value, found in hits.items() if len({str(item["id"]) for *_, item in found}) > 1}
    if several:
        raise ValueError("\n".join(
            f"id {value} names {len(found)} entries; pass more of one:"
            + "".join(f"\n  {item.get('ts') or '-'} {item.get('kind') or '-'} {source_of(item)} id={item['id']}"
                      for *_, item in found) for value, found in several.items()))
    missing = [value for value, found in hits.items() if not found]
    if missing:
        raise ValueError("no entry has id " + ",".join(missing))
    for _, acknowledged, item in sorted({placed.end: placed for found in hits.values() for placed in found}.values()):
        print(f"{entry_header(item)}{' 既読' if acknowledged else ''}\n{full_text(item)}\n")


def inbox_entries(inbox: Path) -> list[dict]:
    """Every complete JSON object line of the inbox, in append order."""
    data = inbox.read_bytes()
    entries = []
    for raw in data[:complete_end(data)].splitlines():
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def answered_ids(path: Path) -> frozenset[str]:
    if not path.exists():
        return frozenset()
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            found.add(record["id"])
    return frozenset(found)


def asked_at(item: dict) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(item.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def questions_of(item: dict) -> list:
    questions = item.get("questions")
    return questions if isinstance(questions, list) else []


def is_handoff(item: dict) -> bool:
    """The facts separator handoff.py adds to every handoff, even without a session signature."""
    return "—— 送る直前に道具が足した事実 ——" in str(item.get("text") or "").splitlines()


def open_asks(entries: list[dict], answered: frozenset[str]) -> list[OpenAsk]:
    """Asks with an id that no answer covers, oldest first, leaving handoffs to lead_decisions.
    An answer to `<id>` covers the ask; answers
    to every `<id>:<n>` cover an ask with structured questions."""
    found = []
    for item in entries:
        if item.get("kind") != "ask" or not item.get("id") or item["id"] in answered or is_handoff(item):
            continue
        questions = questions_of(item)
        left = tuple(number for number in range(1, len(questions) + 1) if f"{item['id']}:{number}" not in answered)
        if not questions or left:
            found.append(OpenAsk(item, left, asked_at(item)))
    # Python's sort is stable: asks without a readable time keep their append order, after the rest.
    return sorted(found, key=lambda ask: (ask.at is None, ask.at or datetime.min.replace(tzinfo=timezone.utc)))


def ledger_owners(proposal_ids: list[str]) -> dict[str, str]:
    """The lane holding each proposal that has one, by adoption-triage's `ledger.py owner`; ADOPTION_TRIAGE_SCRIPT,
    ADOPTION_TRIAGE_EVENTS, and ADOPTION_TRIAGE_IMPORTS select another ledger, as adoption_handoff.py does."""
    script = Path(os.environ.get("ADOPTION_TRIAGE_SCRIPT") or LEDGER).expanduser()
    paths = [part for key, flag in (("ADOPTION_TRIAGE_EVENTS", "--events"), ("ADOPTION_TRIAGE_IMPORTS", "--imports"))
             if os.environ.get(key) for part in (flag, os.environ[key])]
    result = subprocess.run([sys.executable, str(script), *paths, "owner", "--json", *proposal_ids],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip()[:300])
    return {view["proposal_id"]: view["owner"] for view in json.loads(result.stdout)["items"] if view["owner"]}


def close_held_notices(asks: list[OpenAsk], answers: Path) -> list[OpenAsk]:
    """Record answered each adoption notice whose proposals all have a lane in the ledger, naming the lanes;
    return the asks left open."""
    named = {ask.report["id"]: NOTICE_ID.findall(text) for ask in asks
             if (text := str(ask.report.get("text") or "")).startswith(NOTICE)}
    if not any(named.values()):
        return asks
    try:
        owners = ledger_owners(sorted({proposal for proposals in named.values() for proposal in proposals}))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"inbox-read: 台帳から担当を引けず、採用の通知を閉じなかった: {exc}", file=sys.stderr)
        return asks
    closed = set()
    for ask_id, proposals in named.items():
        if proposals and all(proposal in owners for proposal in proposals):
            note = "・".join(f"案 {proposal[:8]} の担当 {owners[proposal]}" for proposal in proposals)
            record_answers(answers, [ask_id], "inbox_read.py open-asks", note)
            print(f"inbox-read: 採用の通知 {ask_id} を閉じた（{note}）", file=sys.stderr)
            closed.add(ask_id)
    return [ask for ask in asks if ask.report["id"] not in closed]


def minutes_since(when: datetime | None, now: datetime) -> int | None:
    return None if when is None else int((now - when).total_seconds() // 60)


def checked_answer_ids(value: str, entries: list[dict]) -> list[str]:
    """Comma-separated `<ask id>` or `<ask id>:<question number>`, each naming one ask of the inbox, with
    the full ask id; an ask id may be its first ID_PREFIX_MIN or more characters, as handoffs print it."""
    asks = {str(item["id"]): item for item in entries if item.get("kind") == "ask" and item.get("id")}
    ids = []
    for answer in (part.strip() for part in value.split(",")):
        given, colon, number = answer.partition(":")
        found = [ask_id for ask_id in asks if names(ask_id, given)]
        if len(found) > 1:
            raise ValueError(f"ask id {given} names {len(found)} asks; pass more of one: " + ", ".join(found))
        if not found or colon and not (number.isdigit() and 1 <= int(number) <= len(questions_of(asks[found[0]]))):
            raise ValueError(f"no ask in the inbox has id {answer}")
        ids.append(found[0] + (f":{number}" if colon else ""))
    return ids


def record_answers(path: Path, ids: list[str], how: str, note: str = "") -> None:
    """Append one line per answered id; triage-notify's lock keeps concurrent writers' lines whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = "".join(json.dumps({"id": answer, "ts": stamp, "how": how, **({"note": note} if note else {})},
                               ensure_ascii=False) + "\n" for answer in ids)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(lines)
        stream.flush()
        os.fsync(stream.fileno())


def overlapping_claims(claims: dict[str, Claim], claimed: str, open_keys: frozenset[str]) -> list[Claim]:
    """The claims on the open ask or questions that `claimed` (`<id>` or `<id>:<n>`) also covers; a question's
    own claim stands before the ask's, as question_claim reads them."""
    ask_id = claimed.split(":", 1)[0]
    covered = ([key for key in open_keys if key.startswith(f"{ask_id}:")] or [ask_id]) if claimed == ask_id \
        else [claimed]
    keys = dict.fromkeys(key if key in claims else ask_id for key in covered)
    return [claims[key] for key in keys if key in claims and key in open_keys]


def record_claims(path: Path, ids: list[str], by: str, open_keys: frozenset[str],
                  take_over: bool = False) -> list[Claim]:
    """Append one claim line per id under a lock, unless another worktree already claims an open ask or
    question they cover: then return those claims and write nothing, so two tabs never both go on to ask."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        held = parsed_claims(stream.read())
        taken = list({claim.id: claim for claimed in ids for claim in overlapping_claims(held, claimed, open_keys)
                      if claim.by != by}.values())
        if taken and not take_over:
            return taken
        # Taking over an ask also takes its questions another worktree claimed one by one.
        written = dict.fromkeys([*ids, *(claim.id for claim in taken
                                         if any(claim.id.startswith(f"{claimed}:") for claimed in ids))])
        stream.write("".join(json.dumps({"id": claimed, "by": by, "ts": stamp}, ensure_ascii=False) + "\n"
                             for claimed in written))
        stream.flush()
        os.fsync(stream.fileno())
    return []


def claim_label(claim: Claim) -> str:
    """`伺い中 <worktree> <HH:MM>〜`, as open-asks prefixes a claimed ask."""
    if claim.inactive_minutes is not None:
        return f"伺っていない（claim のみ、{claim.inactive_minutes} 分）{claim.by}"
    try:
        since = f" {datetime.fromisoformat(claim.at):%H:%M}〜"
    except ValueError:
        since = ""
    return f"伺い中 {claim.by}{since}"


def checked_claims(claims: dict[str, Claim], now: datetime, orca: str, projects: Path) -> dict[str, Claim]:
    """A claim is no proof of a live question after its owner's turn ended fifteen minutes ago."""
    if not claims:
        return claims
    import handoff
    import scan_sessions
    try:
        rows = scan_sessions.orca_payload(orca, ["worktree", "ps"])["worktrees"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return claims
    inactive: dict[str, int] = {}
    for by in dict.fromkeys(claim.by for claim in claims.values()):
        matches = [row for row in rows if by in (row["path"], Path(row["path"]).name,
                                                f"{row.get('repo')}/{Path(row['path']).name}")]
        if len(matches) != 1:
            continue
        try:
            details = handoff.lead_details(projects.expanduser(), matches[0]["path"], orca, handoff.HANDOFFS)
        except (OSError, ValueError, KeyError, LookupError, subprocess.SubprocessError):
            continue
        agents = cast(list[dict], details["agents"])
        if details["pending_questions"] or not agents or any(agent["state"] != "done" for agent in agents):
            continue
        stopped = [scan_sessions.parse_time(agent.get("stopped_at")) for agent in agents]
        if any(at is None for at in stopped):
            continue
        last = max(at for at in stopped if at is not None)
        inactive[by] = int((now - last).total_seconds() // 60)
    return {key: replace(claim, inactive_minutes=min(inactive[claim.by], age))
            if claim.by in inactive and inactive[claim.by] > 15
            and (age := minutes_since(scan_sessions.parse_time(claim.at), now)) is not None and age > 15 else claim
            for key, claim in claims.items()}


def open_ask_view(ask: OpenAsk, now: datetime, claims: dict[str, Claim] | None = None) -> dict:
    report, claims = ask.report, claims or {}
    claimed = ask_claim(ask, claims)
    return {"id": report["id"], "ts": report.get("ts") or "", "host": report.get("host") or "",
            "source": source_of(report), "handle": report.get("handle") or "", "text": report.get("text") or "",
            "open_questions": [{**questions_of(report)[number - 1], "number": number,
                                "claimed": (found.view() if (found := question_claim(claims, report["id"], number))
                                            else None)} for number in ask.open_questions],
            "claimed": claimed.view() if claimed else None,
            "age_minutes": minutes_since(ask.at, now)}


def pending_questions(asks: list[OpenAsk]) -> list[PendingQuestion]:
    """The unanswered structured questions of the open asks, oldest ask first."""
    return [PendingQuestion(f"{ask.report['id']}:{number}", ask.report, questions_of(ask.report)[number - 1])
            for ask in asks for number in ask.open_questions]


def ask_user_question(question: dict) -> dict:
    """AskUserQuestion's question: its context before it, the issue named after it, and the recommended option
    first and marked."""
    recommended = question.get("recommended")
    options = sorted(question["options"], key=lambda option: option["label"] != recommended)
    return {"question": answer_evidence.shown_question(question), "header": question["header"],
            "multiSelect": bool(question.get("multiSelect")),
            "options": [{"label": option["label"] + (RECOMMENDED_MARK if option["label"] == recommended else ""),
                         "description": option["description"]} for option in options]}


def question_batch(questions: list[PendingQuestion], after: str | None = None,
                   order: list[str] | None = None, claims: dict[str, Claim] | None = None) -> dict:
    """The next questions after the id `after` in `order` (every structured question, answered or not, so
    a batch answered since it was printed still pages on), with where each came from and what is left.
    A question another worktree claimed is left out and listed under `claimed`."""
    claims = claims or {}
    taken = {question.id: claim for question in questions
             if (claim := question_claim(claims, question.report["id"], int(question.id.rsplit(":", 1)[1])))}
    claimed = [{"id": question_id, **claim.view()} for question_id, claim in taken.items()]
    questions = [question for question in questions if question.id not in taken]
    if after is not None:
        order = order if order is not None else [question.id for question in questions]
        if after not in order:
            raise ValueError(f"no structured question has id {after}")
        place = {question_id: index for index, question_id in enumerate(order)}
        questions = [question for question in questions if place.get(question.id, -1) > place[after]]
    batch = questions[:QUESTIONS_PER_BATCH]
    left = len(questions) - len(batch)
    return {
        "questions": [ask_user_question(question.question) for question in batch],
        "items": [{"id": question.id, "source": source_of(question.report), "host": question.report.get("host") or "",
                   "handle": question.report.get("handle") or "", "ts": question.report.get("ts") or "",
                   "issue": question.question.get("issue") or ""} for question in batch],
        "remaining": left, "next_after": batch[-1].id if left else None, "claimed": claimed,
    }


def entry(raw: bytes) -> dict | None:
    try:
        item = json.loads(raw)
    except ValueError:
        return None
    return item if isinstance(item, dict) else None


def end_past(inbox: Path, start: int, skip: Callable[[dict], bool]) -> int:
    """The offset after the complete entries from `start` that `skip` accepts, up to the first it does not
    (or a damaged line), so no other entry is passed over."""
    data = inbox.read_bytes()
    lines = data[start:complete_end(data)].splitlines(keepends=True)
    return start + sum(map(len, takewhile(lambda raw: (item := entry(raw)) is not None and skip(item), lines)))


def unread_asks(inbox: Path, cursor: Path, mine: Sender | None = None) -> list[str]:
    """Return lane names for complete ask records after the acknowledged position, leaving out those
    `mine` sent: a tab's own ask is not one it has to read."""
    _, data, complete = pending_bytes(inbox, cursor)
    return [str(item.get("worktree") or item.get("handle") or "unknown")
            for item in map(entry, data[:complete].splitlines())
            if item and item.get("kind") == "ask" and not (mine and mine.sent(item))]


def save(cursor: Path, position: int, exclusive: bool = False) -> None:
    cursor.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with cursor.open("x") as stream:
            stream.write(str(position))
    else:
        temporary = cursor.with_name(cursor.name + ".tmp")
        temporary.write_text(str(position))
        temporary.replace(cursor)


def watch(inbox: Path, cursor: Path, answers: Path, once: bool, interval: float, remind_after: int) -> None:
    """Print each new ask and done, whatever its source, then each ask unanswered `remind_after` minutes,
    once per ask while this watch runs; advance the cursor past every entry printed or not."""
    if not cursor.exists():
        raise ValueError(f"watch reads only after an initialized cursor: run initialize with "
                         f"TRIAGE_READ_OFFSET={cursor} first")
    reminded: set[str] = set()
    announced: set[tuple[str, str, str]] = set()
    while True:
        start, data, complete = pending_bytes(inbox, cursor)
        for raw in data[:complete].splitlines():
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("kind") in WATCHED_KINDS:
                print(summary_line(item), flush=True)
        if complete:
            save(cursor, start + complete)
        now = datetime.now().astimezone()
        claims = read_claims(claims_path())
        for ask in open_asks(inbox_entries(inbox), answered_ids(answers)):
            claim = ask_claim(ask, claims)
            if claim:
                if (key := (ask.report["id"], claim.by, claim.at)) not in announced:
                    announced.add(key)
                    print(f"伺い中 {claim.by}: {summary_line(ask.report)}", flush=True)
                continue
            age = minutes_since(ask.at, now)
            if age is not None and age >= remind_after and ask.report["id"] not in reminded:
                reminded.add(ask.report["id"])
                print(f"unanswered {age} min: {summary_line(ask.report)}", flush=True)
        if once:
            return
        time.sleep(interval)


def owner_line(run: dict) -> str:
    """The run as `hourly_runs.py` lists it: scheduled time, status, start, pane, and transcript (its session)."""
    import hourly_runs  # here: it imports scan_sessions, which imports this module

    try:
        return hourly_runs.run_of(run, Path.home()).line()
    except (OSError, ValueError) as error:
        return f"pane {run.get('terminalPaneKey') or '-'}, {run.get('status') or '-'}; transcript unread: {error}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    read_command = actions.add_parser("read", help="show complete new lines without changing the cursor; "
                                                   "NEXT_OFFSET only without --kind, --source, --id, --since, "
                                                   "and --text")
    read_command.add_argument("--brief", action="store_true", help="show only the first 160 characters of info")
    read_command.add_argument("--kind", type=report_kinds, help="filter by comma-separated ask,done,info")
    read_command.add_argument("--source", help="filter by exact repo/worktree (or worktree for older entries); a "
                                               "worktree name alone also reads its repo/worktree when one repo has "
                                               "it, and a source no entry has names the near ones on stderr")
    read_command.add_argument("--since", type=since_time, metavar="HH:MM|ISO",
                              help="read entries at or after this time, including acknowledged ones; "
                                   "HH:MM means its most recent local occurrence; prints no NEXT_OFFSET")
    read_command.add_argument("--summary", action="store_true",
                              help="show one line per entry with its id and a 160-character excerpt")
    picked = read_command.add_mutually_exclusive_group()
    picked.add_argument("--id", type=entry_ids, dest="ids",
                        help="show only these comma-separated entry ids, in full, wherever the cursor stands "
                             "(acknowledged ones marked 既読); an id may be its first 8 or more characters, "
                             "and one naming several entries lists them and prints nothing")
    picked.add_argument("--text", metavar="WORD",
                        help="search every entry's whole text and structured questions, ignoring case, wherever "
                             "the cursor stands: print each match's header (id, time, source; acknowledged ones "
                             "marked 既読) and the lines holding WORD")
    watch_command = actions.add_parser(
        "watch", help="the lead's Monitor: print every new ask and done as a summary line, advance the cursor, "
                      "and print `unanswered <n> min: <line>` once per ask left unanswered --remind-after minutes")
    watch_command.add_argument("--interval", type=float, default=10, help="seconds between reads (default: 10)")
    watch_command.add_argument("--remind-after", type=int, default=REMIND_AFTER_MINUTES,
                               help="minutes before an unanswered ask is named (default: %(default)s)")
    watch_command.add_argument("--once", action="store_true", help="read once and exit")
    open_command = actions.add_parser("open-asks", help="list asks not answered yet, oldest first, one summary "
                                                        "line each after `unanswered <n> min:`; first record "
                                                        "answered each adoption notice whose proposals have a lane "
                                                        "in the ledger (`ledger.py owner`), naming the lane on stderr")
    open_command.add_argument("--json", action="store_true",
                              help="print a JSON list of {id, ts, host, source, handle, text, open_questions, "
                                   "age_minutes} instead; open_questions holds each unanswered question's "
                                   "number, header, question, options, and recommended")
    open_command.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"))
    open_command.add_argument("--claude-projects", type=Path, default=Path("~/.claude/projects"),
                              help="transcripts used to check whether a claimed question is still being asked")
    answered = actions.add_parser("answered", help="record asks as answered without sending (answered elsewhere, "
                                                   "or no longer asked); send --decision --ask records its own")
    answered.add_argument("ids", nargs="?", help="comma-separated ask ids, or <ask id>:<question number>")
    answered.add_argument("--before", help="instead, every open ask asked before this ISO time")
    answered.add_argument("--note", default="", help="why, kept with the record")
    answer_evidence.add_arguments(answered)
    claim = actions.add_parser("claim", help="record that a worktree is asking the user these asks now, before "
                                             "it asks: open-asks marks them 伺い中, watch stops reminding of "
                                             "them, and questions leaves them out")
    claim.add_argument("ids", help="comma-separated ask ids, or <ask id>:<question number>")
    claim.add_argument("--by", required=True, help="the worktree asking them")
    claim.add_argument("--take-over", action="store_true",
                       help="claim them even though another worktree claims one; only when it is not asking")
    questions = actions.add_parser(
        "questions", help="print, as JSON, the next 4 unanswered structured questions: `questions` to pass to "
                          "AskUserQuestion, `items` naming each one's id and source, `remaining`, and "
                          "`next_after` for the following batch")
    questions.add_argument("--after", metavar="ID", help="start after this question id (a batch's next_after)")
    ack = actions.add_parser("ack", help="acknowledge the byte position printed by an unfiltered read")
    ack.add_argument("position", type=int)
    actions.add_parser("initialize", help="set a new cursor at the current complete end")
    args = parser.parse_args(argv)
    if args.action == "answered" and (args.ids is None) == (args.before is None):
        parser.error("answered takes ids or --before, not both")
    if args.action == "read" and args.text is not None and not args.text.strip():
        parser.error("--text needs a word to search for")
    inbox, cursor = paths()
    try:
        # watch skips info as it advances: on the scheduled run's cursor it would hide them from that run.
        if args.action == "watch" and not os.environ.get("TRIAGE_READ_OFFSET"):
            raise ValueError("watch needs a cursor of its own: set TRIAGE_READ_OFFSET")
        if args.action == "ack" and not os.environ.get("TRIAGE_READ_OFFSET"):
            mine, owner = hourly_runtime.cursor_owner(os.environ.get("ORCA_CLI_COMMAND", "orca"))
            if not mine:
                # After the Mac sleeps, several scheduled tabs run at once: name the one that owns the cursor.
                named = (f" ({owner_line(owner)}), not this tab (pane {os.environ.get('ORCA_PANE_KEY') or '-'})"
                         if owner else "")
                raise ValueError(f"the default inbox cursor belongs to the latest scheduled triage tab{named}; "
                                 "read without it: ~/.cache/orca-briefs/bin/inbox --since HH:MM --wt NAME "
                                 "(HH:MM: when this run began; NAME: the worktree whose reports to read), "
                                 "or read --since HH:MM")
        if args.action == "read":
            source = SourceFilter.of(args.source, inbox_entries(inbox)) if args.source is not None else None
            options = ReadOptions(brief=args.brief, kinds=args.kind, source=source, summary=args.summary,
                                  ids=args.ids, since=args.since, text=args.text)
            (read_ids if args.ids else read_text if args.text is not None else read)(inbox, cursor, options)
            return 0
        if args.action == "watch":
            watch(inbox, cursor, answers_path(), args.once, args.interval, args.remind_after)
            return 0
        if args.action in ("open-asks", "questions", "answered", "claim"):
            entries = inbox_entries(inbox)
            asks = open_asks(entries, answered_ids(answers_path()))
            open_keys = frozenset(key for ask in asks for key in (
                ask.report["id"], *(f"{ask.report['id']}:{number}" for number in ask.open_questions)))
            claims = {key: claim for key, claim in read_claims(claims_path()).items() if key in open_keys}
            now = datetime.now().astimezone()
            if args.action == "claim":
                if not args.by.strip():
                    raise ValueError("--by names the worktree asking")
                ids = checked_answer_ids(args.ids, entries)
                taken = record_claims(claims_path(), ids, args.by.strip(), open_keys, args.take_over)
                for claim in taken:
                    print(f"inbox-read: {claim.id} は {claim_label(claim)}。重ねて伺わない"
                          "（伺っていないと分かったときだけ --take-over）", file=sys.stderr)
                if taken:
                    return 1
                print(f"claimed: {len(ids)} by {args.by.strip()}")
            elif args.action == "answered":
                if args.ids is not None:
                    ids = checked_answer_ids(args.ids, entries)
                    refused = []
                else:
                    before = datetime.fromisoformat(args.before)
                    before = before if before.tzinfo else before.astimezone()
                    earlier = [ask for ask in asks if ask.at is not None and ask.at < before]
                    found = answer_evidence.from_arguments(args, answers_path())
                    shared = answer_evidence.shared_questions(entries)
                    ids = [ask.report["id"] for ask in earlier
                           if answer_evidence.answers_to(ask.report, found, ask.open_questions, shared)]
                    refused = [ask for ask in earlier if ask.report["id"] not in ids]
                record_answers(answers_path(), ids, "inbox_read.py answered", args.note)
                print(f"answered: {len(ids)}")
                for ask in refused:
                    print(f"not marked, no answer after it names this ask or each open question: "
                          f"{summary_line(ask.report)}", file=sys.stderr)
                    for answer in answer_evidence.related_to(ask.report, found)[-3:]:
                        print(f"  to check: {answer.at.astimezone():%m/%d %H:%M} {answer.source}  "
                              f"{' '.join(answer.text.split())[:100]}", file=sys.stderr)
                if refused:
                    print("inbox-read: confirm each one was answered, then mark it with `answered <id> --note <where>`",
                          file=sys.stderr)
                    return 1
            elif args.action == "questions":
                order = [question.id for question in pending_questions(open_asks(entries, frozenset()))]
                print(json.dumps(question_batch(pending_questions(asks), args.after, order, claims),
                                 ensure_ascii=False, indent=2))
            elif args.json:
                claims = checked_claims(claims, now, args.orca, args.claude_projects)
                asks = close_held_notices(asks, answers_path())
                print(json.dumps([open_ask_view(ask, now, claims) for ask in asks], ensure_ascii=False, indent=2))
            else:
                claims = checked_claims(claims, now, args.orca, args.claude_projects)
                for ask in close_held_notices(asks, answers_path()):
                    age = minutes_since(ask.at, now)
                    claim = ask_claim(ask, claims)
                    print(f"{claim_label(claim) + ' ' if claim else ''}"
                          f"unanswered {'?' if age is None else age} min: {summary_line(ask.report)}")
            return 0
        data = inbox.read_bytes()
        end = complete_end(data)
        if args.action == "initialize":
            save(cursor, end, exclusive=True)
            print(f"initialized at {end}")
            return 0
        current = int(cursor.read_text().strip()) if cursor.exists() else 0
        if not current <= args.position <= end or (args.position and data[args.position - 1] != 10):
            raise ValueError("position must be a complete line between the saved cursor and inbox end")
        save(cursor, args.position)
        print(f"acknowledged through {args.position}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        print(f"inbox-read: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
