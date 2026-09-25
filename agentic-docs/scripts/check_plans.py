#!/usr/bin/env python3
"""Fail on kept completed plans, history headings, or oversized active plans."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

DEFAULT_MAX_LINES = 150
HISTORY_HEADING = re.compile(
    r" {0,3}#{2,6}[ \t]+(?P<title>(?:(?:progress|outcomes|surprises|decision log)\b"
    r"|進捗|作業ログ|決定ログ|振り返り).*?)[ \t#]*",
    re.IGNORECASE,
)
FENCE = re.compile(r" {0,3}(?P<marker>`{3,}|~{3,})")


def history_headings(text: str) -> list[str]:
    titles = []
    fence = None
    for line in text.splitlines():
        match = FENCE.match(line)
        if fence is None and match:
            fence = match["marker"]
        elif fence is not None:
            if line.strip().startswith(fence[0] * len(fence)):
                fence = None
        elif heading := HISTORY_HEADING.fullmatch(line):
            titles.append(heading["title"])
    return titles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root. Defaults to cwd.",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_LINES,
        help=f"Maximum lines per active plan. Defaults to {DEFAULT_MAX_LINES}.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plans_root = args.root.resolve() / "docs" / "exec-plans"
    problems = [
        f"{path.relative_to(plans_root)}: delete completed plans; keep results in the PR"
        for path in sorted((plans_root / "completed").rglob("*.md"))
    ]
    for path in sorted((plans_root / "active").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        problems.extend(
            f"{path.relative_to(plans_root)}: history heading '{title}'; "
            "keep only current state and next steps"
            for title in history_headings(text)
        )
        line_count = len(text.splitlines())
        if line_count > args.max_lines:
            problems.append(
                f"{path.relative_to(plans_root)}: {line_count} lines exceeds "
                f"{args.max_lines}; remove finished steps and history"
            )
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
