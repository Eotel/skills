#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Emit DONE owner/repo@sha ok|FAILED [...] once per commit; exit when all finish.

A FAILED line ends with 未起動 ["job", ...]: <reason> for the failed check runs whose first annotation says GitHub
never started them (an Actions budget or the spending limit); only a failed run with annotations is read, one REST
call each.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time


def classify_checks(runs: list[dict]) -> tuple[bool, list[str]]:
    """Return (done, failed_names). Empty or incomplete lists are not done."""
    if not runs or any(run.get("status", "").lower() != "completed" for run in runs):
        return False, []
    failed = [run.get("name", "?") for run in runs
              if (run.get("conclusion") or "").lower() not in ("success", "skipped", "neutral")]
    return True, failed


# GitHub's annotation on a job it never ran, e.g. "... because an Actions budget is preventing further use."
NOT_STARTED = "The job was not started because"


def not_started_note(reasons: dict[str, str]) -> str:
    """' 未起動 ["job", ...]: <reason>' per reason the named jobs share; empty without one."""
    jobs: dict[str, list[str]] = {}
    for name, reason in reasons.items():
        jobs.setdefault(reason, []).append(name)
    return "".join(f" 未起動 {json.dumps(names, ensure_ascii=False)}: {reason}" for reason, names in jobs.items())


def unstarted_runs(repo: str, runs: list[dict]) -> dict[str, str]:
    """{name: reason} of the failed check runs whose first annotation says GitHub never started them.

    A read that fails leaves that run out.
    """
    reasons = {}
    for run in runs:
        if (run.get("conclusion") or "").lower() in ("success", "skipped", "neutral") or not (
                (run.get("output") or {}).get("annotations_count")):
            continue
        try:
            result = subprocess.run(["gh", "api", f"repos/{repo}/check-runs/{run.get('id')}/annotations?per_page=1"],
                                    capture_output=True, text=True, timeout=60)
            annotations = json.loads(result.stdout) if result.returncode == 0 else []
        except (ValueError, subprocess.TimeoutExpired):
            continue
        message = str((annotations[0] or {}).get("message") or "") if isinstance(annotations, list) and annotations else ""
        if message.startswith(NOT_STARTED):
            reasons[run.get("name", "?")] = message
    return reasons


def classify_commit(runs: list[dict], combined: dict, suites: list[dict]) -> tuple[bool, list[str]]:
    """Non-completed suites block only with check runs or a GitHub Actions slug.
    Missing counts mean zero; missing app metadata means non-Actions.
    """
    if any(
        suite.get("status", "").lower() != "completed"
        and ((suite.get("latest_check_runs_count") or 0) > 0
             or (suite.get("app") or {}).get("slug") == "github-actions")
        for suite in suites
    ):
        return False, []
    statuses = combined["statuses"]
    if not runs and not statuses:
        return False, []
    done, failed = classify_checks(runs)
    if runs and not done:
        return False, []
    # GitHub also reports pending when no commit statuses exist.
    if statuses and (
        combined["state"].lower() not in ("success", "failure")
        or any(status.get("state", "").lower() not in ("success", "failure", "error") for status in statuses)
    ):
        return False, []
    failed.extend(status.get("context", "?") for status in statuses
                  if status.get("state", "").lower() in ("failure", "error"))
    return True, failed


def check_runs(repo: str, sha: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise ValueError("gh api failed: " + result.stderr.strip())
    pages = json.loads(result.stdout)
    return [run for page in pages for run in page["check_runs"]]


def combined_status(repo: str, sha: str) -> dict:
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/status?per_page=100"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise ValueError("gh api failed: " + result.stderr.strip())
    pages = json.loads(result.stdout)
    return {"state": pages[0]["state"], "statuses": [status for page in pages for status in page["statuses"]]}


def check_suites(repo: str, sha: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/check-suites?per_page=100"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise ValueError("gh api failed: " + result.stderr.strip())
    pages = json.loads(result.stdout)
    return [suite for page in pages for suite in page["check_suites"]]


def commit_spec(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[A-Fa-f0-9]{7,40}", value):
        raise argparse.ArgumentTypeError("expected owner/repo@sha (7 to 40 hex digits)")
    return value


def positive_interval(value: str) -> float:
    interval = float(value)
    if not 0 < interval < float("inf"):
        raise argparse.ArgumentTypeError("interval must be positive and finite")
    return interval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Non-completed suites block only with check runs or a GitHub Actions slug. "
               "Missing counts mean zero; missing app metadata means non-Actions.",
    )
    parser.add_argument("commits", nargs="+", type=commit_spec, help="commit specs, for example acme/api@abcdef123")
    parser.add_argument("--interval", type=positive_interval, default=60, help="poll interval in seconds (default: 60)")
    args = parser.parse_args(argv)
    pending = list(dict.fromkeys(args.commits))
    try:
        while pending:
            for spec in list(pending):
                repo, sha = spec.split("@", 1)
                try:
                    runs = check_runs(repo, sha)
                    combined = combined_status(repo, sha)
                    suites = check_suites(repo, sha)
                    done, failed = classify_commit(runs, combined, suites)
                except FileNotFoundError:
                    print("watch_checks: gh CLI is required", file=sys.stderr)
                    return 1
                except (ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                    print(f"{spec} watcher error: {exc}", file=sys.stderr, flush=True)
                    continue
                if done:
                    count = len(runs) + len(combined["statuses"])
                    detail = ("FAILED " + json.dumps(failed) + not_started_note(unstarted_runs(repo, runs)) if failed
                              else f"ok ({count} checks)")
                    print(f"DONE {repo}@{sha[:9]} {detail}", flush=True)
                    pending.remove(spec)
            if pending:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
