#!/usr/bin/env python3
"""Append one lane report to the triage inbox.

Lanes run this instead of typing into the triage tab, where the text would land in
the user's half-typed message. The inbox is `TRIAGE_INBOX` or
`~/.cache/orca-session-triage/inbox.jsonl`.
"""

import json
import os
from pathlib import Path
import sys
import time


def inbox_path() -> Path:
    return Path(os.environ.get("TRIAGE_INBOX") or Path.home() / ".cache/orca-session-triage/inbox.jsonl")


def main() -> int:
    text = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not text:
        print("usage: triage_notify.py <report text>", file=sys.stderr)
        return 2
    line = json.dumps(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "worktree": Path.cwd().name, "text": text},
        ensure_ascii=False,
    )
    path = inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # One O_APPEND write per report: concurrent lanes never interleave or overwrite.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    print("appended to the triage inbox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
