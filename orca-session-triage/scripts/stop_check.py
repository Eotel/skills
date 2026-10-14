#!/usr/bin/env python3
"""Stop hook for orca-session-triage: keep the turn open while the review queue is not classed.

`scan_sessions.py --review-out` writes the rows a reader must class (every
unsure row, and a stalled row whose own PR merged) to
`<state dir>/<session id>/review-queue.json`; `orca-row-classify` writes its
verdicts to `review-result.json` beside it. While a queued row has no verdict
for that same queue, the hook blocks the stop once and names the rows. It also
blocks once when the hourly triage of this workspace skipped a run (its precheck
saw this session busy) after this session's last scan, so the skipped sweep is
done before the turn ends. A repeated stop (`stop_hook_active`) goes through, so
a broken classifier cannot trap the session.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

STATE_DIR = "~/.cache/orca-session-triage"
TRIAGE_PROMPT = "/orca-session-triage"
# Skipped runs in a row after which the turn-end sweep does the whole triage: no scheduled run is cleaning up.
WHOLE_TRIAGE_AFTER = 3


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


VERDICT_CLASSES = ("waiting", "blocked", "in_progress", "done_unmerged", "finished")


def decide(hook_input, state_dir, orca="orca"):
    """A Stop hook decision ({"decision": "block", "reason": ...}), or None to let the turn end.

    Damaged or unexpected files, or an Orca CLI that fails, let the turn end: the hook must
    never trap the session.
    """
    if hook_input.get("stop_hook_active") or not hook_input.get("session_id"):
        return None
    session = Path(state_dir) / hook_input["session_id"]
    queue = read_json(session / "review-queue.json")
    if not isinstance(queue, dict):
        return None
    # Both reasons go in the one block: the repeated stop goes through and checks nothing.
    reasons = [r for r in (unclassed(session, queue), unswept(hook_input.get("cwd"), queue, orca)) if r]
    return {"decision": "block", "reason": " ".join(reasons)} if reasons else None


def unclassed(session, queue):
    """Why to stay while a row of this scan's review queue has no verdict, or None."""
    queued = [row["path"] for row in queue.get("rows") or [] if isinstance(row, dict) and row.get("path")]
    if not queued:
        return None
    result = read_json(session / "review-result.json")
    classed = set()
    if isinstance(result, dict) and result.get("queue_generated_at") == queue.get("generated_at"):
        classed = {row.get("path") for row in result.get("rows") or []
                   if isinstance(row, dict) and row.get("class") in VERDICT_CLASSES}
    missing = [path for path in queued if path not in classed]
    if not missing:
        return None
    return (f"orca-session-triage: {len(missing)} row(s) in this scan's review queue have no verdict. "
            f"Invoke the orca-row-classify skill with {session} and act on its verdicts before ending "
            "the turn: " + ", ".join(missing) + ".")


def orca_result(orca, *args):
    out = subprocess.run([orca, *args, "--json"], capture_output=True, text=True, check=True, timeout=15).stdout
    data = json.loads(out)
    return data.get("result", data)


def skipped_runs(orca, workspace, since_ms):
    """The newest triage run for this workspace its precheck skipped after since_ms, and the skips in a row up to now.

    Returns (start in epoch ms, count), or (None, 0) when no run was skipped since. The session may run in a
    subdirectory of the workspace the automation names.
    """
    here = Path(os.path.realpath(workspace))
    newest, in_a_row = None, 0
    for automation in orca_result(orca, "automations", "list")["automations"]:
        target = (automation.get("workspaceId") or "").split("::", 1)[-1]
        if not target or not here.is_relative_to(os.path.realpath(target)):
            continue
        if not (automation.get("prompt") or "").startswith(TRIAGE_PROMPT):
            continue
        runs = sorted(orca_result(orca, "automations", "runs", "--id", automation["id"])["runs"],
                      key=lambda run: run.get("startedAt") or 0, reverse=True)
        skips = 0
        for run in runs:
            if run.get("status") != "skipped_precheck":
                break
            skips += 1
        started = runs[0].get("startedAt") or 0 if skips else 0
        if started > max(since_ms, newest or 0):
            newest, in_a_row = started, skips
    return newest, in_a_row


def unswept(workspace, queue, orca):
    """Why to stay when the hourly triage of this workspace skipped a run after this session's last scan, or None."""
    if not workspace:
        return None
    try:
        since_ms = datetime.fromisoformat(queue["generated_at"]).timestamp() * 1000
        skipped, in_a_row = skipped_runs(orca, workspace, since_ms)
        if skipped is None:
            return None
        at = datetime.fromtimestamp(skipped / 1000).astimezone().strftime("%H:%M")
    except (OSError, OverflowError, subprocess.SubprocessError, ValueError, LookupError, TypeError,
            AttributeError):
        return None
    where = "as references/hourly.md says under \"While this session works on a request\"."
    if in_a_row >= WHOLE_TRIAGE_AFTER:
        return (f"orca-session-triage: the hourly triage skipped {in_a_row} runs in a row (skipped_precheck, the "
                f"last at {at}), so no scheduled run is cleaning up. Run the whole triage before ending the turn, "
                f"cleanup included, {where}")
    return (f"orca-session-triage: the hourly run at {at} was skipped (skipped_precheck) after this session's "
            f"last scan. Sweep before ending the turn, {where}")


def main():
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        return 0
    decision = decide(hook_input, os.path.expanduser(STATE_DIR), os.environ.get("ORCA_CLI_COMMAND", "orca"))
    if decision:
        json.dump(decision, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
