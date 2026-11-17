#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
"""Compaction hooks for the appointed lead (hourly_state.py appoint-lead): keep its commitments through a summary.

    lead_compact.py pre-compact     PreCompact: print what the summary must keep
    lead_compact.py session-start   SessionStart (startup, compact): print the board's digest of open commitments
    lead_compact.py post-compact    PostCompact: save the summary to the board under its session

Each reads the hook's JSON on stdin. Only the appointed lead's session, by its cwd or ORCA_WORKTREE_ID, gets
anything; every other session gets no output. The hook always exits 0. When the board cannot be read, the lead
gets one line naming the command to run by hand. The board command is ORCA_BOARD_COMMITMENTS
(default ~/.cache/orca-briefs/bin/orca_board_commitments.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TextIO

import ask_state
import hourly_state

DIGEST_TOKENS = "3000"
KEEP = ("要約では、ボード（orca_board_commitments.py）に記録した約束・伺い・待機・依頼・後追いは id だけを書く。"
        "ボードに記録の無い指示・約束・期限は、原文のまま残す。")


def board() -> Path:
    return Path(os.environ.get("ORCA_BOARD_COMMITMENTS",
                               "~/.cache/orca-briefs/bin/orca_board_commitments.py")).expanduser()


def is_lead(cwd: str) -> bool:
    state_file = Path(os.environ.get("TRIAGE_HOURLY_STATE", hourly_state.DEFAULT_STATE)).expanduser()
    try:
        lead = hourly_state.appointed_lead(json.loads(state_file.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError):
        return False
    if lead is None:
        return False
    places = [cwd] if cwd else []
    if os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH"):
        places.append(str(hourly_state.worktree()))
    return any(ask_state.same_workspace(str(lead), place) for place in places)


def digest(stdout: TextIO) -> None:
    command = [str(board()), "digest", "--max-tokens", DIGEST_TOKENS]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        failure = "" if result.returncode == 0 and result.stdout.strip() else (
            result.stderr.strip().splitlines() or [f"終了コード {result.returncode}"])[-1]
    except (OSError, subprocess.SubprocessError) as error:
        result, failure = None, str(error)
    if failure or result is None:
        print(f"ボードの約束の一覧を読めませんでした（{failure}）。"
              f"手で {board()} digest --max-tokens {DIGEST_TOKENS} を流し、約束を確かめてください。", file=stdout)
        return
    print(f"ボードの約束の一覧（{board().name} digest）:\n{result.stdout.rstrip()}", file=stdout)


def save_summary(payload: dict) -> None:
    summary, session = payload.get("compact_summary"), payload.get("session_id")
    if not (isinstance(summary, str) and summary.strip() and isinstance(session, str)):
        return
    try:
        result = subprocess.run([str(board()), "save-summary", "--session", session], input=summary,
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"lead_compact: 要約をボードに残せませんでした: {error}", file=sys.stderr)
        return
    if result.returncode:
        print(f"lead_compact: 要約をボードに残せませんでした: {result.stderr.strip()[-300:]}", file=sys.stderr)


def main(argv: list[str], stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    event = argv[0] if argv else ""
    try:
        payload = json.loads(stdin.read() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict) or not is_lead(str(payload.get("cwd") or "")):
        return 0
    if event == "pre-compact":
        print(KEEP, file=stdout)
    elif event == "session-start":
        digest(stdout)
    elif event == "post-compact":
        save_summary(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
