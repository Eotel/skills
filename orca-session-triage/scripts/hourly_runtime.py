"""Identify the scheduled triage turns from Orca's automation run records."""

from datetime import datetime
import json
import os
import re
import subprocess
from typing import Any

# The scheduled triage automation's prompt starts the hourly run; the tools find the automation by it.
HOURLY_PROMPT = "/orca-session-triage hourly"
# What precheck.py prints when it skips a run because a scheduled turn of the workspace is still working.
TURN_WORKING = re.compile(r"skip: a scheduled turn in .+ is working")
STARTED = ("dispatched", "completed")


def orca_result(orca: str, *args: str) -> dict[str, Any]:
    out = subprocess.run([orca, *args, "--json"], capture_output=True, text=True,
                         check=True, timeout=30).stdout
    data = json.loads(out)
    return data.get("result", data)


def workspace_path(workspace_id: str | None) -> str | None:
    return os.path.realpath(workspace_id.split("::", 1)[-1]) if workspace_id else None


def hourly_automations(orca: str) -> list[dict[str, Any]]:
    """Every automation of this Orca whose prompt starts the hourly triage run, in any workspace."""
    return [automation for automation in orca_result(orca, "automations", "list")["automations"]
            if (automation.get("prompt") or "").startswith(HOURLY_PROMPT)]


def scheduled_runs(orca: str, workspace: str) -> list[dict[str, Any]]:
    """Newest first; skip unrelated automations and manually triggered runs."""
    here = os.path.realpath(workspace)
    runs = []
    for automation in hourly_automations(orca):
        if workspace_path(automation.get("workspaceId")) != here:
            continue
        for run in orca_result(orca, "automations", "runs", "--id", automation["id"])["runs"]:
            if (run.get("trigger") == "scheduled" and
                    workspace_path(run.get("workspaceId")) == here):
                runs.append(run)
    runs.sort(key=lambda run: run.get("scheduledFor") or run.get("startedAt") or 0, reverse=True)
    return runs


def pane_key(term: dict[str, Any]) -> str:
    """The terminal's paneKey (`tabId:leafId`), the form of a run's terminalPaneKey and an agent's paneKey.

    Orca lists a tab it detached from the window under its pty id and keeps the
    pane only as recordedPaneKey.
    """
    return term.get("recordedPaneKey") or f"{term.get('tabId')}:{term.get('leafId')}"


def live_panes(terminals: list[dict[str, Any]]) -> frozenset[str]:
    """The pane of each connected terminal.

    A tab Orca detached from the window is orphaned yet stays connected and runs.
    """
    return frozenset(pane_key(term) for term in terminals if term.get("connected"))


def latest_run(runs: list[dict[str, Any]], live: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """The newest run that started a tab: the one whose tab owns the default inbox cursor."""
    # Orca clears terminalPaneKey after a tab closes. Never give its cursor to
    # an older run just because the newest run's tab is gone. An Orca restart
    # marks the run it was dispatching dispatch_failed while its tab goes on.
    return next((run for run in runs
                 if run.get("status") in ("dispatched", "completed")
                 or (run.get("status") == "dispatch_failed" and run.get("terminalPaneKey") in live)), None)


def latest_pane(runs: list[dict[str, Any]], live: frozenset[str] = frozenset()) -> str | None:
    run = latest_run(runs, live)
    return run.get("terminalPaneKey") if run else None


def cursor_owner(orca: str = "orca") -> tuple[bool, dict[str, Any] | None]:
    """Whether this process belongs to the latest started scheduled triage tab, and that tab's run record.

    The record is None outside an Orca tab or when this workspace has no started scheduled run.
    """
    pane = os.environ.get("ORCA_PANE_KEY")
    workspace = os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")
    if not pane or not workspace:
        return False, None
    resolved_workspace = workspace_path(workspace)
    if not resolved_workspace:
        return False, None
    runs = scheduled_runs(orca, resolved_workspace)
    # Read the terminals only when the newest run with a tab may be one marked dispatch_failed.
    failed_first = next((run.get("status") == "dispatch_failed" for run in runs
                         if run.get("status") in ("dispatched", "completed", "dispatch_failed")), False)
    live = live_panes(orca_result(orca, "terminal", "list")["terminals"]) if failed_first else frozenset()
    run = latest_run(runs, live)
    return run is not None and pane == run.get("terminalPaneKey"), run


def later_runs(orca: str = "orca") -> list[dict[str, Any]]:
    """The scheduled runs of this tab's workspace due after this tab's own run, oldest first, that started a tab of
    their own (one marked dispatch_failed while its tab is connected included), or that the precheck skipped while
    a scheduled turn worked: this tab's, which is still in its turn. None outside a scheduled run's tab."""
    pane = os.environ.get("ORCA_PANE_KEY")
    workspace = workspace_path(os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH"))
    if not pane or not workspace:
        return []
    runs = scheduled_runs(orca, workspace)
    own = next((run for run in runs if run.get("terminalPaneKey") == pane), None)
    if own is None:
        return []
    after = [run for run in reversed(runs) if (run.get("scheduledFor") or 0) > (own.get("scheduledFor") or 0)]
    live = (live_panes(orca_result(orca, "terminal", "list")["terminals"])
            if any(run.get("status") == "dispatch_failed" for run in after) else frozenset())
    return [run for run in after if run.get("status") in STARTED
            or run.get("status") == "dispatch_failed" and run.get("terminalPaneKey") in live
            or run.get("status") == "skipped_precheck"
            and TURN_WORKING.match(((run.get("precheckResult") or {}).get("stdout") or "").strip())]


def local_minute(epoch_ms: object) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000).strftime("%m/%d %H:%M") if isinstance(epoch_ms, (int, float)) else "-"


def later_run_note(run: dict[str, Any]) -> str:
    """A run of later_runs as one line: when it was due, what happened and when, and its tab's pane."""
    due, started = local_minute(run.get("scheduledFor")), local_minute(run.get("startedAt"))
    if run.get("status") == "skipped_precheck":
        return f"注意: {due} の定時の回は、この回が動いていたため skipped_precheck になった（{started}）"
    return (f"注意: この回の後に {due} の定時の回が始まった（{run.get('status')}、{started} 開始、"
            f"pane {run.get('terminalPaneKey') or '-'}）。既読位置と引き継ぎはその回のもの")


def current_pane(orca: str = "orca") -> bool:
    """Whether this process belongs to the latest started scheduled triage tab."""
    return cursor_owner(orca)[0]

