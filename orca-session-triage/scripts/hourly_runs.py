#!/usr/bin/env python3
"""List a triage workspace's scheduled hourly runs, newest first, one line each.

Each line gives when the run was scheduled and started (local time), Orca's status,
what the precheck printed when it skipped the run, the run's tab pane, and its Claude
transcript, so neither Orca's automation records nor transcript paths that contain
spaces have to be read by hand. `--json` prints the same fields as a list.
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import cleanup_hourly_tabs
import hourly_runtime
import scan_sessions


@dataclass
class Question:
    tool_use_id: str
    question: str
    asked_at: str
    answered_at: str | None = None
    answer: str | None = None
    result_received: bool = False


@dataclass(frozen=True)
class Conversation:
    tail: dict[str, str]
    questions: list[Question]

    @property
    def waiting(self) -> bool:
        return any(not question.result_received for question in self.questions)


def conversation(path: Path) -> Conversation:
    """The last utterance and each AskUserQuestion, matched to its tool_result by id."""
    tail: dict[str, str] = {}
    asked: dict[str, list[Question]] = {}
    with path.open(encoding="utf-8") as stream:
        for raw in stream:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            content = scan_sessions.message_of(entry).get("content")
            blocks = content if isinstance(content, list) else []
            at = entry.get("timestamp") or ""
            said = scan_sessions.user_text(entry)
            if said and (entry.get("type") == "assistant" or scan_sessions.is_real_user_message(entry)):
                tail = {"role": entry["type"], "at": at, "text": said}
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if entry.get("type") == "assistant" and block.get("name") == "AskUserQuestion":
                    tool_id = str(block.get("id") or "")
                    asked[tool_id] = [Question(tool_id, str(question.get("question") or ""), at)
                                      for question in (block.get("input") or {}).get("questions") or []
                                      if isinstance(question, dict)]
                elif block.get("type") == "tool_result":
                    result = entry.get("toolUseResult") or {}
                    if not isinstance(result, dict):
                        result = {}
                    answers = result.get("answers") or {}
                    for question in asked.get(str(block.get("tool_use_id") or ""), []):
                        question.result_received = True
                        if isinstance(answers, dict) and question.question in answers:
                            question.answered_at = at
                            question.answer = str(answers[question.question])
    return Conversation(tail, [question for batch in asked.values() for question in batch])


@dataclass(frozen=True)
class Run:
    scheduled: str
    status: str
    started: str
    precheck: str
    pane: str
    transcript: str

    def line(self) -> str:
        return " | ".join((self.scheduled or "-", self.status, f"started {self.started or '-'}",
                           self.precheck or "-", f"pane {self.pane or '-'}", self.transcript or "-"))


def local_time(epoch_ms: object) -> str:
    if not isinstance(epoch_ms, (int, float)):
        return ""
    return datetime.fromtimestamp(epoch_ms / 1000).astimezone().isoformat(timespec="seconds")


def run_of(record: dict[str, Any], home: Path) -> Run:
    said = ((record.get("precheckResult") or {}).get("stdout") or "").strip()
    transcript = cleanup_hourly_tabs.transcript_for(record, home)
    return Run(scheduled=local_time(record.get("scheduledFor")), status=str(record.get("status") or ""),
               started=local_time(record.get("startedAt")),
               precheck=said.splitlines()[0] if record.get("status") == "skipped_precheck" and said else "",
               pane=record.get("terminalPaneKey") or "", transcript=str(transcript) if transcript else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=hourly_runtime.workspace_path(
        os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")),
        help="triage workspace path (default: this Orca worktree)")
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    parser.add_argument("--limit", type=int, default=12, help="newest runs to list (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="print the runs as a JSON list")
    parser.add_argument("--tail", action="store_true",
                        help="include each run's last utterance and whether AskUserQuestion has no tool_result")
    parser.add_argument("--questions", action="store_true",
                        help="include each AskUserQuestion, its answer, and when each was recorded")
    args = parser.parse_args(argv)
    if not args.workspace:
        parser.error("--workspace or ORCA_WORKTREE_ID is required")
    try:
        records = hourly_runtime.scheduled_runs(args.orca, args.workspace)[:args.limit]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        print(f"hourly runs: could not read Orca's automation runs: {error}", file=sys.stderr)
        return 1
    try:
        runs = [run_of(record, Path.home()) for record in records]
        conversations = ([conversation(Path(run.transcript)) if run.transcript else None for run in runs]
                         if args.tail or args.questions else [None] * len(runs))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"hourly runs: could not read a transcript: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([{**asdict(run), **({"tail": found.tail if found else None,
                                               "ask_user_question_waiting": found.waiting if found else None}
                                              if args.tail else {}),
                           **({"questions": [asdict(question) for question in found.questions] if found else []}
                              if args.questions else {})}
                          for run, found in zip(runs, conversations)], ensure_ascii=False, indent=2))
        return 0
    for run, found in zip(runs, conversations):
        print(run.line())
        if args.tail:
            tail = found.tail if found else {}
            print(f"  tail {tail.get('at') or '-'} {tail.get('role') or '-'}: {tail.get('text') or '-'}")
            print(f"  AskUserQuestion waiting: {found.waiting if found else None}")
        for question in found.questions if found and args.questions else []:
            print(f"  asked {question.asked_at or '-'}: {question.question}")
            print(f"  answered {question.answered_at or '-'}: {question.answer or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
