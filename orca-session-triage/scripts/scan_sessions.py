#!/usr/bin/env python3
"""Inventory Orca agent sessions from Orca state and agent transcripts."""

import argparse
import glob
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

DEFAULT_CODEX_SESSIONS = [
    "~/Library/Application Support/orca/codex-accounts/*/home/sessions",
    "~/.codex/sessions",
]


CLAUDE_DIR_LIMIT = 200


def claude_project_dir(projects_root, worktree_path):
    """Claude Code stores a cwd's transcripts under the path with non-alphanumerics as '-'.

    Names longer than 200 characters are cut to 200 and given a hash suffix; see
    find_claude_dir for locating those.
    """
    return Path(projects_root) / re.sub(r"[^A-Za-z0-9]", "-", worktree_path)


def records_cwd(directory, worktree_path):
    for path in directory.glob("*.jsonl"):
        for row in load_jsonl(path):
            if "cwd" in row:
                if row["cwd"] == worktree_path:
                    return True
                break
    return False


def find_claude_dir(projects_root, worktree_path):
    exact = claude_project_dir(projects_root, worktree_path)
    if exact.is_dir() or len(exact.name) <= CLAUDE_DIR_LIMIT or not Path(projects_root).is_dir():
        return exact
    prefix = exact.name[:CLAUDE_DIR_LIMIT]
    for directory in Path(projects_root).iterdir():
        if directory.name.startswith(prefix) and records_cwd(directory, worktree_path):
            return directory
    return exact


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def message_of(entry):
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def user_text(entry):
    content = message_of(entry).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def is_real_user_message(entry):
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    value = user_text(entry)
    return bool(value) and not value.lstrip().startswith("<")


def read_claude(projects_root, worktree_path):
    """Summarize the newest Claude transcript for a worktree, or None when there is none."""
    candidates = list(find_claude_dir(projects_root, worktree_path).glob("*.jsonl"))
    if not candidates:
        return None
    path = max(candidates, key=lambda p: p.stat().st_mtime)
    rows = load_jsonl(path)
    last = max((i for i, e in enumerate(rows) if is_real_user_message(e)), default=-1)
    texts, asked, answered = [], {}, set()
    for entry in rows[last + 1:]:
        content = message_of(entry).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if entry.get("type") == "assistant" and kind == "text" and block["text"].strip():
                texts.append(block["text"])
            elif kind == "tool_use" and block.get("name") == "AskUserQuestion":
                asked[block.get("id")] = block.get("input")
            elif kind == "tool_result":
                answered.add(block.get("tool_use_id"))
    return {
        "transcript": str(path),
        "last_user": user_text(rows[last]) if last >= 0 else None,
        "final_text": texts[-1] if texts else None,
        "pending_questions": [q for i, q in asked.items() if i not in answered],
    }


def message_text(payload):
    return " ".join(c.get("text", "") for c in payload.get("content") or [] if isinstance(c, dict))


def summarize_rollout(path):
    """Return (cwd, summary) for one Codex rollout file."""
    cwd, asks, outputs, last_user, last_assistant = None, {}, set(), None, None
    for row in load_jsonl(path):
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        cwd = cwd or payload.get("cwd")
        kind = payload.get("type")
        if kind in ("function_call", "custom_tool_call") and "user_input" in (payload.get("name") or ""):
            try:
                questions = json.loads(payload.get("arguments") or "{}").get("questions")
            except json.JSONDecodeError:
                questions = None
            asks[payload.get("call_id")] = {"asked_at": row.get("timestamp"), "questions": questions}
        elif kind in ("function_call_output", "custom_tool_call_output"):
            outputs.add(payload.get("call_id"))
        elif kind == "message" and payload.get("role") == "user":
            value = message_text(payload)
            if value and not value.lstrip().startswith(("<", "# AGENTS.md instructions")):
                last_user = (row.get("timestamp"), value)
        elif kind == "message" and payload.get("role") == "assistant":
            last_assistant = message_text(payload) or last_assistant
    pending = []
    for call_id, ask in asks.items():
        if call_id in outputs:
            continue
        later = bool(last_user and ask["asked_at"] and last_user[0] > ask["asked_at"])
        pending.append({**ask, "answered_in_chat_later": later})
    return cwd, {
        "rollout": str(path),
        "last_user": last_user[1] if last_user else None,
        "last_assistant": last_assistant,
        "pending_requests": pending,
    }


def asks_user(final_text):
    lines = [line.strip() for line in (final_text or "").splitlines() if line.strip()]
    return bool(lines) and lines[-1].endswith(("?", "？"))


def waiting_reasons(row, decision_marker):
    reasons = []
    claude = row["claude"] or {}
    if claude.get("pending_questions"):
        reasons.append("AskUserQuestion pending")
    elif asks_user(claude.get("final_text")):
        reasons.append("final message asks")
    if any(not r["answered_in_chat_later"] for s in row["codex"] for r in s["pending_requests"]):
        reasons.append("Codex request pending")
    if decision_marker and (row["comment"] or "").startswith(decision_marker):
        reasons.append("decision marker in comment")
    return reasons


def classify(row, decision_marker):
    """Return (class, reasons): waiting > unstarted > working > finished > idle."""
    reasons = waiting_reasons(row, decision_marker)
    if reasons:
        return "waiting", reasons
    has_transcript = {"claude": bool(row["claude"]), "codex": bool(row["codex"])}
    silent = sorted({t["agent"] for t in row["terminals"] if t["agent"] in has_transcript
                     and not has_transcript[t["agent"]]})
    if silent:
        return "unstarted", [agent + " terminal without a recent transcript" for agent in silent]
    if any(a["state"] == "working" for a in row["agents"]):
        return "working", []
    if (row["linked_pr"] or {}).get("state") == "merged":
        return "finished", ["PR merged"]
    return "idle", []


def build_inventory(worktrees, terminals, claude_root, codex_by_cwd, decision_marker):
    """Join Orca worktrees with their terminals and transcripts and classify each one."""
    by_worktree = {}
    for term in terminals:
        by_worktree.setdefault(term.get("worktreeId"), []).append(term)
    rows = []
    for w in worktrees:
        if w.get("isMainWorktree"):
            continue
        row = {
            "path": w["path"], "repo": w.get("repo"), "branch": w.get("branch"),
            "comment": w.get("comment"), "linked_pr": w.get("linkedPR"),
            "agents": [{"type": a.get("agentType"), "state": a.get("state")} for a in w.get("agents") or []],
            "terminals": [{"handle": t.get("handle"), "agent": t.get("agentIdentity"), "title": t.get("title")}
                          for t in by_worktree.get(w.get("worktreeId"), [])],
            "claude": read_claude(claude_root, w["path"]),
            "codex": codex_by_cwd.get(w["path"], []),
        }
        row["class"], row["reasons"] = classify(row, decision_marker)
        rows.append(row)
    return rows


def read_codex(session_roots, since=None):
    """Group Codex rollout summaries by the cwd each session ran in.

    Rollouts last modified before `since` (epoch seconds) are skipped.
    """
    by_cwd = {}
    for root in session_roots:
        for path in sorted(Path(root).glob("**/rollout-*.jsonl")):
            if since is not None and path.stat().st_mtime < since:
                continue
            cwd, summary = summarize_rollout(path)
            if cwd:
                by_cwd.setdefault(cwd, []).append(summary)
    return by_cwd


def orca_result(orca, args, key, json_file):
    if json_file:
        data = json.loads(Path(json_file).read_text())
    else:
        out = subprocess.run([orca, *args, "--json"], capture_output=True, text=True, check=True).stdout
        data = json.loads(out)
    return data.get("result", data)[key] if isinstance(data, dict) else data


def expand_roots(patterns):
    roots = []
    for pattern in patterns:
        roots.extend(p for p in glob.glob(os.path.expanduser(pattern)) if os.path.isdir(p))
    return roots


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    parser.add_argument("--ps-json", help="read `orca worktree ps --json` output from this file")
    parser.add_argument("--terminals-json", help="read `orca terminal list --json` output from this file")
    parser.add_argument("--claude-projects", default="~/.claude/projects",
                        help="Claude Code transcript root (default: ~/.claude/projects)")
    parser.add_argument("--codex-sessions", action="append",
                        help="Codex session root or glob; repeatable (default: Orca codex accounts and ~/.codex/sessions)")
    parser.add_argument("--since-hours", type=float, default=24,
                        help="skip Codex rollouts not modified within this many hours; an older idle "
                             "Codex then reads as unstarted (default: 24)")
    parser.add_argument("--decision-marker", default="要判断",
                        help="worktree comment prefix that marks a pending human decision (default: 要判断)")
    args = parser.parse_args(argv)

    worktrees = orca_result(args.orca, ["worktree", "ps"], "worktrees", args.ps_json)
    terminals = orca_result(args.orca, ["terminal", "list"], "terminals", args.terminals_json)
    since = time.time() - args.since_hours * 3600 if args.since_hours else None
    codex = read_codex(expand_roots(args.codex_sessions or DEFAULT_CODEX_SESSIONS), since)
    rows = build_inventory(worktrees, terminals, os.path.expanduser(args.claude_projects), codex,
                           args.decision_marker)
    json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
