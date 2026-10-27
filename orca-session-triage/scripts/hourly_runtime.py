"""Identify the scheduled triage turns from Orca's automation run records."""

import json
import os
import subprocess
from typing import Any


def orca_result(orca: str, *args: str) -> dict[str, Any]:
    out = subprocess.run([orca, *args, "--json"], capture_output=True, text=True,
                         check=True, timeout=30).stdout
    data = json.loads(out)
    return data.get("result", data)


def workspace_path(workspace_id: str | None) -> str | None:
    return os.path.realpath(workspace_id.split("::", 1)[-1]) if workspace_id else None


def scheduled_runs(orca: str, workspace: str) -> list[dict[str, Any]]:
    """Newest first; skip unrelated automations and manually triggered runs."""
    here = os.path.realpath(workspace)
    automations = orca_result(orca, "automations", "list")["automations"]
    runs = []
    for automation in automations:
        if (workspace_path(automation.get("workspaceId")) != here or
                not (automation.get("prompt") or "").startswith("/orca-session-triage hourly")):
            continue
        for run in orca_result(orca, "automations", "runs", "--id", automation["id"])["runs"]:
            if (run.get("trigger") == "scheduled" and
                    workspace_path(run.get("workspaceId")) == here):
                runs.append(run)
    runs.sort(key=lambda run: run.get("scheduledFor") or run.get("startedAt") or 0, reverse=True)
    return runs


def latest_pane(runs: list[dict[str, Any]]) -> str | None:
    # Orca clears terminalPaneKey after a tab closes. Never give its cursor to
    # an older run just because the newest run's tab is gone.
    return next((run.get("terminalPaneKey") for run in runs
                 if run.get("status") in ("dispatched", "completed")), None)


def current_pane(orca: str = "orca") -> bool:
    """Whether this process belongs to the latest started scheduled triage tab."""
    pane = os.environ.get("ORCA_PANE_KEY")
    workspace = os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")
    if not pane or not workspace:
        return False
    resolved_workspace = workspace_path(workspace)
    return bool(resolved_workspace and pane == latest_pane(scheduled_runs(orca, resolved_workspace)))
