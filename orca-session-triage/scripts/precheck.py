#!/usr/bin/env python3
"""Precheck for the scheduled triage.

Exit 0 runs it, 1 skips it while the triage workspace is busy, and 2 skips it when Orca cannot tell.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BUSY_STATES = ("working", "blocked", "waiting")


def orca_page(orca, ps_json):
    if ps_json:
        data = json.loads(Path(ps_json).read_text())
    else:
        out = subprocess.run([orca, "worktree", "ps", "--json"], capture_output=True, text=True, check=True,
                             timeout=30).stdout
        data = json.loads(out)
    return data.get("result", data)


def busy_states(page, workspace):
    """States of the workspace's agents that should hold the run, or None when Orca does not list it."""
    here = os.path.realpath(workspace)
    row = next((w for w in page["worktrees"] if os.path.realpath(w["path"]) == here), None)
    if row is None:
        return None
    agents = [a for a in row.get("agents") or [] if a.get("state") in BUSY_STATES]
    if agents and all(background_only(a) for a in agents):
        jobs = background_jobs(workspace)
        if len(agents) == len(jobs) == 1 and watch_inbox(jobs[0]):
            return []
    return [busy_label(a) for a in agents]


def background_only(agent):
    main = agent.get("mainAgent")
    return agent.get("state") == "working" and isinstance(main, dict) and main.get("state") == "done"


def background_jobs(workspace):
    """Live Claude tool shells in this workspace, with their live child commands.

    Orca reports that a background job exists, but not which command it runs.
    Inspect the local Claude processes only for the after-turn exception.
    An unavailable process census returns no jobs and keeps the run skipped.
    """
    try:
        lines = subprocess.run(["ps", "-ww", "-axo", "pid=,ppid=,command="], capture_output=True,
                               text=True, check=True, timeout=10).stdout.splitlines()
        processes = {}
        for line in lines:
            parts = line.strip().split(None, 2)
            if len(parts) == 3:
                pid, ppid, command = parts
                processes[int(pid)] = (int(ppid), command)
        roots = []
        here = os.path.realpath(workspace)
        for pid, (_, command) in processes.items():
            if not re.match(r"(?:\S*/)?claude(?:\s|$)", command):
                continue
            try:
                cwd = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                     capture_output=True, text=True, check=True, timeout=5).stdout
            except (OSError, subprocess.SubprocessError):
                continue  # another Claude process can exit during the snapshot
            if any(os.path.realpath(line[1:]) == here
                   for line in cwd.splitlines() if line.startswith("n")):
                roots.append(pid)
        jobs = []
        for pid, (parent, command) in processes.items():
            if parent not in roots or "CLAUDE_CODE_SESSION_ID=" not in command or "eval " not in command:
                continue
            children = [child for child_parent, child in processes.values() if child_parent == pid]
            jobs.append((command, children))
        return jobs
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def watch_inbox(job):
    command, children = job
    marker = r"(?:^|/)watch-inbox(?:\s|$)"
    return bool(re.search(marker, command) and any(re.search(marker, child) for child in children))


def busy_label(agent):
    """Orca keeps an agent working while its background shell, monitor, or helper runs after its turn."""
    return "working (a background job after its turn)" if background_only(agent) else agent["state"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    parser.add_argument("--ps-json", help="read `orca worktree ps --json` output from this file")
    parser.add_argument("--workspace", required=True,
                        help="path of the triage workspace; Orca runs a precheck in the repo's main checkout")
    args = parser.parse_args(argv)

    try:
        page = orca_page(args.orca, args.ps_json)
        busy = busy_states(page, args.workspace)
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError, AttributeError) as error:
        said = isinstance(error, subprocess.CalledProcessError) and (error.stderr or "").strip()
        print(f"skip: could not read Orca worktrees: {said or error}")
        return 2
    if busy is None and page.get("truncated"):
        print(f"skip: {args.workspace} is not among the {len(page['worktrees'])} worktrees Orca listed "
              "before cutting the list short")
        return 2
    if busy is None:
        print(f"skip: {args.workspace} is not an Orca worktree")
        return 2
    if busy:
        print(f"skip: an agent in {args.workspace} is {', '.join(busy)}")
        return 1
    print(f"run: no agent in {args.workspace} works or waits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
