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

# A line holding only one of these is a shell waiting for input (starship and
# similar prompts put the glyph on its own line). Prompts that carry text, such
# as `taihei=#` or `42%`, may belong to a running program, so they never count.
BARE_PROMPTS = ("❯", "$", "%")


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
    cwd, asks, outputs, last_user, last_assistant, last_role = None, {}, set(), None, None, None
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
                last_user, last_role = (row.get("timestamp"), value), "user"
        elif kind == "message" and payload.get("role") == "assistant":
            value = message_text(payload)
            if value:
                last_assistant, last_role = value, "assistant"
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
        "last_role": last_role,
        "pending_requests": pending,
    }


def asks_user(final_text):
    """True when one of the last three lines is a question; agents often add a reason after it."""
    lines = [line.strip() for line in (final_text or "").splitlines() if line.strip()]
    return any(line.endswith(("?", "？")) for line in lines[-3:])


def waiting_reasons(row, decision_marker):
    reasons = []
    claude = row["claude"] or {}
    if claude.get("pending_questions"):
        reasons.append("AskUserQuestion pending")
    elif asks_user(claude.get("final_text")):
        reasons.append("final message asks")
    if any(not r["answered_in_chat_later"] for s in row["codex"] for r in s["pending_requests"]):
        reasons.append("Codex request pending")
    latest = row["codex"][-1] if row["codex"] else {}
    if latest.get("last_role") == "assistant" and asks_user(latest.get("last_assistant")):
        reasons.append("Codex final message asks")
    if any(a["state"] == "waiting" for a in row["agents"]):
        reasons.append("Orca agent waiting")
    if decision_marker and (row["comment"] or "").startswith(decision_marker):
        reasons.append("decision marker in comment")
    return reasons


def classify(row, decision_marker):
    """Return (class, reasons): waiting > unstarted > working > finished > stalled.

    A stalled worktree has nothing running and nothing asked, yet is still open: it
    waits on the user's next step (merge, review request, close, next task).
    """
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
    if row["status"] == "completed" and (row["linked_pr"] or {}).get("state") != "open":
        return "finished", ["board status completed"]
    return "stalled", []


def shell_is_idle(term, now, idle_minutes):
    """A shell whose screen ends at a bare prompt and that printed nothing for idle_minutes."""
    lines = [line.strip() for line in (term.get("preview") or "").splitlines() if line.strip()]
    last_output = term.get("lastOutputAt")
    quiet = last_output is not None and now - last_output / 1000 >= idle_minutes * 60
    return bool(lines) and lines[-1] in BARE_PROMPTS and quiet


def closable_terminals(row_class, terms, now, idle_minutes):
    """Handles to close in a kept worktree: idle shells, plus agents once nothing is pending."""
    agents_done = row_class in ("finished", "stalled")
    return [t["handle"] for t in terms if t.get("handle")
            and (agents_done if t.get("agentIdentity") else shell_is_idle(t, now, idle_minutes))]


def build_inventory(worktrees, terminals, claude_root, codex_by_cwd, decision_marker,
                    now=None, shell_idle_minutes=30):
    """Join Orca worktrees with their terminals and transcripts and classify each one."""
    now = time.time() if now is None else now
    by_worktree = {}
    for term in terminals:
        if not term.get("orphaned"):
            by_worktree.setdefault(term.get("worktreeId"), []).append(term)
    rows = []
    for w in worktrees:
        if w.get("isMainWorktree"):
            continue
        terms = by_worktree.get(w.get("worktreeId"), [])
        row = {
            "path": w["path"], "repo": w.get("repo"), "branch": w.get("branch"),
            "comment": w.get("comment"), "linked_pr": w.get("linkedPR"),
            "status": w.get("workspaceStatus"),
            "agents": [{"type": a.get("agentType"), "state": a.get("state")} for a in w.get("agents") or []],
            "terminals": [{"handle": t.get("handle"), "agent": t.get("agentIdentity"), "title": t.get("title")}
                          for t in terms],
            "claude": read_claude(claude_root, w["path"]),
            "codex": codex_by_cwd.get(w["path"], []),
        }
        row["class"], row["reasons"] = classify(row, decision_marker)
        row["closable"] = closable_terminals(row["class"], terms, now, shell_idle_minutes)
        row["meaningful_tabs"] = [t.get("handle") or "(no handle) " + (t.get("title") or "")
                                  for t in terms if t.get("handle") not in row["closable"]]
        rows.append(row)
    return rows


def github_slug(remote_url):
    """owner/repo of a github.com remote; None for any other host."""
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", remote_url or "")
    return match.group(1) if match else None


def git_state(path):
    """Branch, uncommitted paths, commits not pushed to the upstream, and the GitHub repo.

    None outside a git checkout; `unpushed` is None without an upstream.
    """
    def run(*args):
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=30)

    try:
        status = run("status", "--porcelain")
        if status.returncode != 0:
            return None
        ahead = run("rev-list", "--count", "@{u}..HEAD")
        branch = run("symbolic-ref", "-q", "--short", "HEAD").stdout.strip() or None
        head = run("rev-parse", "HEAD").stdout.strip() or None
        origin = run("remote", "get-url", "origin").stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {
        "branch": branch, "head": head,
        "dirty": len(status.stdout.splitlines()),
        "unpushed": int(ahead.stdout) if ahead.returncode == 0 else None,
        "github": github_slug(origin),
    }


def summarize_pr(data):
    """Reduce `gh pr view --json` output to what decides a stalled worktree's next step."""
    checks = {}
    for check in data.get("statusCheckRollup") or []:
        key = (check.get("conclusion") or check.get("state") or check.get("status") or "UNKNOWN").upper()
        checks[key] = checks.get(key, 0) + 1
    return {
        "number": data.get("number"), "url": data.get("url"), "state": data.get("state"),
        "head": data.get("headRefName"), "head_oid": data.get("headRefOid"),
        "review": data.get("reviewDecision") or None,
        "requested": [r.get("login") or r.get("name") for r in data.get("reviewRequests") or []],
        # GitHub reports a deleted account's review with a null author, shown as "ghost".
        "reviews": [((r.get("author") or {}).get("login") or "ghost") + ":" + r.get("state", "")
                    for r in data.get("latestReviews") or []],
        "merge_state": data.get("mergeStateStatus"), "checks": checks,
    }


PR_FIELDS = ("number,url,state,headRefName,headRefOid,reviewDecision,reviewRequests,latestReviews,"
             "mergeStateStatus,statusCheckRollup")


def gh_pr(slug, ref):
    """Summary of a GitHub PR by number or head branch, or None when gh finds none or fails."""
    try:
        out = subprocess.run(["gh", "pr", "view", str(ref), "-R", slug, "--json", PR_FIELDS],
                             capture_output=True, text=True, timeout=30)
        return summarize_pr(json.loads(out.stdout)) if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def add_next_step_facts(rows, git_lookup=git_state, pr_lookup=gh_pr):
    """Return rows where each stalled row also carries its git and PR state.

    `pr.head_matches` is False when the PR found (a branch name can be reused)
    does not point at the checkout's HEAD.
    """
    result = []
    for row in rows:
        if row["class"] != "stalled":
            result.append(row)
            continue
        git = git_lookup(row["path"])
        ref = (row["linked_pr"] or {}).get("number") or (git or {}).get("branch")
        pr = pr_lookup(git["github"], ref) if git and git.get("github") and ref else None
        if pr is not None:
            pr = {**pr, "head_matches": pr.get("head_oid") == git.get("head")}
        result.append({**row, "git": git, "pr": pr})
    return result


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
                        help="skip Codex rollouts not modified within this many hours; an older quiet "
                             "Codex then reads as unstarted (default: 24)")
    parser.add_argument("--decision-marker", default="要判断",
                        help="worktree comment prefix that marks a pending human decision (default: 要判断)")
    parser.add_argument("--shell-idle-minutes", type=float, default=30,
                        help="a shell at its prompt counts as closable after this long without output "
                             "(default: 30)")
    args = parser.parse_args(argv)

    worktrees = orca_result(args.orca, ["worktree", "ps"], "worktrees", args.ps_json)
    terminals = orca_result(args.orca, ["terminal", "list"], "terminals", args.terminals_json)
    since = time.time() - args.since_hours * 3600 if args.since_hours else None
    codex = read_codex(expand_roots(args.codex_sessions or DEFAULT_CODEX_SESSIONS), since)
    rows = add_next_step_facts(build_inventory(worktrees, terminals, os.path.expanduser(args.claude_projects),
                                               codex, args.decision_marker,
                                               shell_idle_minutes=args.shell_idle_minutes))
    json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
