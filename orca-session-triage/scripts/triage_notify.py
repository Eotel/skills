#!/usr/bin/env python3
"""Append one lane report to a triage tab's inbox.

Lanes run this instead of typing into the triage tab, where the text would land in
the user's half-typed message. Each triage tab names its own inbox in its briefs.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def lane_label() -> str:
    """Name the lane by repository and worktree, wherever inside it the lane runs."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        top = ""
    path = Path(top) if top else Path.cwd()
    return "/".join(path.parts[-2:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inbox", type=Path, help="the triage tab's inbox file, as its brief names it")
    parser.add_argument("text", nargs="*", help="the report; read from stdin when omitted")
    args = parser.parse_args()
    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        parser.error("the report text is empty")
    line = json.dumps(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "lane": lane_label(), "text": text},
        ensure_ascii=False,
    )
    args.inbox.parent.mkdir(parents=True, exist_ok=True)
    # One O_APPEND write per report: concurrent lanes never interleave or overwrite.
    fd = os.open(args.inbox, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    print("appended to the triage inbox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
