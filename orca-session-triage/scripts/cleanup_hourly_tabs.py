#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Close idle previous scheduled triage tabs; keep the shared workspace.

Requires Orca run, terminal, agent, question, transcript, and rendered-screen evidence.
"""

import argparse
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import hourly_runtime
import scan_sessions

SPINNERS = set("◐◑◒◓⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✻✽✢·")
# Claude Code's selectors say "Enter to select"; Codex's pickers (model switch, /model) say "enter select · esc back".
BUSY = ("esc to interrupt", "Working (", "Enter to select", "Enter to confirm", "Esc to cancel",
        "enter select", "esc back")
QUESTION = re.compile(r"[?？]|ください|お願いします|ご判断|ご回答|選んで|教えて|"
                      r"(?:です|ます|でしょう|よろしい|します)か|"
                      r"\b(?:please|let me know|should I|would you)\b", re.IGNORECASE)


@dataclass(frozen=True)
class PaneSession:
    session_id: str
    transcript: Path | None
    agent: dict[str, Any]


def saved_sessions(path: Path, workspace: str) -> dict[str, PaneSession]:
    """The provider conversation each pane last reported through Orca's agent hooks."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    sessions = {}
    for pane, entry in (data.get("entries") or {}).items():
        if hourly_runtime.workspace_path(entry.get("worktreeId")) != os.path.realpath(workspace):
            continue
        provider = entry.get("providerSession") or {}
        sessions[pane] = PaneSession(provider.get("id") or "",
                                     Path(provider["transcriptPath"]) if provider.get("transcriptPath") else None,
                                     entry.get("payload") or {})
    return sessions


def prior_tabs(runs: list[dict[str, Any]], terminals: list[dict[str, Any]], agents: list[dict[str, Any]],
               workspace: str, current_pane: str | None,
               sessions: dict[str, PaneSession] | None = None) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Only completed scheduled runs with an idle, owned terminal can be closed."""
    sessions = sessions or {}
    recorded = [session.transcript for session in sessions.values() if session.transcript]
    transcripts = [(run, transcript_for(run, Path.home(), recorded)) for run in runs
                   if run.get("status") == "completed" and not run.get("terminalPaneKey")]
    for term in terminals:
        if hourly_runtime.workspace_path(term.get("worktreeId")) != os.path.realpath(workspace):
            continue
        pane = hourly_runtime.pane_key(term)
        if pane in (current_pane, os.environ.get("ORCA_PANE_KEY")):
            continue
        matches = [run for run in runs if run.get("terminalPaneKey") == pane]
        saved = sessions.get(pane)
        if not matches and term.get("orphaned") and term.get("connected"):
            matches = [run for run, transcript in transcripts
                       if saved and transcript and transcript.stem == saved.session_id]
        if not matches and not term.get("orphaned"):
            continue
        if len(matches) != 1:
            print(f"keep: {term.get('handle') or pane}: no unique scheduled run and transcript")
            continue
        run = matches[0]
        owned_agents = [agent for agent in agents
                        if agent.get("paneKey") == pane or agent.get("parentPaneKey") == pane]
        if not owned_agents and term.get("orphaned") and saved and saved.agent:
            owned_agents = [saved.agent]
        problems = []
        if run.get("status") != "completed":
            problems.append("scheduled run is not completed")
        if not term.get("handle") or not term.get("connected"):
            problems.append("terminal is not connected or has no handle")
        if not owned_agents or any(agent.get("state") != "done" or
                                   (agent.get("mainAgent") or {}).get("state") != "done"
                                   for agent in owned_agents):
            problems.append("agent or background work is active or unknown")
        if problems:
            print(f"keep: {term.get('handle') or pane}: " + "; ".join(problems))
            continue
        yield {**run, "terminalPaneKey": pane}, term


def transcript_for(run: dict[str, Any], home: Path, recorded: Iterable[Path] = ()) -> Path | None:
    recorded = [path for path in recorded if path.is_file()]
    session_id = (run.get("usage") or {}).get("providerSessionId")
    if session_id and "/" not in session_id:
        found = {path.resolve(): path for path in recorded if path.stem == session_id}
        found.update((path.resolve(), path) for path in (home / ".claude/projects").glob(f"*/{session_id}.jsonl"))
        if len(found) == 1:
            return next(iter(found.values()))
    started = run.get("startedAt")
    workspace = hourly_runtime.workspace_path(run.get("workspaceId"))
    if not isinstance(started, (int, float)) or not workspace:
        return None
    directory = scan_sessions.find_claude_dir(home / ".claude/projects", workspace)
    matches = []
    paths = {path.resolve(): path for path in recorded}
    paths.update((path.resolve(), path) for path in directory.glob("*.jsonl"))
    for path in paths.values():
        with path.open(encoding="utf-8", errors="replace") as stream:
            for raw in stream:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if entry.get("type") != "user" or entry.get("isMeta"):
                    continue
                prompt = scan_sessions.user_text(entry) or ""
                if not prompt:
                    continue
                stamp = entry.get("timestamp")
                scheduled_prompt = (prompt.startswith("/orca-session-triage hourly") or
                                    "<command-name>/orca-session-triage</command-name>" in prompt and
                                    "<command-args>hourly" in prompt)
                if stamp and scheduled_prompt:
                    begun = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
                    if -5000 <= begun - started <= 300000:
                        matches.append(path)
                break
    return matches[0] if len(matches) == 1 else None


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


def latest_exchange(transcript: Path) -> tuple[str | None, bool]:
    """The latest exchange's final text, and whether an AskUserQuestion in it still has no answer."""
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
    return final_text, bool(pending_tools)


def requests(text: str) -> list[str]:
    """Lines of a final message that ask the user something, for the current run to carry over."""
    return [line.strip() for line in text.splitlines() if QUESTION.search(line)]


def close_blockers(term, transcript, screen, min_idle_minutes, now):
    problems = []
    newest = newest_activity(transcript)
    if newest is None or (now - newest).total_seconds() < min_idle_minutes * 60:
        problems.append("recent or unknown transcript activity")
    final_text, asking = latest_exchange(transcript)
    if asking or not final_text:
        problems.append("unanswered conversation question")
    if (term.get("title") or "")[:1] in SPINNERS:
        problems.append("title is spinning")
    if screen.get("source") != "screen":
        problems.append("rendered screen unavailable")
    if scan_sessions.has_draft(screen):
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
        (question.get("answer") is not None or question.get("withdrawn_at") or
         question.get("owner_pane") not in (None, pane))
        for question in questions.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=hourly_runtime.workspace_path(
        os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")))
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"))
    parser.add_argument("--state-file", type=Path, default=Path.home() /
                        ".cache/orca-session-triage/hourly-state.json")
    parser.add_argument("--min-idle-minutes", type=int, default=5)
    parser.add_argument("--agent-status-file", type=Path, default=Path.home() /
                        "Library/Application Support/orca/agent-hooks/last-status.json",
                        help="Orca's saved pane and provider-session records (default: %(default)s)")
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
        current = hourly_runtime.latest_pane(runs, hourly_runtime.live_panes(terminals))
        sessions = saved_sessions(args.agent_status_file, args.workspace)
        recorded = [session.transcript for session in sessions.values() if session.transcript]
        for run, term in prior_tabs(runs, terminals, row.get("agents") or [], args.workspace, current,
                                    sessions):
            if not no_pending_questions(args.state_file, run["terminalPaneKey"]):
                print(f"keep: {term['handle']} may have an unanswered question")
                continue
            transcript = transcript_for(run, Path.home(), recorded)
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
            final_text, _ = latest_exchange(transcript)
            for line in requests(final_text or ""):
                print(f"carry: {term['handle']}: {line}")
            hourly_runtime.orca_result(args.orca, "terminal", "close", "--terminal", term["handle"])
            print(f"closed: {term['handle']}: completed scheduled run, idle transcript and empty composer")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"keep: could not verify previous tabs: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
