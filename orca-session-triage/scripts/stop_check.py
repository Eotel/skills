#!/usr/bin/env python3
"""Stop hook for orca-session-triage: keep the turn open while the review queue is not classed.

`scan_sessions.py --review-out` writes the rows a reader must class (every
unsure row, and a stalled row whose own PR merged) to
`<state dir>/<session id>/review-queue.json`; `orca-row-classify` writes its
verdicts to `review-result.json` beside it. While a queued row has no verdict
for that same queue, the hook blocks the stop once and names the rows. It also
blocks once while an artifact this session published is still watched live:
Orca counts the tab as working while the watch runs. A missing external inbox service
is named when the triage automation is installed. A repeated stop
(`stop_hook_active`) goes through, so a broken classifier cannot trap the session.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

STATE_DIR = "~/.cache/orca-session-triage"
TRIAGE_PROMPT = "/orca-session-triage"
# Kept in the session's state directory: how far the transcript was read and which artifacts are still watched.
WATCH_STATE = "artifact-watches.json"
# `liveSubscription` of an Artifact publish result that leaves a live watch running.
LIVE_SUBSCRIPTIONS = ("arming", "connected")
# Claude Code's notice that a watch ended on its own (connection lost, artifact not found).
WATCH_LIFECYCLE = "<task-type>artifact-watch-lifecycle</task-type>"


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
    # Every reason goes in the one block: the repeated stop goes through and checks nothing.
    reasons = [watched(session, hook_input.get("transcript_path"))]
    queue = read_json(session / "review-queue.json")
    if isinstance(queue, dict):
        reasons += [unclassed(session, queue), missing_inbox_watch(hook_input.get("cwd"), orca)]
    reasons = [r for r in reasons if r]
    return {"decision": "block", "reason": " ".join(reasons)} if reasons else None


def watched(session, transcript):
    """Why to stay while an artifact this session published is still watched live, or None.

    Reads only the transcript lines written since the last stop. An Artifact publish whose result has a live
    subscription starts a watch; an ArtifactComments watch-stop result for the same URL, or Claude Code's notice
    that the watch ended on its own, ends it.
    """
    if not transcript:
        return None
    state_file = session / WATCH_STATE
    state = read_json(state_file)
    if not isinstance(state, dict) or state.get("transcript") != transcript:
        state = {}
    offset = state.get("offset") if isinstance(state.get("offset"), int) else 0
    kept = state.get("watched") if isinstance(state.get("watched"), list) else []
    urls = dict.fromkeys(url for url in kept if isinstance(url, str))
    try:
        with open(transcript, "rb") as lines:
            if os.fstat(lines.fileno()).st_size < offset:
                offset, urls = 0, {}
            lines.seek(offset)
            for line in lines:
                if not line.endswith(b"\n"):
                    break  # still being written; read it at the next stop
                offset += len(line)
                if b'"liveSubscription"' in line or b'"unwatch"' in line or b"artifact-watch-lifecycle" in line:
                    follow_watch(line, urls)
    except OSError:
        return None
    try:
        session.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"transcript": transcript, "offset": offset, "watched": list(urls)}))
    except OSError:
        pass  # the next stop reads the same lines again
    if not urls:
        return None
    return ("orca-session-triage: a live watch still runs on " + ", ".join(urls) + ". Stop each with the "
            "ArtifactComments tool (action:\"watch\", on:false, url:<that URL>) before ending the turn: a live "
            "watch keeps Orca counting this tab as working, so the hourly run is skipped.")


def follow_watch(line, urls):
    """Add the URL a publish result starts watching to urls, or drop the one a watch-stop result or notice names."""
    try:
        entry = json.loads(line)
    except ValueError:
        return
    if not isinstance(entry, dict):
        return
    notice = entry.get("attachment")
    if isinstance(notice, dict) and isinstance(notice.get("prompt"), str) and WATCH_LIFECYCLE in notice["prompt"]:
        for url in re.findall(r"Watch on (\S+) ended", notice["prompt"]):
            urls.pop(url, None)
        return
    result = entry.get("toolUseResult")
    if not isinstance(result, dict):
        return
    stopped = result.get("unwatch")
    if isinstance(stopped, dict) and isinstance(stopped.get("url"), str):
        urls.pop(stopped["url"], None)
    elif result.get("liveSubscription") in LIVE_SUBSCRIPTIONS and isinstance(result.get("url"), str):
        urls[result["url"]] = None


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


def triage_automations(orca, workspace):
    here = Path(os.path.realpath(workspace))
    return [a for a in orca_result(orca, "automations", "list")["automations"]
            if (a.get("workspaceId") or "").split("::", 1)[-1]
            and here.is_relative_to(os.path.realpath(a["workspaceId"].split("::", 1)[-1]))
            and (a.get("prompt") or "").startswith(TRIAGE_PROMPT)]


def inbox_service_running():
    script = Path(__file__).with_name("inbox_watch_service.py")
    result = subprocess.run([sys.executable, str(script), "status"], capture_output=True,
                            text=True, timeout=15)
    return result.returncode == 0


def missing_inbox_watch(workspace, orca):
    if not workspace:
        return None
    try:
        if not triage_automations(orca, workspace) or inbox_service_running():
            return None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
    return ("orca-session-triage: the external triage inbox service is not running. "
            "See references/hourly.md for the watch-inbox service status and cutover steps; "
            "do not start another reader of ~/.cache/orca-briefs/offsets/close-done-triage.offset.")


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
