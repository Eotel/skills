#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Run the hourly triage reads in parallel and keep each result outside terminal output.

Each invocation creates a unique directory under --output-root. results.json lists
every exit code and output path. Failed commands keep their stdout and stderr;
only successful, parseable JSON is published as a .json result. Cleanup can close
previous completed scheduled tabs under its existing safety checks.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import hourly_runtime
import hourly_state
import json_output


@dataclass(frozen=True)
class ReadTask:
    name: str
    command: list[str]
    json_result: bool = False
    result_file: Path | None = None


def read_task(task: ReadTask, directory: Path, timeout: float) -> dict:
    stdout = directory / (task.name + ".stdout")
    stderr = directory / (task.name + ".stderr")
    result = {"name": task.name, "command": task.command, "exit_code": 0,
              "stdout": str(stdout), "stderr": str(stderr), "result": None, "error": None}
    with stdout.open("w", encoding="utf-8") as output, stderr.open("w", encoding="utf-8") as errors:
        try:
            completed = subprocess.run(task.command, stdout=output, stderr=errors, text=True, timeout=timeout)
            result["exit_code"] = completed.returncode
        except subprocess.TimeoutExpired as error:
            result.update(exit_code=124, error=str(error))
            errors.write(str(error) + "\n")
        except OSError as error:
            result.update(exit_code=127, error=str(error))
            errors.write(str(error) + "\n")
    if result["exit_code"] == 0 and task.json_result:
        try:
            source = task.result_file or stdout
            value = json.loads(source.read_text(encoding="utf-8"))
            destination = directory / (task.name + ".json")
            json_output.emit(value, destination)
            result["result"] = str(destination)
        except (OSError, ValueError) as error:
            result.update(exit_code=1, error=f"JSON result: {error}")
            with stderr.open("a", encoding="utf-8") as errors:
                errors.write(str(result["error"]) + "\n")
    return result


def tasks_for(args: argparse.Namespace, directory: Path) -> list[ReadTask]:
    scripts = Path(__file__).resolve().parent
    tools = args.bin_dir.expanduser()
    state = ["--state-file", str(args.state_file.expanduser())]
    orca = ["--orca", args.orca]
    lead_file = directory / "lead-state.partial.json"
    asks_file = directory / "open-asks.partial.json"
    asana_file = directory / "asana99-status.partial.json"
    return [
        ReadTask("cleanup-hourly-tabs", [str(scripts / "cleanup_hourly_tabs.py"),
                 "--workspace", args.workspace, *state, *orca]),
        ReadTask("lead-state", [str(scripts / "handoff.py"), "lead-state", *state, *orca,
                 "--out", str(lead_file)], True, lead_file),
        ReadTask("open-asks", [str(scripts / "inbox_read.py"), "open-asks", *orca,
                 "--out", str(asks_file)], True, asks_file),
        ReadTask("unanswered", [str(scripts / "lead_decisions.py"), "unanswered", *orca], True),
        ReadTask("pending", [str(scripts / "hourly_state.py"), *state, "pending"], True),
        ReadTask("handoff-last", [str(scripts / "handoff.py"), "last"]),
        ReadTask("scan", [str(scripts / "scan_sessions.py"), *orca, *state,
                 "--review-out", str(directory / "review-queue.json")], True),
        ReadTask("backlog-watch", [str(tools / "backlog-watch")]),
        ReadTask("asana99-status", [str(tools / "asana99_status.py"), str(asana_file), "--diff"], True, asana_file),
        ReadTask("topic-check", [str(tools / "topic"), "check", "--all"]),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=hourly_runtime.workspace_path(
        os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")),
        help="the hourly workspace whose old scheduled tabs cleanup may close")
    parser.add_argument("--output-root", type=Path, default=Path("~/.cache/orca-session-triage/reads"),
                        help="parent of the unique run directory (default: %(default)s)")
    parser.add_argument("--state-file", type=Path, default=Path(hourly_state.DEFAULT_STATE))
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"))
    parser.add_argument("--bin-dir", type=Path, default=Path("~/.cache/orca-briefs/bin"),
                        help="installed backlog-watch, asana99_status.py and topic commands")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per command (default: %(default)s)")
    args = parser.parse_args(argv)
    if not args.workspace or args.timeout <= 0:
        parser.error("--workspace (or ORCA_WORKTREE_ID) and a positive --timeout are required")
    try:
        root = args.output_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=root))
        print(f"run: {directory}", flush=True)
        tasks = tasks_for(args, directory)
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            results = list(pool.map(lambda task: read_task(task, directory, args.timeout), tasks))
        scan = next(item for item in results if item["name"] == "scan")
        if scan["exit_code"] == 0:
            stale_file = directory / "stale-asks.partial.json"
            stale = ReadTask("stale-asks", [str(Path(__file__).with_name("hourly_state.py")),
                "--state-file", str(args.state_file.expanduser()), "stale-asks", "--from", scan["result"],
                "--orca", args.orca, "--out", str(stale_file)], True, stale_file)
            results.append(read_task(stale, directory, args.timeout))
        else:
            results.append({"name": "stale-asks", "exit_code": None, "error": "skipped: scan failed"})
        json_output.emit(results, directory / "results.json")
        for result in results:
            print(f"{result['name']}: exit {result['exit_code']}"
                  + (f" | {result['error']}" if result.get("error") else ""))
        return int(any(result["exit_code"] != 0 for result in results))
    except OSError as error:
        print(f"hourly read: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
