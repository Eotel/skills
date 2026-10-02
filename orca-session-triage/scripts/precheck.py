#!/usr/bin/env python3
"""Precheck for the scheduled triage.

Exit 0 runs it, 1 skips it while the triage workspace is busy, and 2 skips it when Orca cannot tell.
"""

import argparse
import json
import os
from pathlib import Path
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
    return [a["state"] for a in row.get("agents") or [] if a.get("state") in BUSY_STATES]


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
