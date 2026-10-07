#!/usr/bin/env python3
"""Stop hook for orca-session-triage: keep the turn open while the review queue is not classed.

`scan_sessions.py --review-out` writes the rows a reader must class (every
unsure row, and a stalled row whose own PR merged) to
`<state dir>/<session id>/review-queue.json`; `orca-row-classify` writes its
verdicts to `review-result.json` beside it. While a queued row has no verdict
for that same queue, the hook blocks the stop once and names the rows. A
repeated stop (`stop_hook_active`) goes through, so a broken classifier cannot
trap the session.
"""

import json
import os
from pathlib import Path
import sys

STATE_DIR = "~/.cache/orca-session-triage"


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


VERDICT_CLASSES = ("waiting", "blocked", "in_progress", "done_unmerged", "finished")


def decide(hook_input, state_dir):
    """A Stop hook decision ({"decision": "block", "reason": ...}), or None to let the turn end.

    Damaged or unexpected files let the turn end: the hook must never trap the session.
    """
    if hook_input.get("stop_hook_active") or not hook_input.get("session_id"):
        return None
    session = Path(state_dir) / hook_input["session_id"]
    queue = read_json(session / "review-queue.json")
    if not isinstance(queue, dict):
        return None
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
    return {"decision": "block", "reason": (
        f"orca-session-triage: {len(missing)} row(s) in this scan's review queue have no verdict. "
        f"Invoke the orca-row-classify skill with {session} and act on its verdicts before ending "
        "the turn: " + ", ".join(missing))}


def main():
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        return 0
    decision = decide(hook_input, os.path.expanduser(STATE_DIR))
    if decision:
        json.dump(decision, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
