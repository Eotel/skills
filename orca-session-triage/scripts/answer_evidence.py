"""What shows that an inbox ask was answered, for tools that must not take a mark for an answer.

An answer is one of:
- a decision the lead recorded with lead_decisions.py, the latest one per key (a later record replaces the earlier);
- an AskUserQuestion answer in the Claude transcripts of the worktrees that ask the user (the lead's, and the
  hourly runs' triage workspace);
- an answer `send --decision --ask <id>[:<n>]` delivered (`send --decision` in ask-answers.jsonl).

Only an answer that came after the ask and names it is proof: one delivered to it, or for an ask without
structured questions one naming its id (in full or its first 8 or more characters), and for an ask with them one
per question, delivered to `<id>:<n>` or quoting the question as `inbox_read.py questions` shows it (its text, then
its issue in brackets) when no other ask of the inbox shows the same. An answer that names only the asking worktree or a
PR number of the ask (`#1234`, `owner/repo#1234`) is related: a lane asks more than once, so it is a candidate to
check, never proof. A mark `inbox_read.py answered` wrote is not an answer: on 2026-10-29 an ask marked answered
in bulk waited 25 hours with no answer anywhere. inbox_read.py `answered --before` refuses to mark an ask without
proof, and hourly_state.py `stale-asks` reads the waiting rows of a scan against them.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
from operator import attrgetter
import os
from pathlib import Path
import re

import lead_decisions
import scan_sessions

DELIVERED = "send --decision"
# A worktree name this short ("main", "web") appears in unrelated answers.
NAME_MIN = 4
# `#1234` or `owner/repo#1234`; shorter numbers are too common to name a PR.
PR_NUMBER = re.compile(r"(?<![#\d])#(\d{3,})(?!\d)")
ID_PREFIX = 8
# rm_check.py asks the lead `remove:<path>` about a worktree's keep note: an answer about the worktree, not to its ask.
NOT_ANSWERS = ("remove:",)


@dataclass(frozen=True)
class Answer:
    at: datetime
    source: str
    text: str
    ask_id: str | None = None  # the `<id>` or `<id>:<n>` a delivered answer names exactly
    answered: tuple[str, ...] = ()  # the questions an AskUserQuestion answer answered, as it showed them


def questions_of(ask: dict) -> list[str]:
    """Each structured question as inbox_read.ask_user_question shows it to the user."""
    return [str(question.get("question") or "") + (f"（{question['issue']}）" if question.get("issue") else "")
            if isinstance(question, dict) else "" for question in ask.get("questions") or []]


def shared_questions(entries: Iterable[dict]) -> frozenset[str]:
    """Questions two or more asks show alike: quoting one cannot tell which ask it answers."""
    shown = Counter(question for item in entries if item.get("kind") == "ask"
                    for question in dict.fromkeys(questions_of(item)) if question)
    return frozenset(question for question, count in shown.items() if count > 1)


def names_id(text: str, ask_id: str) -> bool:
    """Whether `text` holds the id in full or one of its prefixes of ID_PREFIX or more characters."""
    found = re.finditer(rf"(?<![\w-]){re.escape(ask_id[:ID_PREFIX])}[\w-]*", text)
    return len(ask_id) >= ID_PREFIX and any(ask_id.startswith(match.group(0)) for match in found)


def proves(answer: Answer, ask: dict, number: int | None, shared: frozenset[str]) -> bool:
    """Whether the answer names the ask (number None) or its structured question `number` exactly."""
    ask_id = str(ask.get("id") or "")
    if answer.ask_id is not None:
        return answer.ask_id in (ask_id, f"{ask_id}:{number}")
    if number is None:
        return bool(ask_id) and names_id(answer.text, ask_id)
    question = questions_of(ask)[number - 1]
    if not question or question in shared:
        return False
    return question in answer.answered if answer.answered else question in answer.text


def relates(answer: Answer, ask: dict) -> bool:
    worktree = str(ask.get("worktree") or "")
    numbers = dict.fromkeys(PR_NUMBER.findall(str(ask.get("text") or "")))
    return ((len(worktree) >= NAME_MIN
             and re.search(rf"(?<![\w-]){re.escape(worktree)}(?![\w-])", answer.text) is not None)
            or any(re.search(rf"(?<![#\d])#{number}(?!\d)", answer.text) for number in numbers))


def after_ask(ask: dict, answers: Sequence[Answer]) -> list[Answer]:
    asked_at = scan_sessions.parse_time(ask.get("ts"))
    return [] if asked_at is None else sorted((answer for answer in answers if answer.at >= asked_at),
                                              key=attrgetter("at"))


def answers_to(ask: dict, answers: Sequence[Answer], questions: Iterable[int] | None = None,
               shared: frozenset[str] = frozenset()) -> list[Answer]:
    """The answers that prove the ask answered, earliest first; [] while one of `questions` (default: all of its
    structured questions) has none, or for an ask without a readable time. `shared` is shared_questions of the
    inbox."""
    later = after_ask(ask, answers)
    if not ask.get("questions"):
        return [answer for answer in later if proves(answer, ask, None, shared)]
    numbers = list(range(1, len(questions_of(ask)) + 1) if questions is None else questions)
    per_question = [[answer for answer in later if proves(answer, ask, number, shared)] for number in numbers]
    if not all(per_question):
        return []
    return sorted({id(answer): answer for found in per_question for answer in found}.values(),
                  key=attrgetter("at"))


def related_to(ask: dict, answers: Sequence[Answer]) -> list[Answer]:
    """Answers after the ask that name only its worktree or PR numbers, earliest first: candidates to check."""
    return [answer for answer in after_ask(ask, answers) if relates(answer, ask)]


def decisions(log: Path) -> Iterator[Answer]:
    yield from (Answer(at, f"lead_decisions {item.decision}", f"{item.decision}\n{item.answer}")
                for item in lead_decisions.latest(lead_decisions.records(log)).values()
                if not item.decision.startswith(NOT_ANSWERS)
                if (at := scan_sessions.parse_time(item.decided_at)) is not None)


def delivered(log: Path) -> Iterator[Answer]:
    if not log.exists():
        return
    for raw in log.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if (isinstance(record, dict) and record.get("how") == DELIVERED and isinstance(record.get("id"), str)
                and (at := scan_sessions.parse_time(record.get("ts"))) is not None):
            yield Answer(at, DELIVERED, str(record.get("note") or ""), record["id"])


def asked_user(transcripts: Iterable[Path]) -> Iterator[Answer]:
    """Each AskUserQuestion the user answered: the questions it answered, and as its text the questions, choices,
    answers, and notes, one per line (JSON would escape the quotes and line breaks a question holds)."""
    for path in transcripts:
        with path.open("rb") as stream:
            # The lead's transcript passes 150 MB; only an answered AskUserQuestion carries "answers".
            for raw in (line for line in stream if b'"answers"' in line):
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                result = entry.get("toolUseResult") if isinstance(entry, dict) else None
                if not (isinstance(result, dict) and isinstance(result.get("answers"), dict)
                        and (at := scan_sessions.parse_time(entry.get("timestamp"))) is not None):
                    continue
                asked = [question for question in result.get("questions") or [] if isinstance(question, dict)]
                notes = [note.get("notes") for note in (result.get("annotations") or {}).values()
                         if isinstance(note, dict)] if isinstance(result.get("annotations"), dict) else []
                lines = [*(question.get("question") for question in asked),
                         *(f"{option.get('label')}: {option.get('description')}" for question in asked
                           for option in question.get("options") or [] if isinstance(option, dict)),
                         *result["answers"].values(), *notes]
                yield Answer(at, f"AskUserQuestion {path.name}", "\n".join(str(line) for line in lines if line),
                             answered=tuple(str(question) for question in result["answers"]))


def transcripts_of(projects: Path, worktrees: Iterable[Path]) -> Iterator[Path]:
    for worktree in dict.fromkeys(worktrees):
        yield from sorted(scan_sessions.find_claude_dir(projects, str(worktree)).glob("*.jsonl"))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--decisions-file", type=Path, default=lead_decisions.DEFAULT_LOG,
                        help="the lead's recorded answers (default: %(default)s)")
    parser.add_argument("--asked-in", type=Path, action="append", default=[], metavar="WORKTREE",
                        help="a worktree whose Claude transcripts hold AskUserQuestion answers, such as the lead's "
                             "or the triage workspace; repeat per worktree. The current directory is always read")
    parser.add_argument("--claude-projects", type=Path,
                        default=Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude") / "projects",
                        help="Claude Code transcript root (default: $CLAUDE_CONFIG_DIR/projects, else %(default)s)")


def from_arguments(args: argparse.Namespace, answers_log: Path) -> list[Answer]:
    worktrees = [Path.cwd(), *(path.expanduser().absolute() for path in args.asked_in)]
    return [*decisions(args.decisions_file.expanduser()), *delivered(answers_log),
            *asked_user(transcripts_of(args.claude_projects.expanduser(), worktrees))]
