#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""PreToolUse hook for orca-session-triage: on a heavily loaded Mac, give a Bash call the longest timeout.

While the 1-minute load is above LOAD_LIMIT, a Bash call with no timeout, or one under TIMEOUT_MS,
comes back as `updatedInput` with only `timeout` set to TIMEOUT_MS, so a triage tool that runs past
the default 120 seconds stays in the foreground instead of moving to the background. A call Claude
starts in the background keeps its input: there the timeout sets how long it may run.
It reads the load with os.getloadavg() alone and calls no Orca CLI, so it stays fast on a loaded Mac.
It returns no permissionDecision, so the call still goes through the session's permission rules.
Reads the hook input on stdin; prints nothing when the call is left alone. No options.
"""

import json
import os
import sys
from typing import Any

LOAD_LIMIT = 300.0
# The Bash tool's default maximum timeout, in milliseconds.
TIMEOUT_MS = 600000


def longer_timeout(hook_input: dict[str, Any], load: float) -> dict[str, Any] | None:
    """The PreToolUse output that raises the call's timeout to TIMEOUT_MS, or None to leave it alone."""
    tool_input = hook_input.get("tool_input")
    if hook_input.get("tool_name") != "Bash" or load <= LOAD_LIMIT or not isinstance(tool_input, dict):
        return None
    timeout = tool_input.get("timeout")
    if tool_input.get("run_in_background") is True or (isinstance(timeout, (int, float)) and timeout >= TIMEOUT_MS):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "updatedInput": {**tool_input, "timeout": TIMEOUT_MS}}}


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print(__doc__)
        return 0
    try:
        hook_input = json.load(sys.stdin)
        load = os.getloadavg()[0]
    except (ValueError, OSError):
        return 0
    decision = longer_timeout(hook_input, load) if isinstance(hook_input, dict) else None
    if decision:
        json.dump(decision, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
