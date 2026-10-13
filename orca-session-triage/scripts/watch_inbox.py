#!/usr/bin/env python3
"""Print each new triage inbox line once, resuming from a saved byte offset.

Run under Monitor: `watch_inbox.py <offset-file>`. A Monitor expires after 30 minutes;
because the offset is saved after every batch, the re-armed watch prints the reports
that arrived in between and repeats none.
"""

import json
import os
from pathlib import Path
import sys
import time


def inbox_path() -> Path:
    return Path(os.environ.get("TRIAGE_INBOX") or Path.home() / ".cache/orca-session-triage/inbox.jsonl")


def read_new(inbox: Path, offset: int) -> tuple[list[str], int]:
    """Return the complete lines after `offset` and the offset that follows them."""
    if not inbox.exists():
        return [], offset
    size = inbox.stat().st_size
    if size < offset:  # the inbox was replaced
        offset = 0
    if size == offset:
        return [], offset
    with inbox.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    end = data.rfind(b"\n") + 1  # leave a half-written last line for the next pass
    lines = []
    for raw in data[:end].splitlines():
        try:
            report = json.loads(raw)
            lines.append(f"[{report.get('worktree')}] {report.get('text')}")
        except ValueError:
            lines.append(raw.decode("utf-8", "replace"))
    return lines, offset + end


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: watch_inbox.py <offset-file>", file=sys.stderr)
        return 2
    state = Path(sys.argv[1])
    offset = int(state.read_text().strip() or 0) if state.exists() else 0
    while True:
        lines, offset = read_new(inbox_path(), offset)
        for line in lines:
            print(line, flush=True)
        if lines:
            state.write_text(str(offset))
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
