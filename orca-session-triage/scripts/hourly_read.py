#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
"""Run the hourly triage reads in parallel and keep each result outside terminal output.

Each invocation creates a unique directory under --output-root and ends by printing
the path of its results.json: a JSON array with one record per command, each with
the keys name, command, exit_code, stdout and stderr (the paths of the command's
output files), result (the path of its .json result, or null), error, retry,
compared_to, and changes.
The lines `<name>: exit N` before it list the same exit codes. Failed commands keep
their stdout and stderr; only successful, parseable JSON is published as a .json
result. Cleanup can close previous completed scheduled tabs under its existing
safety checks.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import hourly_runtime
import hourly_state
import json_output
import lead_decisions
from bash_timeout import LOAD_LIMIT

RECORD_KEYS = ("name", "command", "exit_code", "stdout", "stderr", "result", "error", "retry", "compared_to", "changes",
               "read_at")
LOAD_TIMEOUT_FACTOR = 2.5
COMPARE = ("tracker-gaps", "asana99-status", "backlog-watch")
BACKLOG_ITEM = re.compile(r"^(?:返信|返信不要|担当確認|判定済み) [A-Z][A-Z0-9_]+-\d+\b")
BACKLOG_ELAPSED = re.compile(r" \| (?:\d+時間)?\d+分 \| ")
BACKLOG_PR_ELAPSED = re.compile(
    r"( \| PR [\w.-]+/[\w.-]+#\d+ (?:レビュー待ち|下書き|承認済み|修正依頼|open)) \d+(?:日|時間)(?= →| \||$)")
BACKLOG_CURSOR = Path("~/.cache/orca-briefs/backlog-watch/state.json")
ASANA_PARTIAL = re.compile(r"^Eotel へのレビュー依頼: 未取得", re.MULTILINE)


@dataclass(frozen=True)
class ReadTask:
    name: str
    command: list[str]
    json_result: bool = False
    result_file: Path | None = None


def read_task(task: ReadTask, directory: Path, timeout: float, *, retry: bool = False) -> dict:
    stem = task.name + (".retry" if retry else "")
    stdout, stderr = directory / (stem + ".stdout"), directory / (stem + ".stderr")
    result = {**dict.fromkeys(RECORD_KEYS), "name": task.name, "command": task.command, "exit_code": 0,
              "stdout": str(stdout), "stderr": str(stderr)}
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
    result["read_at"] = time.time()
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


def backlog_cursor() -> bytes | None:
    try:
        return BACKLOG_CURSOR.expanduser().read_bytes()
    except FileNotFoundError:
        return b""
    except OSError:
        return None


def read_with_retry(task: ReadTask, directory: Path, timeout: float, loaded: bool) -> dict:
    backlog = loaded and task.name == "backlog-watch"
    before = backlog_cursor() if backlog else None
    first = read_task(task, directory, timeout)
    if not loaded or first["exit_code"] != 124:
        return first
    if backlog and (before is None or backlog_cursor() != before):
        first["error"] = "通知カーソルが更新済みか確認できないため、終了コード 124 を維持します"
        return first
    if task.result_file and task.result_file.is_file():
        task.result_file.rename(task.result_file.with_suffix(".first-attempt.json"))
    result = read_task(task, directory, timeout, retry=True)
    result["retry"] = {"timeout": timeout, "exit_code": result["exit_code"],
                       "first_exit_code": first["exit_code"], "first_stdout": first["stdout"],
                       "first_stderr": first["stderr"], "first_error": first["error"]}
    return result


def earlier_reads(root: Path) -> list[tuple[Path, dict[str, dict]]]:
    history = []
    for path in root.glob("*/results.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            history.append((path.parent, {item["name"]: item for item in data
                                          if isinstance(item, dict) and item.get("name") in COMPARE
                                          and item.get("exit_code") == 0}))
    return history


def earlier_artifacts(history: list[tuple[Path, dict[str, dict]]], name: str,
                      field: str) -> list[tuple[Path, dict, Path]]:
    candidates = []
    for directory, records in history:
        record = records.get(name, {})
        if not (filename := record.get(field)):
            continue
        artifact = directory / Path(filename).name
        stamp = record.get("read_at")
        if not isinstance(stamp, (float, int)):
            try:
                stamp = (directory / Path(record.get("stdout") or filename).name).stat().st_mtime
            except OSError:
                continue
        candidates.append((stamp, directory, record, artifact))
    return [(directory, record, artifact) for _, directory, record, artifact in
            sorted(candidates, key=lambda item: item[0], reverse=True)]


def comparison_data(path: Path, name: str) -> dict[str, dict | str]:
    text = path.read_text(encoding="utf-8")
    if name == "backlog-watch":
        return {BACKLOG_PR_ELAPSED.sub(r"\1", BACKLOG_ELAPSED.sub(" | ", line, count=1)): line
                for line in text.splitlines() if BACKLOG_ITEM.match(line)}
    data = json.loads(text)
    rows = data.get("gaps", []) if isinstance(data, dict) else data
    if name == "tracker-gaps":
        return {row["key"]: row for row in rows}
    return {str(row.get("url") or row.get("task")): row for row in rows}


def has_partial_asana_output(directory: Path, record: dict) -> bool:
    name = record.get("stdout")
    if not name:
        return False
    try:
        return bool(ASANA_PARTIAL.search((directory / Path(name).name).read_text(encoding="utf-8")))
    except OSError:
        return False


def compare_reads(results: list[dict], history: list[tuple[Path, dict[str, dict]]]) -> None:
    for result in results:
        name = result["name"]
        if name not in COMPARE or result["exit_code"] != 0:
            continue
        field = "stdout" if name == "backlog-watch" else "result"
        path = Path(result[field])
        try:
            if name == "asana99-status" and has_partial_asana_output(path.parent, result):
                result["error"] = "比較できません。レビュー依頼の取得が完了していません"
                continue
            current = comparison_data(path, name)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            result["error"] = f"比較データ: {error}"
            continue
        previous = {}
        for directory, record, artifact in earlier_artifacts(history, name, field):
            if name == "asana99-status" and has_partial_asana_output(directory, record):
                continue
            try:
                previous = comparison_data(artifact, name)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            result["compared_to"] = str(directory)
            break
        new = sorted(current.keys() - previous.keys())
        gone = sorted(previous.keys() - current.keys())
        if name == "tracker-gaps":
            log = Path(os.environ.get("TRIAGE_LEAD_DECISIONS") or lead_decisions.DEFAULT_LOG).expanduser()
            try:
                decisions = lead_decisions.latest(lead_decisions.records(log))
            except (OSError, ValueError) as error:
                result["error"] = f"tracker の決定を確認できません（{error}）"
                decisions = {}
            document = json.loads(path.read_text(encoding="utf-8"))
            document = document if isinstance(document, dict) else {"gaps": document}
            for key, row in current.items():
                decision = decisions.get(f"tracker:{key}")
                if (isinstance(row, dict) and decision
                        and re.match(r"\s*放置(?:[：:。\s]|$)", decision.answer)):
                    row.update(ignored=True, ignore_decision={"answer": decision.answer, "decided_at": decision.decided_at})
            document.update(gaps=list(current.values()), new=[current[key] for key in new],
                            gone=[previous[key] for key in gone])
            json_output.emit(document, path)
        changed = (sorted(key for key in current.keys() & previous.keys() if current[key] != previous[key])
                   if name == "asana99-status" else [])
        result["changes"] = {"new": new, "gone": gone, "changed": changed}
        if name == "backlog-watch":
            result["changes"].update(new=[current[key] for key in new], gone=[previous[key] for key in gone])


def print_read(result: dict, now: datetime) -> None:
    name = result["name"]
    print(f"{name}: exit {result['exit_code']}" + (f" | {result['error']}" if result.get("error") else ""))
    if retry := result.get("retry"):
        print(f"{name}: retry exit {retry['first_exit_code']} → {retry['exit_code']} (timeout {retry['timeout']:g}s)")
    if changes := result.get("changes"):
        print(f"{name}: compared to {result.get('compared_to') or '前回の成功記録なし'}")
        for kind, rows in changes.items():
            if rows:
                print(f"  {kind} {len(rows)}: " + " | ".join(rows))
    if result["exit_code"] != 0 or name not in ("open-asks", "stale-asks"):
        return
    rows = json.loads(Path(result["result"]).read_text(encoding="utf-8"))
    if name == "stale-asks":
        if summary := hourly_state.summary(rows, now):
            print(summary)
    else:
        for row in rows:
            text = " ".join((row.get("text") or "").split())
            print(f"{row.get('ts') or '-'} {row.get('source') or '-'} id={row['id']} {text[:160]}"
                  + ("…" if len(text) > 160 else ""))


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
        ReadTask("tracker-gaps", [str(tools / "project-status"), "--gaps", "--json"], True),
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
    parser.add_argument("--bin-dir", type=Path, default=Path("~/.local/share/eotel-harness/current/bin"),
                        help="installed backlog-watch, asana99_status.py, topic and project-status commands")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per command (default: %(default)s)")
    args = parser.parse_args(argv)
    if not args.workspace or args.timeout <= 0:
        parser.error("--workspace (or ORCA_WORKTREE_ID) and a positive --timeout are required")
    try:
        root = args.output_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        history = earlier_reads(root)
        now = datetime.now(timezone.utc)
        directory = Path(tempfile.mkdtemp(prefix=now.strftime("%Y%m%dT%H%M%SZ-"), dir=root))
        print(f"run: {directory}", flush=True)
        try:
            loaded = os.getloadavg()[0] > LOAD_LIMIT
        except OSError:
            loaded = False
        timeout = args.timeout * LOAD_TIMEOUT_FACTOR if loaded else args.timeout
        tasks = tasks_for(args, directory)
        reads_answers = {"lead-state", "unanswered", "pending", "scan"}
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {task.name: pool.submit(read_with_retry, task, directory, timeout, loaded)
                       for task in tasks if task.name not in reads_answers}
            futures["open-asks"].result()
            futures.update({task.name: pool.submit(read_with_retry, task, directory, timeout, loaded)
                            for task in tasks if task.name in reads_answers})
            results = [futures[task.name].result() for task in tasks]
        scan = next(item for item in results if item["name"] == "scan")
        if scan["exit_code"] == 0:
            stale_file = directory / "stale-asks.partial.json"
            stale = ReadTask("stale-asks", [str(Path(__file__).with_name("hourly_state.py")),
                "--state-file", str(args.state_file.expanduser()), "stale-asks", "--from", scan["result"],
                "--orca", args.orca, "--out", str(stale_file)], True, stale_file)
            results.append(read_with_retry(stale, directory, timeout, loaded))
        else:
            results.append({**dict.fromkeys(RECORD_KEYS), "name": "stale-asks", "error": "skipped: scan failed"})
        compare_reads(results, history)
        json_output.emit(results, directory / "results.json")
        for result in results:
            print_read(result, now)
        if scan["exit_code"] == 0:
            queue_path = directory / "review-queue.json"
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            print(f"review queue: {queue_path}（{len(queue['rows'])} 行）／分類: orca-row-classify {directory}")
        print(f"results: {directory / 'results.json'}（命令ごとの記録の配列。各記録の鍵: {'・'.join(RECORD_KEYS)}）")
        return int(any(result["exit_code"] != 0 for result in results))
    except (OSError, ValueError) as error:
        print(f"hourly read: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
