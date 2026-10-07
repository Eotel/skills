#!/usr/bin/env python3
"""Inventory Orca agent sessions from Orca state and agent transcripts."""

import argparse
from datetime import datetime, timezone
import glob
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

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
    """A message the user typed; meta entries and compaction summaries are Claude Code's own."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
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
    texts, final_at, asked, answered, api_error = [], None, {}, set(), None
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
                final_at = entry.get("timestamp")
                api_error = block["text"] if entry.get("isApiErrorMessage") else None
            elif kind == "tool_use" and block.get("name") == "AskUserQuestion":
                asked[block.get("id")] = block.get("input")
            elif kind == "tool_result":
                answered.add(block.get("tool_use_id"))
    return {
        "transcript": str(path),
        "last_user": user_text(rows[last]) if last >= 0 else None,
        "last_user_at": rows[last].get("timestamp") if last >= 0 else None,
        "final_text": texts[-1] if texts else None,
        "final_at": final_at,
        "pending_questions": [q for i, q in asked.items() if i not in answered],
        "api_error": api_error,
    }


def message_text(payload):
    return " ".join(c.get("text", "") for c in payload.get("content") or [] if isinstance(c, dict))


def summarize_rollout(path):
    """Return (cwd, summary) for one Codex rollout file."""
    cwd, asks, outputs, last_user, last_role = None, {}, set(), None, None
    last_assistant = last_assistant_at = None
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
                last_assistant, last_assistant_at, last_role = value, row.get("timestamp"), "assistant"
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
        "last_assistant_at": last_assistant_at,
        "last_role": last_role,
        "pending_requests": pending,
    }


# Jev (TypeSafe) answers typed questions about a text: a yes/no probability
# (noul) and a pick among named options with a confidence (choice).
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_QUESTIONS = {
    # Asked apart from the state: as one more option, "waits for your
    # instruction" read as in_progress.
    "asks_user": {"type": "noul", "instructions": (
        "Does this message end by waiting for the user's answer, decision, or instruction "
        "before the agent can continue?")},
    "status": {"type": "choice",
               "instructions": "What is the state of the agent's work, judging from its last message?",
               "criteria": {
                   "in_progress": "still working, or waiting on its own helper, background job, or CI",
                   # Agents end with what they did not verify; read as work left, a
                   # merged and finished worktree stayed stalled (2026-10-06).
                   "blocked_on_others": "its part is not finished until someone else acts, such as a "
                                        "reviewer who has to review or merge, a coordinator, or another team",
                   "done": "the work is finished; notes on what was not verified, or offers of "
                           "optional follow-up such as deleting a branch, do not count as work left"}},
}
JEV_MAX_CHARS = 4000  # the ask sits at the end; earlier context rarely changes the answer
JEV_TIMEOUT = 15
JEV_KEY_FILE = "~/.config/typesafe/api_key"
# Jev is sure from these bounds on; a score between them, or no score, is
# unsure and the final message is read before the row is classed.
ASKS_FROM = 0.7
SETTLED_BELOW = 0.3
STATE_SURE_FROM = 0.6  # a status confidence below this is unsure
NO_VERDICT = {"asks": None, "status": None, "confidence": None}


def jev_key(env, key_file):
    """TYPESAFE_API_KEY from the environment, else the key file's content, else None."""
    if env.get("TYPESAFE_API_KEY"):
        return env["TYPESAFE_API_KEY"]
    try:
        return Path(key_file).expanduser().read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def probability(value):
    value = float(value)
    if not 0 <= value <= 1:  # also false for NaN
        raise ValueError(f"probability out of range: {value}")
    return value


def jev_judge(api_key, opener=None):
    """A judge returning Jev's verdict on a message, or None on failure.

    The verdict holds `asks` (probability that it waits on the user), `status`
    (in_progress, blocked_on_others, or done), and that status's `confidence`.
    """
    def judge(message):
        body = {"state": message[-JEV_MAX_CHARS:], "model": "jev-latest", "questions": JEV_QUESTIONS}
        request = urllib.request.Request(JEV_URL, data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
        try:
            with (opener or urllib.request.urlopen)(request, timeout=JEV_TIMEOUT) as response:
                answers = json.load(response)["answers"]
            status = answers["status"]["choice"]
            if status not in JEV_QUESTIONS["status"]["criteria"]:
                raise ValueError(f"unknown status: {status!r}")
            return {"asks": probability(answers["asks_user"]["noul"]), "status": status,
                    "confidence": probability(answers["status"]["confidence"])}
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
            print(f"Jev could not judge a final message: {error!r}", file=sys.stderr)
            return None
    return judge


def is_working(row):
    return any(a["state"] == "working" for a in row["agents"])


def main_state(agent):
    main = agent.get("mainAgent")
    return main.get("state") if isinstance(main, dict) else None


def turn_runs(row):
    """An agent is in its own turn, not only holding a background shell, monitor, or helper.

    Orca keeps `state` at working while such a job runs after the turn ended;
    `mainAgent.state` then says done.
    """
    return any(a["state"] == "working" and a.get("turn") != "done" for a in row["agents"])


def latest_message(row):
    """(source, text) of the newest final message, Claude's when the times tie or are missing.

    A helper's report to its lead comes before the lead's own last word, so only
    the newest one says how the worktree stands. Codex counts only when it spoke
    after the user.
    """
    claude = row["claude"] or {}
    latest = row["codex"][-1] if row["codex"] else {}
    codex_spoke = latest.get("last_role") == "assistant" and latest.get("last_assistant")
    if not claude.get("final_text"):
        return ("codex", latest["last_assistant"]) if codex_spoke else None
    claude_at, codex_at = claude.get("final_at"), latest.get("last_assistant_at")
    if codex_spoke and claude_at and codex_at and codex_at > claude_at:
        return "codex", latest["last_assistant"]
    return "claude", claude["final_text"]


def cut_off(row):
    """The API error that ended Claude's last turn, or None.

    Claude Code does not resume such a turn, and a monitor it left running keeps
    Orca at working, so only the transcript shows the agent stopped mid-task.
    """
    return (row["claude"] or {}).get("api_error")


def is_sure_ask(asks):
    return asks is not None and asks >= ASKS_FROM


def unsure_reason(jev):
    if jev["asks"] is None:
        return "Jev could not judge: read the final message"
    if SETTLED_BELOW <= jev["asks"] < ASKS_FROM:
        return f"Jev unsure whether it asks ({jev['asks']:.2f}): read the final message"
    if jev["confidence"] < STATE_SURE_FROM:
        return f"Jev unsure of the state ({jev['status']} {jev['confidence']:.2f}): read the final message"
    return None


BACKGROUND_REASON = "a background job runs after the agent's turn"
STATE_REASONS = {
    "in_progress": "final message says work continues, yet no agent runs",
    "blocked_on_others": "final message waits on someone else",
    "done": "final message says done",
}


def waiting_reasons(row, decision_marker):
    reasons = []
    claude = row["claude"] or {}
    jev = row.get("jev") or {}
    asks = jev.get("asks")
    if claude.get("pending_questions"):
        reasons.append("AskUserQuestion pending")
    elif is_sure_ask(asks) and jev["source"] == "claude":
        reasons.append(f"final message asks (Jev {asks:.2f})")
    if any(not r["answered_in_chat_later"] for s in row["codex"] for r in s["pending_requests"]):
        reasons.append("Codex request pending")
    if is_sure_ask(asks) and jev["source"] == "codex":
        reasons.append(f"Codex final message asks (Jev {asks:.2f})")
    if any(a["state"] == "waiting" for a in row["agents"]):
        reasons.append("Orca agent waiting")
    if decision_marker and (row["comment"] or "").startswith(decision_marker):
        reasons.append("decision marker in comment")
    return reasons


NO_SESSION_REASON = "no agent session in this worktree"


def classify(row, decision_marker):
    """Return (class, reasons): waiting > unstarted > working > unsure > finished > stalled.

    Jev reads the newest final message of an idle worktree. When it is unsure,
    or gave no usable answer, the worktree is unsure: read the message and class
    it yourself before anything closes. Only work Jev calls done can finish; a
    stalled worktree has nothing running and nothing asked, yet is still open,
    and its reason says what Jev read (work continues, waits on someone else,
    or done but not merged). A worktree Orca calls working only for a background
    job is read too: a question makes it waiting or unsure, and it never
    finishes while the job runs. A turn an API error cut off, or a lead that
    left its workers' mail unread, is stalled even then; unread mail is also
    named on a waiting row.
    """
    reasons = waiting_reasons(row, decision_marker)
    mail = [mail_reason(row["unread_mail"])] if row.get("unread_mail") else []
    if reasons:
        return "waiting", reasons + mail
    has_transcript = {"claude": bool(row["claude"]), "codex": bool(row["codex"])}
    silent = sorted({t["agent"] for t in row["terminals"] if t["agent"] in has_transcript
                     and not has_transcript[t["agent"]]})
    if silent:
        return "unstarted", [agent + " terminal without a recent transcript" for agent in silent]
    jev = row.get("jev")
    if turn_runs(row):
        return "working", []
    if cut_off(row):
        return "stalled", ["turn ended with an API error: " + cut_off(row)]
    if mail:
        return "stalled", mail
    if is_working(row):
        if jev and (jev["asks"] is None or jev["asks"] >= SETTLED_BELOW):
            return "unsure", [unsure_reason(jev)]
        return "working", [BACKGROUND_REASON]
    if jev and unsure_reason(jev):
        return "unsure", [unsure_reason(jev)]
    state = f"{STATE_REASONS[jev['status']]} (Jev {jev['confidence']:.2f})" if jev else None
    if jev and jev["status"] != "done":
        return "stalled", [state]
    if (row["linked_pr"] or {}).get("state") == "merged":
        return "finished", ["PR merged"]
    if row["status"] == "completed" and (row["linked_pr"] or {}).get("state") != "open":
        return "finished", ["board status completed"]
    if not (row["agents"] or row["claude"] or row["codex"]):
        return "stalled", [NO_SESSION_REASON]
    return "stalled", [state] if state else []


def shell_is_idle(term, now, idle_minutes):
    """A shell whose screen ends at a bare prompt and that printed nothing for idle_minutes."""
    lines = [line.strip() for line in (term.get("preview") or "").splitlines() if line.strip()]
    last_output = term.get("lastOutputAt")
    quiet = last_output is not None and now - last_output / 1000 >= idle_minutes * 60
    return bool(lines) and lines[-1] in BARE_PROMPTS and quiet


def agents_done(row):
    """Nothing is pending on the agents: closing their tabs loses no work.

    An agent whose last message says work continues may be waiting on a
    background job that its tab still holds. A turn an API error cut off, and a
    lead with unread worker mail, are woken in that tab.
    """
    still_working = (row.get("jev") or {}).get("status") == "in_progress"
    to_wake = cut_off(row) or row.get("unread_mail")
    return row["class"] in ("finished", "stalled") and not still_working and not to_wake


def closable_terminals(agents_closable, terms, now, idle_minutes):
    """Handles to close in a kept worktree: idle shells, plus agents once nothing is pending."""
    return [t["handle"] for t in terms if t.get("handle")
            and (agents_closable if t.get("agentIdentity") else shell_is_idle(t, now, idle_minutes))]


def iso_time(epoch_ms):
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def done_cards(agents, terms):
    """One card per agent Orca shows as done, with the live tab that closes it.

    A tab Orca reopened carries a new pane id, so a card whose pane is gone
    takes the worktree's only unclaimed tab of its agent. `handle` is None
    while the tab sleeps, or when several tabs could be its own.
    `last_message` is what Orca kept of the agent's last word and can be empty;
    the row's transcripts hold the rest.
    """
    by_pane = {f"{t.get('tabId')}:{t.get('leafId')}": t.get("handle") for t in terms}
    panes = {a.get("paneKey") for a in agents}
    unclaimed = {}
    for t in terms:
        if t.get("handle") and f"{t.get('tabId')}:{t.get('leafId')}" not in panes:
            unclaimed.setdefault(t.get("agentIdentity"), []).append(t["handle"])

    def handle_of(agent):
        own = by_pane.get(agent.get("paneKey"))
        spare = unclaimed.get(agent.get("agentType"), [])
        alone = sum(1 for a in agents if a.get("agentType") == agent.get("agentType")
                    and by_pane.get(a.get("paneKey")) is None) == 1
        return own or (spare[0] if len(spare) == 1 and alone else None)

    return [{
        "agent": a.get("agentType"), "handle": handle_of(a),
        "done_at": iso_time(a.get("stateStartedAt")), "interrupted": bool(a.get("interrupted")),
        "prompt": a.get("prompt") or "", "last_message": a.get("lastAssistantMessage") or "",
    } for a in agents if a.get("state") == "done"]


def build_inventory(worktrees, terminals, claude_root, codex_by_cwd, decision_marker,
                    now=None, shell_idle_minutes=30, judge=lambda message: None, mail=None):
    """Join Orca worktrees with their terminals and transcripts and classify each one.

    `judge(message)` returns Jev's verdict on the newest final message, a dict of
    `asks`, `status`, and `confidence` (see jev_judge), or None when it failed.
    `mail` maps a lead's terminal handle to its unread worker mail (see
    unread_worker_mail).
    A repository's main checkout is read only while it hosts an agent (`main`).
    """
    now = time.time() if now is None else now
    by_worktree = {}
    for term in terminals:
        # Orphaned and disconnected is the stale record of a closed tab; a tab
        # Orca detached from the window stays connected and still runs.
        if term.get("connected") or not term.get("orphaned"):
            by_worktree.setdefault(term.get("worktreeId"), []).append(term)
    rows = []
    for w in worktrees:
        main = bool(w.get("isMainWorktree"))
        if main and not w.get("agents"):
            continue
        terms = by_worktree.get(w.get("worktreeId"), [])
        row = {
            "path": w["path"], "main": main, "repo": w.get("repo"), "branch": w.get("branch"),
            "comment": w.get("comment"), "linked_pr": w.get("linkedPR"),
            "status": w.get("workspaceStatus"),
            "unread": bool(w.get("unread")),
            "agents": [{"type": a.get("agentType"), "state": a.get("state"), "turn": main_state(a)}
                       for a in w.get("agents") or []],
            "terminals": [{"handle": t.get("handle"), "agent": t.get("agentIdentity"), "title": t.get("title")}
                          for t in terms],
            "done_cards": done_cards(w.get("agents") or [], terms),
            "claude": read_claude(claude_root, w["path"]),
            "codex": codex_by_cwd.get(w["path"], []),
        }
        row["unread_mail"] = unread_since_turn(row, mail or {}, now)
        latest = None if turn_runs(row) or cut_off(row) else latest_message(row)
        row["jev"] = {"source": latest[0], **(judge(latest[1]) or NO_VERDICT)} if latest else None
        row["class"], row["reasons"] = classify(row, decision_marker)
        row["closable"] = closable_terminals(agents_done(row), terms, now, shell_idle_minutes)
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
        "merge_state": data.get("mergeStateStatus"), "merged_at": data.get("mergedAt"), "checks": checks,
    }


PR_FIELDS = ("number,url,state,headRefName,headRefOid,reviewDecision,reviewRequests,latestReviews,"
             "mergeStateStatus,mergedAt,statusCheckRollup")


def gh_pr(slug, ref):
    """Summary of a GitHub PR by number or head branch, or None when gh finds none or fails."""
    try:
        out = subprocess.run(["gh", "pr", "view", str(ref), "-R", slug, "--json", PR_FIELDS],
                             capture_output=True, text=True, timeout=30)
        return summarize_pr(json.loads(out.stdout)) if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def parse_time(value):
    """An aware datetime; a time without a zone is UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


WORKER_MAIL = ("worker_done", "question", "escalation")
MAIL_GRACE_MINUTES = 5  # Orca types its own notice into an idle lead first


def unread_worker_mail(messages, runs):
    """Unread worker reports, questions, and escalations, keyed by the lead terminal they wait on."""
    leads = {run.get("id"): run.get("coordinator_handle") for run in runs}
    mail = {}
    for m in messages:
        run = m.get("run_id")
        if m.get("read") or m.get("type") not in WORKER_MAIL or m.get("to_handle") != f"run:{run}":
            continue
        if leads.get(run):
            mail.setdefault(leads[run], []).append(
                {"type": m["type"], "created_at": m.get("created_at"), "subject": m.get("subject")})
    return mail


def unread_since_turn(row, mail, now, grace_minutes=MAIL_GRACE_MINUTES):
    """The lead's unread worker mail that arrived after its last turn began, oldest first.

    Orca types a notice into an idle lead, yet has left reports unread for hours.
    Mail from before that turn is left out: a lead can read it with `--peek`,
    which leaves it unread, and a notice or wake line that started the turn
    already told it about that mail.
    """
    began = parse_time((row["claude"] or {}).get("last_user_at"))
    if began is None:
        return []
    arrived = []
    for m in (m for t in row["terminals"] for m in mail.get(t["handle"], [])):
        at = parse_time(m.get("created_at"))
        if at is not None and began < at and at.timestamp() <= now - grace_minutes * 60:
            arrived.append(m)
    return sorted(arrived, key=lambda m: m["created_at"])


def mail_reason(unread):
    noun = "message" if len(unread) == 1 else "messages"
    return f"{len(unread)} orchestration {noun} unread since its last turn began (oldest {unread[0]['created_at']})"


def final_message_at(row):
    """When the final message Jev read was written, or None."""
    if (row.get("jev") or {}).get("source") == "codex":
        return parse_time((row.get("codex") or [{}])[-1].get("last_assistant_at"))
    return parse_time((row.get("claude") or {}).get("final_at"))


def merged_after_final_message(row, pr):
    """The worktree's own PR merged after a final message that waited on someone else.

    That message was written before the merge it was waiting for, often a
    review or the user's own merge on GitHub, so the wait it reported is over.
    """
    if not (row["class"] == "stalled" and (row.get("jev") or {}).get("status") == "blocked_on_others"
            and pr.get("state") == "MERGED" and pr.get("head_matches")):
        return False
    said_at, merged_at = final_message_at(row), parse_time(pr.get("merged_at"))
    return said_at is not None and merged_at is not None and merged_at > said_at


def merged_with_nothing_left(row, pr):
    """A checkout no agent ran in whose own PR merged at HEAD.

    Orca's PR link is often missing or still says open after the merge, so the
    PR found by the lookup decides. A row an agent spoke in is not finished
    here even when Jev read it as done: Jev's read is fast and shallow, so that
    row waits in the review queue for a careful one.
    """
    return (row["class"] == "stalled" and row.get("reasons") == [NO_SESSION_REASON]
            and pr.get("state") == "MERGED" and bool(pr.get("head_matches")))


def add_next_step_facts(rows, git_lookup=git_state, pr_lookup=gh_pr):
    """Return rows where each row a reader classes (stalled, unsure, waiting) also carries its git and PR state.

    `pr.head_matches` is False when the PR found (a branch name can be reused)
    does not point at the checkout's HEAD. A stalled row whose final message
    waited on someone else finishes once its PR merged after that message.
    """
    result = []
    for row in rows:
        if row["class"] not in READ_CLASSES:
            result.append(row)
            continue
        git = git_lookup(row["path"])
        ref = (row["linked_pr"] or {}).get("number") or (git or {}).get("branch")
        pr = pr_lookup(git["github"], ref) if git and git.get("github") and ref else None
        if pr is not None:
            pr = {**pr, "head_matches": pr.get("head_oid") == git.get("head")}
        row = {**row, "git": git, "pr": pr}
        if pr is not None and merged_after_final_message(row, pr):
            row = {**row, "class": "finished", "reasons": ["PR merged after the final message"]}
        elif pr is not None and merged_with_nothing_left(row, pr):
            row = {**row, "class": "finished", "reasons": ["PR merged"]}
        result.append(row)
    return result


QUEUE_FIELDS = ("path", "repo", "class", "reasons", "comment", "jev", "pr", "git", "claude")
VERDICTS_FILE = "~/.cache/orca-session-triage/verdicts.json"
VERDICT_CLASSES = ("waiting", "blocked", "in_progress", "done_unmerged", "finished")
READ_CLASSES = ("unsure", "stalled", "waiting")


def row_final_at(row):
    """When the newest final message a reader would class was written (Claude or Codex), or None."""
    codex = row.get("codex") or []
    said = [at for at in ((row.get("claude") or {}).get("final_at"),
                          codex[-1].get("last_assistant_at") if codex else None) if at]
    return max(said, key=lambda at: parse_time(at) or datetime.min.replace(tzinfo=timezone.utc), default=None)


def read_key(row):
    """What a careful read of the row saw: its final message, its PR, and its tree. None without a message.

    A verdict holds only while this key is unchanged, so a merge, a new push, or
    new uncommitted work sends the row back for another read.
    """
    final_at = row_final_at(row)
    if final_at is None:
        return None
    pr, git = row.get("pr") or {}, row.get("git") or {}
    return json.dumps([final_at, pr.get("state"), pr.get("number"), pr.get("head_oid"), git.get("dirty")])


def current_verdict(row, verdicts):
    """The stored verdict for what the row shows now, or None."""
    key = read_key(row)
    verdict = verdicts.get(row["path"]) if key is not None else None
    return verdict if isinstance(verdict, dict) and verdict.get("read") == key else None


def review_queue(rows, verdicts=None):
    """Rows a careful reader must class before anything closes.

    Jev reads a final message fast and shallow, so every row it judged from one
    (unsure, stalled, or waiting) is queued, and so is a stalled row whose own PR
    merged although its message did not say it was done. A row keeps the verdict
    orca-row-classify gave it while its message, PR, and tree stay as read, so
    only new or changed rows are read again.
    """
    verdicts = verdicts or {}

    def needs_reader(row):
        merged = (row.get("pr") or {}).get("state") == "MERGED"
        judged = row.get("jev") is not None and row["class"] in READ_CLASSES
        if not (judged or row["class"] == "unsure" or (row["class"] == "stalled" and merged)):
            return False
        return current_verdict(row, verdicts) is None

    return [{**{key: row.get(key) for key in QUEUE_FIELDS},
             "codex": row["codex"][-1] if row.get("codex") else None,
             "final_at": row_final_at(row), "read": read_key(row)}
            for row in rows if needs_reader(row)]


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomically(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def load_verdicts(store_path):
    """The verdict store as a dict; a damaged one is moved aside to `<name>.bad`, never overwritten."""
    store = Path(os.path.expanduser(str(store_path)))
    if not store.exists():
        return {}
    data = read_json_file(store)
    if isinstance(data, dict):
        return {path: v for path, v in data.items() if isinstance(v, dict)}
    os.replace(store, store.with_name(store.name + ".bad"))
    return {}


def classed_queue(session):
    """{path: verdict} from a session directory whose result answers its own queue, else {}."""
    queue, result = read_json_file(session / "review-queue.json"), read_json_file(session / "review-result.json")
    if not (isinstance(queue, dict) and isinstance(result, dict)
            and result.get("queue_generated_at") == queue.get("generated_at")):
        return {}
    read = {e["path"]: e.get("read") for e in queue.get("rows") or [] if isinstance(e, dict) and "path" in e}
    return {v["path"]: {**{k: x for k, x in v.items() if k not in ("path", "read")}, "read": read[v["path"]]}
            for v in result.get("rows") or []
            if isinstance(v, dict) and v.get("path") in read and v.get("class") in VERDICT_CLASSES
            and read[v["path"]] is not None}


def absorb_verdicts(store_path, write=True):
    """Fold every session's classed queue (the store's sibling directories) into the store and return it."""
    store = Path(os.path.expanduser(str(store_path)))
    verdicts = load_verdicts(store)
    for queue in sorted(store.parent.glob("*/review-queue.json"), key=lambda p: p.stat().st_mtime):
        verdicts = {**verdicts, **classed_queue(queue.parent)}
    if write:
        write_json_atomically(store, verdicts)
    return verdicts


def with_verdicts(rows, verdicts):
    """Rows a careful reader classed carry that verdict while what it read still holds."""
    def marked(row):
        verdict = current_verdict(row, verdicts) if row["class"] in READ_CLASSES else None
        return {**row, "verdict": verdict} if verdict else row
    return [marked(row) for row in rows]


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


ORCA_TIMEOUT = 60


def orca_runs(orca):
    """Every orchestration Run; run-list returns at most 100 a page."""
    runs, cursor, seen = [], None, set()
    while True:
        argv = [orca, "orchestration", "run-list", "--limit", "100", *(["--cursor", cursor] if cursor else []), "--json"]
        out = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=ORCA_TIMEOUT).stdout
        page = json.loads(out)["result"]
        runs.extend(page["runs"])
        seen.add(cursor)
        cursor = page.get("nextCursor")
        if not cursor or cursor in seen:
            return runs


def read_mail(args):
    """Unread worker mail by lead terminal; none when Orca state comes from files without a mailbox file."""
    if args.ps_json and not args.inbox_json:
        return {}
    try:
        messages = orca_result(args.orca, ["orchestration", "inbox", "--limit", "2000"], "messages",
                               args.inbox_json)
        runs = orca_result(args.orca, [], "runs", args.runs_json) if args.runs_json else orca_runs(args.orca)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"could not read the orchestration mailbox ({error}): unread worker mail is not checked",
              file=sys.stderr)
        return {}
    return unread_worker_mail(messages, runs)


def expand_roots(patterns):
    roots = []
    for pattern in patterns:
        roots.extend(p for p in glob.glob(os.path.expanduser(pattern)) if os.path.isdir(p))
    return roots


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    parser.add_argument("--ps-json", help="read `orca worktree ps --json` output from this file")
    parser.add_argument("--terminals-json", help="read `orca terminal list --json` output from this file")
    parser.add_argument("--inbox-json", help="read `orca orchestration inbox --json` output from this file; "
                                             "with --ps-json and without it, worker mail is not checked")
    parser.add_argument("--runs-json", help="read `orca orchestration run-list --json` output from this file")
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
    parser.add_argument("--jev-key-file", default=JEV_KEY_FILE,
                        help="Jev (TypeSafe) API key file, read when TYPESAFE_API_KEY is unset "
                             "(default: %(default)s)")
    parser.add_argument("--review-out",
                        help="also write the rows a careful reader must class (every row Jev judged from a final "
                             "message without a verdict for it yet, and a stalled row whose own PR merged) to this "
                             "JSON file, for orca-row-classify; verdicts already written for any session's queue "
                             "beside the store are kept in --verdicts first")
    parser.add_argument("--verdicts", default=VERDICTS_FILE,
                        help="where orca-row-classify verdicts are kept per worktree (default: %(default)s)")
    return parser.parse_args(argv)


def write_review_queue(path, rows, verdicts=None):
    """Write {"generated_at", "rows"} so a later check can tell whether this scan's queue was classed."""
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "rows": review_queue(rows, verdicts)}
    write_json_atomically(target, payload)


def main(argv=None):
    args = parse_args(argv)
    worktrees = orca_result(args.orca, ["worktree", "ps"], "worktrees", args.ps_json)
    terminals = orca_result(args.orca, ["terminal", "list"], "terminals", args.terminals_json)
    since = time.time() - args.since_hours * 3600 if args.since_hours else None
    codex = read_codex(expand_roots(args.codex_sessions or DEFAULT_CODEX_SESSIONS), since)
    key = jev_key(os.environ, args.jev_key_file)
    if key is None:
        print(f"TYPESAFE_API_KEY is unset and {args.jev_key_file} is missing or unreadable: "
              "idle final messages come back unsure, so read them", file=sys.stderr)
    judge = jev_judge(key) if key else (lambda message: None)
    rows = add_next_step_facts(build_inventory(worktrees, terminals, os.path.expanduser(args.claude_projects),
                                               codex, args.decision_marker,
                                               shell_idle_minutes=args.shell_idle_minutes, judge=judge,
                                               mail=read_mail(args)))
    verdicts = absorb_verdicts(args.verdicts, write=bool(args.review_out))
    rows = with_verdicts(rows, verdicts)
    if args.review_out:
        write_review_queue(args.review_out, rows, verdicts)
    json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
