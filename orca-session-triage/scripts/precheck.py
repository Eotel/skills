#!/usr/bin/env python3
"""Precheck for the scheduled triage.

Exit 0 runs it, 1 skips it while busy or without a reusable tab, and 2 skips it when Orca cannot tell.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

BUSY_STATES = ("working", "blocked", "waiting")


def orca_result(orca, *args):
    out = subprocess.run([orca, *args], capture_output=True, text=True, check=True, timeout=30).stdout
    data = json.loads(out)
    return data.get("result", data)


def orca_page(orca, ps_json):
    if ps_json:
        data = json.loads(Path(ps_json).read_text())
        return data.get("result", data)
    else:
        return orca_result(orca, "worktree", "ps", "--json")


def workspace_row(page, workspace):
    here = os.path.realpath(workspace)
    return next((w for w in page["worktrees"] if os.path.realpath(w["path"]) == here), None)


def busy_states(page, workspace):
    """States of the workspace's agents that should hold the run, or None when Orca does not list it."""
    row = workspace_row(page, workspace)
    if row is None:
        return None
    agents = [a for a in row.get("agents") or [] if a.get("state") in BUSY_STATES]
    return [busy_label(a) for a in agents]


def background_only(agent):
    main = agent.get("mainAgent")
    return agent.get("state") == "working" and isinstance(main, dict) and main.get("state") == "done"


def busy_label(agent):
    """Orca keeps an agent working while its background shell, monitor, or helper runs after its turn."""
    return "working (a background job after its turn)" if background_only(agent) else agent["state"]


def tui_idle(orca, handle):
    result = subprocess.run([orca, "terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                             "--timeout-ms", "1000", "--json"], capture_output=True, text=True, timeout=5)
    page = json.loads(result.stdout)
    wait = (page.get("result") or {}).get("wait")
    if isinstance(wait, dict) and wait.get("satisfied") is False:
        return False
    if (page.get("error") or {}).get("code") == "timeout":
        return False
    if result.returncode or not isinstance(wait, dict) or wait.get("satisfied") is not True:
        raise ValueError(f"Orca could not check terminal readiness: {page.get('error') or result.stderr}")
    return True


def reusable_tab(orca, row):
    """Find a live prior-run tab under Orca's run order and pane ownership rules."""
    workspace_id = row["worktreeId"]
    automations = orca_result(orca, "automations", "list", "--json")["automations"]
    own = [a for a in automations if a.get("workspaceId") == workspace_id and
           "orca-session-triage/scripts/precheck.py" in (a.get("precheck") or {}).get("command", "")]
    if len(own) != 1 or own[0].get("reuseSession") is not True:
        raise ValueError(f"expected one reuse-session triage automation for {workspace_id}; found {len(own)}")
    automation = own[0]
    runs = orca_result(orca, "automations", "runs", "--id", automation["id"], "--json")["runs"]
    if (not runs and not automation.get("lastRunAt")) or (
            not any(r.get("terminalPaneKey") for r in runs) and
            any(r.get("runNumber") == 1 for r in runs)):
        return "first run"

    terminals_page = orca_result(orca, "terminal", "list", "--worktree", "id:" + workspace_id, "--json")
    terminals = {f'{t["tabId"]}:{t["leafId"]}': t for t in terminals_page["terminals"]
                 if t.get("tabId") and t.get("leafId")}
    agents = {a.get("paneKey"): a for a in row.get("agents") or [] if a.get("paneKey")}
    seen_panes = set()
    for run in sorted(runs, key=lambda r: r.get("dispatchedAt") or r.get("startedAt") or
                      r.get("createdAt") or 0, reverse=True):
        if run.get("automationId") != automation["id"]:
            continue
        pane = run.get("terminalPaneKey")
        if not pane or pane in seen_panes:
            continue
        seen_panes.add(pane)
        if run.get("status") != "completed" or run.get("workspaceId") != workspace_id:
            continue
        terminal = terminals.get(pane)
        agent = agents.get(pane)
        if (terminal and terminal.get("handle") and run.get("terminalPtyId") and
                terminal.get("ptyId") == run["terminalPtyId"] and
                terminal.get("connected") and terminal.get("writable") and not terminal.get("orphaned") and
                terminal.get("agentIdentity") == automation["agentId"] and agent and
                agent.get("state") == "done" and
                agent.get("agentType") in (None, "unknown", automation["agentId"])):
            return pane if tui_idle(orca, terminal["handle"]) else None
    if terminals_page.get("truncated"):
        raise ValueError("Orca truncated the terminal list")
    return None


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
    try:
        pane = reusable_tab(args.orca, workspace_row(page, args.workspace))
    except (OSError, subprocess.SubprocessError, ValueError, LookupError, TypeError, AttributeError) as error:
        said = isinstance(error, subprocess.CalledProcessError) and (error.stderr or "").strip()
        print(f"skip: could not check tab reuse: {said or error}")
        return 2
    if pane is None:
        print("skip: no prior automation tab can be reused")
        return 1
    if pane == "first run":
        print(f"run: no prior automation tab; start the first run in {args.workspace}")
        return 0
    print(f"run: prior automation tab {pane} can be reused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
