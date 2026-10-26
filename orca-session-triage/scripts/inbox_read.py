#!/usr/bin/env python3
"""Read new triage inbox lines by append position, then acknowledge handled lines.

`read` does not advance the cursor. After handling its output, pass the printed
byte position to `ack`. `initialize` starts a new cursor at the current end
only after the lead has handled all existing inbox entries.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import hourly_runtime


def paths():
    inbox = Path(os.environ.get("TRIAGE_INBOX") or Path.home() / ".cache/orca-briefs/triage-inbox.jsonl")
    cursor = Path(os.environ.get("TRIAGE_READ_OFFSET") or Path.home() / ".cache/orca-session-triage/inbox-read.offset")
    return inbox, cursor


def complete_end(data):
    return data.rfind(b"\n") + 1


def pending_bytes(inbox, cursor):
    start = int(cursor.read_text().strip()) if cursor.exists() else 0
    with inbox.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if start > size:
            raise ValueError("inbox is shorter than the saved read position")
        stream.seek(start)
        data = stream.read()
    return start, data, complete_end(data)


def read(inbox, cursor, brief=False):
    start, data, complete = pending_bytes(inbox, cursor)
    end = start + complete
    for raw in data[:end - start].splitlines():
        try:
            item = json.loads(raw)
            label = " ".join(str(item.get(key) or "-") for key in ("ts", "kind", "host"))
            worktree = str(item.get("worktree") or "-")
            location = f"{item['repo']}/{worktree}" if item.get("repo") else worktree
            body = item.get("text") or ""
            if brief and item.get("kind") == "info":
                compact = " ".join(body.split())
                body = compact[:160] + ("…" if len(compact) > 160 else "")
            print(f"{label} {location}\n{body}\n")
        except (ValueError, TypeError, AttributeError):
            print(raw.decode("utf-8", "replace"))
    print(f"NEXT_OFFSET={end}")


def unread_asks(inbox, cursor):
    """Return lane names for complete ask records after the acknowledged position."""
    _, data, complete = pending_bytes(inbox, cursor)
    asks = []
    for raw in data[:complete].splitlines():
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("kind") == "ask":
            asks.append(str(item.get("worktree") or item.get("handle") or "unknown"))
    return asks


def save(cursor, position, exclusive=False):
    cursor.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with cursor.open("x") as stream:
            stream.write(str(position))
    else:
        temporary = cursor.with_name(cursor.name + ".tmp")
        temporary.write_text(str(position))
        os.replace(temporary, cursor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    read_command = actions.add_parser("read", help="show complete new lines without changing the cursor")
    read_command.add_argument("--brief", action="store_true", help="show only the first 160 characters of info")
    ack = actions.add_parser("ack", help="acknowledge the byte position printed by read")
    ack.add_argument("position", type=int)
    actions.add_parser("initialize", help="set a new cursor at the current complete end")
    args = parser.parse_args(argv)
    inbox, cursor = paths()
    try:
        if (args.action != "initialize" and not os.environ.get("TRIAGE_READ_OFFSET") and
                not hourly_runtime.current_pane(os.environ.get("ORCA_CLI_COMMAND", "orca"))):
            raise ValueError("the default inbox cursor belongs to the latest scheduled triage tab")
        if args.action == "read":
            read(inbox, cursor, brief=args.brief)
            return 0
        data = inbox.read_bytes()
        end = complete_end(data)
        if args.action == "initialize":
            save(cursor, end, exclusive=True)
            print(f"initialized at {end}")
            return 0
        current = int(cursor.read_text().strip()) if cursor.exists() else 0
        if not current <= args.position <= end or (args.position and data[args.position - 1] != 10):
            raise ValueError("position must be a complete line between the saved cursor and inbox end")
        save(cursor, args.position)
        print(f"acknowledged through {args.position}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        print(f"inbox-read: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
