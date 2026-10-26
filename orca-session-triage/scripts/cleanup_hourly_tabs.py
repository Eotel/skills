#!/usr/bin/env python3
"""Close idle previous scheduled triage tabs; keep the shared workspace.

Requires Orca run, terminal, agent, question, transcript, and rendered-screen evidence.
"""

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import hourly_runtime

SPINNERS = set("◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✻✽✢·")
BUSY = ("esc to interrupt", "Working (", "Enter to select", "Enter to confirm", "Esc to cancel")
QUESTION = re.compile(r"[?？]|ください|お願いします|ご判断|ご回答|選んで|教えて|"
                      r"(?:です|ます|でしょう|よろしい|します)か[。.!]?\s*$|"
                      r"\b(?:please|let me know|should I|would you)\b", re.IGNORECASE)


def prior_tabs(runs, terminals, agents, workspace, current_pane):
    """Only completed scheduled runs with an idle, owned terminal can be closed."""
    terms = {f"{term.get('tabId')}:{term.get('leafId')}": term for term in terminals
             if hourly_runtime.workspace_path(term.get("worktreeId")) == os.path.realpath(workspace)}
    for run in runs:
        pane = run.get("terminalPaneKey")
        if run.get("status") != "completed" or not pane or pane == current_pane:
            continue
        term = terms.get(pane)
        owned_agents = [agent for agent in agents
                        if agent.get("paneKey") == pane or agent.get("parentPaneKey") == pane]
        if (not term or not term.get("handle") or not term.get("connected") or
                term.get("orphaned") or not owned_agents or
                any(agent.get("state") != "done" or
                    (agent.get("mainAgent") or {}).get("state") != "done"
                    for agent in owned_agents)):
            continue
        yield run, term


def transcript_for(run, home):
    session_id = (run.get("usage") or {}).get("providerSessionId")
    if not session_id or "/" in session_id:
        return None
    found = list((home / ".claude/projects").glob(f"*/{session_id}.jsonl"))
    return found[0] if len(found) == 1 else None


def newest_activity(transcript):
    newest = None
    with transcript.open(encoding="utf-8", errors="replace") as stream:
        for raw in deque(stream, maxlen=400):
            try:
                entry = json.loads(raw)
                if entry.get("type") not in ("user", "assistant", "response_item", "event_msg"):
                    continue
                stamp = entry.get("timestamp")
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
                if when and (newest is None or when > newest):
                    newest = when
            except (ValueError, TypeError, AttributeError):
                continue
    return newest


def unanswered_conversation(transcript):
    """Keep a tab if its latest exchange may still need the user."""
    final_text = None
    pending_tools = set()
    with transcript.open(encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            message = entry.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            blocks = content if isinstance(content, list) else []
            if entry.get("type") == "user" and not entry.get("isMeta") and not entry.get("isCompactSummary"):
                spoken = content if isinstance(content, str) else " ".join(
                    block.get("text", "") for block in blocks if isinstance(block, dict)
                    and block.get("type") == "text")
                if spoken.strip() and not spoken.lstrip().startswith("<"):
                    final_text, pending_tools = None, set()
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if entry.get("type") == "assistant":
                    if block.get("type") == "text" and block.get("text", "").strip():
                        final_text = block["text"]
                    elif block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                        pending_tools.add(block.get("id"))
                elif block.get("type") == "tool_result":
                    pending_tools.discard(block.get("tool_use_id"))
    return bool(pending_tools) or not final_text or bool(QUESTION.search(final_text[-500:]))


def close_blockers(term, transcript, screen, min_idle_minutes, now):
    problems = []
    newest = newest_activity(transcript)
    if newest is None or (now - newest).total_seconds() < min_idle_minutes * 60:
        problems.append("recent or unknown transcript activity")
    if unanswered_conversation(transcript):
        problems.append("unanswered conversation question")
    if (term.get("title") or "")[:1] in SPINNERS:
        problems.append("title is spinning")
    if screen.get("source") != "screen":
        problems.append("rendered screen unavailable")
    if screen.get("draft") not in (None, ""):
        problems.append("unsent draft")
    tail = screen.get("tail") or []
    lines = tail if isinstance(tail, list) else tail.splitlines()
    visible = "\n".join(lines[-15:])
    if any(marker in visible for marker in BUSY):
        problems.append("screen shows active input or work")
    prompts = [line.strip()[1:].strip() for line in lines[-10:]
               if line.strip().startswith(("❯", "›"))]
    if not prompts or prompts[-1] not in ("", "Ask Codex to do anything"):
        problems.append("empty composer is not visible")
    return problems


def no_pending_questions(state_file, pane):
    if not state_file.exists():
        return True
    data = json.loads(state_file.read_text())
    questions = data["questions"]
    return isinstance(questions, dict) and all(
        isinstance(question, dict) and
        (question.get("answer") is not None or
         question.get("owner_pane") not in (None, pane))
        for question in questions.values())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=hourly_runtime.workspace_path(
        os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")))
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"))
    parser.add_argument("--state-file", type=Path, default=Path.home() /
                        ".cache/orca-session-triage/hourly-state.json")
    parser.add_argument("--min-idle-minutes", type=int, default=5)
    args = parser.parse_args(argv)
    if not args.workspace:
        parser.error("--workspace or ORCA_WORKTREE_ID is required")
    try:
        runs = hourly_runtime.scheduled_runs(args.orca, args.workspace)
        terminals = hourly_runtime.orca_result(args.orca, "terminal", "list")["terminals"]
        page = hourly_runtime.orca_result(args.orca, "worktree", "ps")
        row = next((row for row in page["worktrees"]
                    if os.path.realpath(row["path"]) == os.path.realpath(args.workspace)), None)
        if row is None:
            print("keep: workspace is missing")
            return 0
        current = hourly_runtime.latest_pane(runs)
        for run, term in prior_tabs(runs, terminals, row.get("agents") or [], args.workspace, current):
            if not no_pending_questions(args.state_file, run["terminalPaneKey"]):
                print(f"keep: {term['handle']} may have an unanswered question")
                continue
            transcript = transcript_for(run, Path.home())
            if transcript is None:
                print(f"keep: {term['handle']} has no unique transcript")
                continue
            read = hourly_runtime.orca_result(args.orca, "terminal", "read", "--terminal",
                                              term["handle"], "--screen")
            screen = read.get("terminal") or read
            problems = close_blockers(term, transcript, screen, args.min_idle_minutes,
                                      datetime.now(timezone.utc))
            if problems:
                print(f"keep: {term['handle']}: " + "; ".join(problems))
                continue
            hourly_runtime.orca_result(args.orca, "terminal", "close", "--terminal", term["handle"])
            print(f"closed: {term['handle']}")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"keep: could not verify previous tabs: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
