"""What shows that an inbox ask was answered, for tools that must not take a mark for an answer.

An answer is one of:
- a decision the lead recorded with lead_decisions.py;
- an AskUserQuestion answer in the Claude transcripts of the worktrees that ask the user (the lead's, and the
  hourly runs' triage workspace);
- an answer `send --decision --ask <id>` delivered (`send --decision` in ask-answers.jsonl), which names its ask.

A decision or an AskUserQuestion answer covers an ask when it came after the ask and names it: the ask's id (its
first 8 or more characters), the asking worktree's name, a PR number of 3 or more digits from the ask's text, or
one of its structured questions. A mark `inbox_read.py answered` wrote is not an answer: on 2026-10-29 an ask
marked answered in bulk waited 25 hours with no answer anywhere. inbox_read.py `answered --before` refuses to mark
an ask no answer covers, and hourly_state.py `stale-asks` reads the waiting rows of a scan against them.
"""

from __future__ import annotations

import argparse
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
PR_NUMBER = re.compile(r"(?<![\w#])#(\d{3,})(?!\d)")
ID_PREFIX = 8
# rm_check.py asks the lead `remove:<path>` about a worktree's keep note: an answer about the worktree, not to its ask.
NOT_ANSWERS = ("remove:",)


@dataclass(frozen=True)
class Answer:
    at: datetime
    source: str
    text: str
    ask_id: str | None = None  # the ask (or `<id>:<n>`) a delivered answer names exactly

    def covers(self, ask: dict, asked_at: datetime) -> bool:
        if self.ask_id is not None:
            return self.ask_id.partition(":")[0] == ask.get("id")
        return self.at >= asked_at and any(pattern.search(self.text) for pattern in names_of(ask))


def token(value: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(value)}(?![\w-])")


def names_of(ask: dict) -> list[re.Pattern[str]]:
    """What an answer to `ask` would name: its id, its worktree, its PR numbers, and its structured questions."""
    worktree = str(ask.get("worktree") or "")
    questions = [str(question.get("question")).strip() for question in ask.get("questions") or []
                 if isinstance(question, dict) and question.get("question")]
    return [*([token(str(ask["id"])[:ID_PREFIX])] if ask.get("id") else []),
            *([token(worktree)] if len(worktree) >= NAME_MIN else []),
            *(re.compile(rf"(?<![\w#])#{number}(?!\d)")
              for number in dict.fromkeys(PR_NUMBER.findall(str(ask.get("text") or "")))),
            *(re.compile(re.escape(question)) for question in questions)]


def decisions(log: Path) -> Iterator[Answer]:
    yield from (Answer(at, f"lead_decisions {item.decision}", f"{item.decision}\n{item.answer}")
                for item in lead_decisions.records(log) if not item.decision.startswith(NOT_ANSWERS)
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
    """Each AskUserQuestion the user answered, with its questions, choices, and answers as its text."""
    for path in transcripts:
        with path.open("rb") as stream:
            # The lead's transcript passes 150 MB; only an answered AskUserQuestion carries "answers".
            for raw in (line for line in stream if b'"answers"' in line):
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                result = entry.get("toolUseResult") if isinstance(entry, dict) else None
                if (isinstance(result, dict) and isinstance(result.get("answers"), dict)
                        and (at := scan_sessions.parse_time(entry.get("timestamp"))) is not None):
                    yield Answer(at, f"AskUserQuestion {path.name}", json.dumps(result, ensure_ascii=False))


def transcripts_of(projects: Path, worktrees: Iterable[Path]) -> Iterator[Path]:
    for worktree in dict.fromkeys(worktrees):
        yield from sorted(scan_sessions.find_claude_dir(projects, str(worktree)).glob("*.jsonl"))


def answer_to(ask: dict, answers: Sequence[Answer]) -> Answer | None:
    """The earliest answer that covers the ask; None for an ask without a readable time."""
    asked_at = scan_sessions.parse_time(ask.get("ts"))
    if asked_at is None:
        return None
    covering = [answer for answer in answers if answer.covers(ask, asked_at)]
    return min(covering, key=attrgetter("at")) if covering else None


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
