#!/usr/bin/env python3
"""List a triage workspace's scheduled hourly runs, newest first, one line each.

Each line gives when the run was scheduled and started (local time), Orca's status,
what the precheck printed when it skipped the run, the run's tab pane, and its Claude
transcript, so neither Orca's automation records nor transcript paths that contain
spaces have to be read by hand. `--json` prints the same fields as a list.
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import cleanup_hourly_tabs
import hourly_runtime


@dataclass(frozen=True)
class Run:
    scheduled: str
    status: str
    started: str
    precheck: str
    pane: str
    transcript: str

    def line(self) -> str:
        return " | ".join((self.scheduled or "-", self.status, f"started {self.started or '-'}",
                           self.precheck or "-", f"pane {self.pane or '-'}", self.transcript or "-"))


def local_time(epoch_ms: object) -> str:
    if not isinstance(epoch_ms, (int, float)):
        return ""
    return datetime.fromtimestamp(epoch_ms / 1000).astimezone().isoformat(timespec="seconds")


def run_of(record: dict[str, Any], home: Path) -> Run:
    said = ((record.get("precheckResult") or {}).get("stdout") or "").strip()
    transcript = cleanup_hourly_tabs.transcript_for(record, home)
    return Run(scheduled=local_time(record.get("scheduledFor")), status=str(record.get("status") or ""),
               started=local_time(record.get("startedAt")),
               precheck=said.splitlines()[0] if record.get("status") == "skipped_precheck" and said else "",
               pane=record.get("terminalPaneKey") or "", transcript=str(transcript) if transcript else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=hourly_runtime.workspace_path(
        os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")),
        help="triage workspace path (default: this Orca worktree)")
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    parser.add_argument("--limit", type=int, default=12, help="newest runs to list (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="print the runs as a JSON list")
    args = parser.parse_args(argv)
    if not args.workspace:
        parser.error("--workspace or ORCA_WORKTREE_ID is required")
    try:
        records = hourly_runtime.scheduled_runs(args.orca, args.workspace)[:args.limit]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        print(f"hourly runs: could not read Orca's automation runs: {error}", file=sys.stderr)
        return 1
    runs = [run_of(record, Path.home()) for record in records]
    if args.json:
        print(json.dumps([asdict(run) for run in runs], ensure_ascii=False, indent=2))
    else:
        print("\n".join(run.line() for run in runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
