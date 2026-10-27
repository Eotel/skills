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

import hourly_runtime


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


def runs_page(orca, workspace, runs_json):
    if runs_json:
        data = json.loads(Path(runs_json).read_text())
        return data.get("result", data)["runs"]
    return hourly_runtime.scheduled_runs(orca, workspace)


def scheduled_turn_working(agent, status_by_pane):
    status = status_by_pane.get(agent.get("paneKey")) or status_by_pane.get(agent.get("parentPaneKey"))
    # An Orca restart marks the run it was dispatching dispatch_failed while its tab goes on working.
    return status in ("dispatched", "dispatch_failed") or (
        status == "completed" and (agent.get("mainAgent") or {}).get("state") == "done")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    parser.add_argument("--ps-json", help="read `orca worktree ps --json` output from this file")
    parser.add_argument("--runs-json", help="read `orca automations runs --json` output from this file")
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
    active = working_agents(row)
    if active:
        if any(not (agent.get("paneKey") or agent.get("parentPaneKey")) for agent in active):
            print("skip: could not identify a working agent's pane")
            return 2
        try:
            runs = runs_page(args.orca, args.workspace, args.runs_json)
            if not runs:
                print("skip: could not identify the scheduled runs")
                return 2
            scheduled_panes = {run.get("terminalPaneKey"): run.get("status") for run in runs
                               if run.get("status") in ("dispatched", "dispatch_failed", "completed") and
                               run.get("trigger") == "scheduled" and
                               hourly_runtime.workspace_path(run.get("workspaceId")) ==
                               os.path.realpath(args.workspace) and run.get("terminalPaneKey")}
        except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError, AttributeError) as error:
            print(f"skip: could not read scheduled turns: {error}")
            return 2
        if any(scheduled_turn_working(agent, scheduled_panes) for agent in active):
            print(f"skip: a scheduled turn in {args.workspace} is working")
            return 1
    print(f"run: start a fresh session in {args.workspace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
