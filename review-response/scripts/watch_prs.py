#!/usr/bin/env python3
"""Poll a re-read list of owner/repo#N PRs and persist event history as JSON."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time


SUCCESS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
FAILURE = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}


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


def diff_events(spec, previous, snapshot, me):
    """Pure diff: return (event lines, new state) without mutating either input.

    snapshot contains the gh PR view fields plus reviews, line_comments and
    comments arrays. previous is the returned state from the last successful poll
    (or an empty dict). CI history and feed IDs survive pending checks and retries.
    """
    state = dict(previous or {})
    events = []
    current_state = snapshot["state"]
    if current_state in ("MERGED", "CLOSED") and current_state != state.get("state"):
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
    return events, state


def gh_json(args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError("gh failed: " + result.stderr.strip())
    return json.loads(result.stdout)


def fetch_snapshot(spec):
    repo, number = spec.split("#", 1)
    view = gh_json(["pr", "view", number, "--repo", repo, "--json", "state,headRefOid,mergeable,statusCheckRollup"])
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


def positive_interval(value):
    value = float(value)
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("interval must be positive and finite")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("list_file", type=Path, help="one owner/repo#N per line; blank lines and # comments are ignored")
    parser.add_argument("--state-file", type=Path, required=True, help="persisted event state; a missing file starts empty")
    parser.add_argument("--me", required=True, help="ignore reviews and comments by this GitHub login")
    parser.add_argument("--baseline", action="store_true", help="silently record new PRs during the first cycle")
    parser.add_argument("--interval", type=positive_interval, default=60, help="poll interval in seconds (default: 60)")
    parser.add_argument("--once", action="store_true", help="poll one cycle and exit")
    args = parser.parse_args(argv)
    try:
        states = load_state(args.state_file)
        first_cycle = True
        while True:
            had_errors = False
            # Callers can add or remove PRs while this process is running.
            for spec in read_specs(args.list_file):
                try:
                    snapshot = fetch_snapshot(spec)
                    events, updated = diff_events(spec, states.get(spec, {}), snapshot, args.me)
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
