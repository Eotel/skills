#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""List a triage workspace's scheduled hourly runs, newest first, one line each.

Each line gives when the run was scheduled and started (local time), Orca's status,
what the precheck printed when it skipped the run, the run's tab pane, and its Claude
transcript, so neither Orca's automation records nor transcript paths that contain
spaces have to be read by hand. `--json` prints the same fields as a list.

`--prompt` prints the prompt of the workspace's scheduled triage automation, and
`--set-prompt FILE` replaces it with the file's whole text (less its final newline)
through `orca automations edit`, then reads it back: when Orca keeps another text,
it prints the difference and exits 1.
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import unified_diff
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


def triage_automation(orca: str, workspace: str) -> dict[str, Any]:
    """The workspace's one scheduled triage automation; ValueError names the workspaces that have one instead."""
    found = hourly_runtime.hourly_automations(orca)
    here = os.path.realpath(workspace)
    mine = [automation for automation in found if hourly_runtime.workspace_path(automation.get("workspaceId")) == here]
    if len(mine) == 1:
        return mine[0]
    if mine:
        raise ValueError(f"{len(mine)} scheduled triage automations run in {here}: "
                         + ", ".join(str(automation.get("id")) for automation in mine))
    elsewhere = sorted({hourly_runtime.workspace_path(automation.get("workspaceId")) or "-" for automation in found})
    raise ValueError(f"no scheduled triage automation runs in {here}"
                     + (f"; pass --workspace with one of: {', '.join(elsewhere)}" if elsewhere else ""))


def set_prompt(orca: str, workspace: str, path: Path) -> int:
    """Replace the triage automation's prompt with the file's text, then compare what Orca reads back."""
    prompt = path.read_text(encoding="utf-8").removesuffix("\n")
    if not prompt.startswith(hourly_runtime.HOURLY_PROMPT):
        raise ValueError(f"{path} does not start with {hourly_runtime.HOURLY_PROMPT!r}: the scheduled-run tools "
                         "find the automation by it")
    automation_id = str(triage_automation(orca, workspace)["id"])
    hourly_runtime.orca_result(orca, "automations", "edit", automation_id, "--prompt", prompt)
    saved = next((str(automation.get("prompt") or "")
                  for automation in hourly_runtime.orca_result(orca, "automations", "list")["automations"]
                  if automation.get("id") == automation_id), "")
    if saved != prompt:
        print(f"hourly runs: Orca reads back another prompt for {automation_id}:", file=sys.stderr)
        sys.stderr.writelines(unified_diff(prompt.splitlines(keepends=True), saved.splitlines(keepends=True),
                                           str(path), f"orca automation {automation_id}"))
        return 1
    print(f"prompt set: {automation_id} ({len(prompt)} characters, read back the same)")
    return 0


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
    prompt = parser.add_mutually_exclusive_group()
    prompt.add_argument("--prompt", action="store_true",
                        help="print the prompt of the workspace's scheduled triage automation instead of its runs")
    prompt.add_argument("--set-prompt", type=Path, metavar="FILE",
                        help="replace that prompt with FILE's whole text (less its final newline; it must start "
                             f"with {hourly_runtime.HOURLY_PROMPT}), read it back, and exit 1 printing the "
                             "difference when Orca keeps another")
    args = parser.parse_args(argv)
    if not args.workspace:
        parser.error("--workspace or ORCA_WORKTREE_ID is required")
    if args.prompt or args.set_prompt:
        try:
            if args.set_prompt:
                return set_prompt(args.orca, args.workspace, args.set_prompt)
            print(triage_automation(args.orca, args.workspace)["prompt"])
            return 0
        except subprocess.CalledProcessError as error:
            print(f"hourly runs: orca {' '.join(error.cmd[1:3])} failed: "
                  f"{(error.stderr or error.stdout or '').strip()}", file=sys.stderr)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            print(f"hourly runs: {error}", file=sys.stderr)
        return 1
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
