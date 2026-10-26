#!/usr/bin/env python3
"""Show a macOS notification for each new ask in the triage inbox.

This reader has its own byte cursor. The launchd service initializes that cursor
at the current end, so enabling alerts never replays old questions.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from inbox_read import complete_end


APPLE_SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)


def notify(item):
    lane = str(item.get("worktree") or item.get("handle") or "担当未記載").splitlines()[0]
    first_line = str(item.get("text") or "確認依頼が届きました").splitlines()[0]
    title = f"担当からの確認依頼: {lane[:80]}"
    body = first_line[:180]
    command = ["/usr/bin/osascript"]
    for line in APPLE_SCRIPT:
        command.extend(("-e", line))
    subprocess.run([*command, title, body], check=True, timeout=15,
                   capture_output=True, text=True)
    print(f"ask alert: {lane}", flush=True)


def process(inbox, cursor, send=notify):
    start = int(cursor.read_text().strip())
    with inbox.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if start > size:
            start = 0
        stream.seek(start)
        data = stream.read()
    end = complete_end(data)
    position = start
    for raw in data[:end].splitlines(keepends=True):
        try:
            item = json.loads(raw)
        except ValueError:
            item = None
        if isinstance(item, dict) and item.get("kind") == "ask":
            send(item)
        position += len(raw)
        temporary = cursor.with_name(cursor.name + ".tmp")
        temporary.write_text(str(position))
        os.replace(temporary, cursor)
    return position


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=Path.home() / ".cache/orca-briefs/triage-inbox.jsonl")
    parser.add_argument("--cursor", type=Path, default=Path.home() / ".cache/orca-session-triage/inbox-alert.offset")
    parser.add_argument("--once", action="store_true", help="read available records once, then exit")
    args = parser.parse_args(argv)
    try:
        while True:
            process(args.inbox, args.cursor)
            if args.once:
                return 0
            time.sleep(2)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"inbox-alert: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
