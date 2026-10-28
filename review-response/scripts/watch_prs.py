#!/usr/bin/env python3
"""Poll a re-read list of owner/repo#N PRs and persist event history as JSON.

Events: merged or closed, CI result per head, conflicts, new reviews and comments,
and each Codex review's status, which Codex edits into one summary comment.

Cost per cycle: one `gh api rate_limit`, which does not count against the primary
rate limits (it can count against secondary limits), then one GraphQL `gh pr view`
per open PR. The three REST feeds (reviews, line comments, issue comments; one call
per 100 items each) are read only after the PR's updatedAt moves, which GitHub does
for new comments, reviews, pushes and comment edits, and once more on the next cycle
because updatedAt counts whole seconds. A PR whose merge or close is recorded in the
state file is not read again; to watch it after a reopen, stop the watcher, remove
the PR's entry from the state file, and start the watcher again (a running watcher
keeps the entry in memory and writes it back). While fewer than 1000 GraphQL points
remain, the cycle prints RATE_LIMIT_LOW and sleeps until the reset.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time


MIN_INTERVAL = 60
MIN_GRAPHQL_REMAINING = 1000
TERMINAL = ("MERGED", "CLOSED")
SUCCESS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILURE = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
# Codex edits one summary comment in place, so its review progress never arrives as a new comment.
CODEX_MARKER = re.compile(r"<!--\s*(codex-review-state|codex-security-review):v1\s*(\{.*?\})\s*-->", re.S)
# The visible table row, e.g. "| 📝 **Code Review** | ✅ **Completed** ... | `38e31d6` |".
CODEX_ROW = re.compile(r"\*\*(Code|Security) Review\*\*\s*\|[^|]*?\*\*(\w+)\*\*[^|]*\|\s*`([0-9a-f]{7,40})`")


def finished_checks(checks):
    """Classify a gh statusCheckRollup, including check runs and legacy statuses."""
    failed = []
    for check in checks:
        if "status" in check:
            if (check.get("status") or "").upper() != "COMPLETED":
                return False, []
            result = (check.get("conclusion") or "").upper()
        else:
            result = (check.get("state") or "").upper()
        if result not in SUCCESS | FAILURE:
            return False, []
        if result in FAILURE:
            failed.append(check.get("name") or check.get("context") or "?")
    return bool(checks), failed


def codex_reviews(comments):
    """Map each Codex review kind to (commit sha, status, has_any_findings) from its summary comment."""
    reviews = {}
    for item in comments:
        if not (item.get("user") or {}).get("login", "").startswith("chatgpt-codex-connector"):
            continue
        for marker, raw in CODEX_MARKER.findall(item.get("body") or ""):
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if marker == "codex-review-state":
                for kind in ("code_review", "security_review"):
                    review = data.get(kind) or {}
                    if review.get("status"):
                        reviews[kind] = (review.get("commit_sha") or "", review["status"], review.get("has_any_findings"))
            elif data.get("status"):
                # This marker carries only the security review.
                reviews.setdefault("security_review", (data.get("headSha") or "", data["status"], None))
        # Without the state marker, the code review shows only in the table.
        for review, status, sha in CODEX_ROW.findall(item.get("body") or ""):
            reviews.setdefault(f"{review.lower()}_review", (sha, status.lower(), None))
    return reviews


def diff_events(spec: str, previous: dict, snapshot: dict, me: str) -> tuple[list[str], dict]:
    """Pure diff: return (event lines, new state) without mutating either input.

    snapshot contains the gh PR view fields plus reviews, line_comments and
    comments arrays, which are absent when they were not read. Absent feeds report
    nothing, because only unseen IDs and changed Codex reviews are reported.
    previous is the returned state from the last successful poll (or an empty
    dict). CI history and feed IDs survive pending checks and retries.
    """
    state = dict(previous or {})
    events = []
    if "comments" in snapshot:
        updated_at = snapshot.get("updatedAt")
        # Two reads at one updatedAt rule out a change later in that same second.
        if updated_at and updated_at == state.get("feeds_read_at"):
            state["feeds_settled_at"] = updated_at
        state["feeds_read_at"] = updated_at
    current_state = snapshot["state"]
    if current_state in TERMINAL and current_state != state.get("state"):
        events.append(f"{spec} {current_state}")
    state["state"] = current_state
    checks = snapshot.get("statusCheckRollup") or []
    done, failed = finished_checks(checks)
    if current_state == "OPEN" and done:
        head = snapshot["headRefOid"]
        result = "FAILURE" if failed else "SUCCESS"
        terminal_results = sorted(
            ("check", check.get("name") or "?", (check.get("conclusion") or "").upper())
            if "status" in check else
            ("status", check.get("context") or "?", (check.get("state") or "").upper())
            for check in checks
        )
        signature = json.dumps([head, result, terminal_results], ensure_ascii=False, separators=(",", ":"))
        if state.get("ci") != signature:
            detail = " failed=" + json.dumps(failed, ensure_ascii=False) if failed else ""
            events.append(f"{spec} CI {result} ({len(checks)} checks, {len(failed)} failed, head {head[:9]}){detail}")
        state["ci"] = signature
    mergeable = snapshot.get("mergeable") or ""
    if current_state == "OPEN" and mergeable == "CONFLICTING" and state.get("mergeable") != "CONFLICTING":
        events.append(f"{spec} CONFLICTING with base")
    state["mergeable"] = mergeable
    seen = set(state.get("seen") or [])
    for feed, kind, extra_field in (
        ("reviews", "REVIEW", "state"),
        ("line_comments", "LINE", "path"),
        ("comments", "COMMENT", None),
    ):
        for item in snapshot.get(feed) or []:
            # Draft reviews are private until submitted.
            if feed == "reviews" and item.get("state") == "PENDING":
                continue
            identity = f"{kind}:{item['id']}"
            if identity in seen:
                continue
            seen.add(identity)
            who = (item.get("user") or {}).get("login", "?")
            if who.casefold() == me.casefold():
                continue
            extra = item.get(extra_field, "") if extra_field else ""
            body = " ".join((item.get("body") or "").split())[:120]
            events.append(f"{spec} NEW {kind} by {who} {extra} :: {body}")
    state["seen"] = sorted(seen)
    codex = dict(state.get("codex") or {})
    for kind, (sha, status, findings) in sorted(codex_reviews(snapshot.get("comments") or []).items()):
        signature = [sha, status, findings]
        if codex.get(kind) != signature:
            note = {True: ", findings", False: ", no findings"}.get(findings, "")
            events.append(f"{spec} CODEX {kind} {status.upper()} (head {sha[:9]}{note})")
        codex[kind] = signature
    state["codex"] = codex
    return events, state


def gh_json(args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError("gh failed: " + result.stderr.strip())
    return json.loads(result.stdout)


def graphql_budget() -> tuple[int, int]:
    """Return the GraphQL points remaining and their reset epoch; this read is free."""
    graphql = gh_json(["api", "rate_limit"])["resources"]["graphql"]
    return int(graphql["remaining"]), int(graphql["reset"])


def fetch_snapshot(spec: str, previous: dict) -> dict:
    repo, number = spec.split("#", 1)
    view = gh_json(["pr", "view", number, "--repo", repo, "--json", "state,headRefOid,mergeable,statusCheckRollup,updatedAt"])
    if view.get("updatedAt") and view["updatedAt"] == previous.get("feeds_settled_at"):
        return view
    for feed, endpoint in (
        ("reviews", f"repos/{repo}/pulls/{number}/reviews"),
        ("line_comments", f"repos/{repo}/pulls/{number}/comments"),
        ("comments", f"repos/{repo}/issues/{number}/comments"),
    ):
        pages = gh_json(["api", "--paginate", "--slurp", endpoint + "?per_page=100"])
        view[feed] = [item for page in pages for item in page]
    # A failure in any feed leaves the prior state intact for the next poll.
    return view


def load_state(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict) or any(not isinstance(value, dict) for value in state.values()):
        raise ValueError("state file must contain a JSON object of PR states")
    return state


def read_specs(path):
    specs = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", line):
            raise ValueError(f"invalid PR spec on list line {number}; expected owner/repo#N")
        if line not in specs:
            specs.append(line)
    return specs


def poll_interval(value: str) -> float:
    interval = float(value)
    if not MIN_INTERVAL <= interval < float("inf"):
        raise argparse.ArgumentTypeError(f"interval must be at least {MIN_INTERVAL} seconds and finite")
    return interval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("list_file", type=Path, help="one owner/repo#N per line; blank lines and # comments are ignored")
    parser.add_argument("--state-file", type=Path, required=True, help="persisted event state; a missing file starts empty")
    parser.add_argument("--me", required=True, help="ignore reviews and comments by this GitHub login")
    parser.add_argument("--baseline", action="store_true", help="silently record new PRs during the first cycle")
    parser.add_argument(
        "--interval", type=poll_interval, default=MIN_INTERVAL,
        help=f"poll interval in seconds, at least {MIN_INTERVAL}; smaller values are rejected (default: {MIN_INTERVAL})",
    )
    parser.add_argument("--once", action="store_true", help="poll one cycle and exit; exits 1 instead of sleeping on RATE_LIMIT_LOW")
    args = parser.parse_args(argv)
    try:
        states = load_state(args.state_file)
        first_cycle = True
        while True:
            had_errors = False
            try:
                remaining, reset = graphql_budget()
            except FileNotFoundError:
                raise ValueError("gh CLI is required") from None
            except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
                # The budget check is advisory, so a failed check still polls.
                print(f"watch_prs: rate limit check failed: {exc}", file=sys.stderr, flush=True)
            else:
                if remaining < MIN_GRAPHQL_REMAINING:
                    reset_at = datetime.fromtimestamp(reset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    print(f"RATE_LIMIT_LOW graphql remaining={remaining} reset={reset_at}", flush=True)
                    if args.once:
                        return 1
                    time.sleep(max(reset - time.time(), args.interval))
                    continue
            # Callers can add or remove PRs while this process is running.
            for spec in read_specs(args.list_file):
                previous = states.get(spec, {})
                if previous.get("state") in TERMINAL:
                    continue
                try:
                    snapshot = fetch_snapshot(spec, previous)
                    events, updated = diff_events(spec, previous, snapshot, args.me)
                except FileNotFoundError:
                    raise ValueError("gh CLI is required") from None
                except (ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                    had_errors = True
                    print(f"{spec} watcher error: {exc}", file=sys.stderr, flush=True)
                    continue
                silent = args.baseline and first_cycle and spec not in states
                states[spec] = updated
                if not silent:
                    for event in events:
                        print(event, flush=True)
            # Atomic replacement prevents an interrupted write from losing history.
            temporary = args.state_file.with_name(args.state_file.name + ".tmp")
            temporary.write_text(json.dumps(states, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(args.state_file)
            if args.once:
                return 1 if had_errors else 0
            first_cycle = False
            time.sleep(args.interval)
    except (OSError, ValueError) as exc:
        print(f"watch_prs: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
