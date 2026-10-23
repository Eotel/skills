#!/usr/bin/env python3
"""Print each new line of a triage inbox once, resuming from a saved byte offset.

Run under Monitor. A Monitor expires after 30 minutes; because the offset is saved
after every batch, the watch started again with the same offset file prints the
reports that arrived in between and repeats none.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import time


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
            lines.append(f"[{report.get('lane')}] {report.get('text')}")
        except ValueError:
            lines.append(raw.decode("utf-8", "replace"))
    return lines, offset + end


def save_offset(state: Path, offset: int) -> None:
    """Replace the offset file in one step, so a killed watch never leaves it half-written."""
    temporary = state.with_name(state.name + ".tmp")
    temporary.write_text(str(offset))
    os.replace(temporary, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inbox", type=Path, help="this triage tab's inbox file")
    parser.add_argument("offset_file", type=Path, help="where the read position is kept between runs")
    args = parser.parse_args()
    state = args.offset_file
    offset = int(state.read_text().strip() or 0) if state.exists() else 0
    while True:
        lines, offset = read_new(args.inbox, offset)
        for line in lines:
            print(line, flush=True)
        if lines:
            save_offset(state, offset)
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
