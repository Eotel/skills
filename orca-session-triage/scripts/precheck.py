#!/usr/bin/env python3
"""Allow a fresh hourly triage session unless an earlier turn is still working.

Exit 0 to run, 1 when a turn is working, or 2 when Orca cannot tell. A turn
waiting for an answer can stay open while the next hourly session starts.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def orca_page(orca, ps_json):
    if ps_json:
        data = json.loads(Path(ps_json).read_text())
    else:
        out = subprocess.run([orca, "worktree", "ps", "--json"], capture_output=True,
                             text=True, check=True, timeout=30).stdout
        data = json.loads(out)
    return data.get("result", data)


def workspace_row(page, workspace):
    here = os.path.realpath(workspace)
    return next((w for w in page["worktrees"] if os.path.realpath(w["path"]) == here), None)


def working_agents(row):
    """A background job also keeps its agent working in Orca."""
    return [a for a in row.get("agents") or [] if a.get("state") == "working"]


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
        row = workspace_row(page, args.workspace)
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError, AttributeError) as error:
        said = isinstance(error, subprocess.CalledProcessError) and (error.stderr or "").strip()
        print(f"skip: could not read Orca worktrees: {said or error}")
        return 2
    if row is None and page.get("truncated"):
        print(f"skip: {args.workspace} is not among the {len(page['worktrees'])} worktrees Orca listed "
              "before cutting the list short")
        return 2
    if row is None:
        print(f"skip: {args.workspace} is not an Orca worktree")
        return 2
    if working_agents(row):
        print(f"skip: an agent in {args.workspace} is working")
        return 1
    print(f"run: start a fresh session in {args.workspace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
