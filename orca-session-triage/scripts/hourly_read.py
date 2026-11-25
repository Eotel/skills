#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
"""Run the hourly triage reads in parallel and keep each result outside terminal output.

Each invocation creates a unique directory under --output-root and ends by printing
the path of its results.json: a JSON array with one record per command, each with
the keys name, command, exit_code, stdout and stderr (the paths of the command's
output files), result (the path of its .json result, or null), error, retry,
compared_to, and changes.
At startup, reads/current points to the new run and NEXT_OFFSET is recorded. The
lines `<name>: exit N | PATH` report each completed command. Failed commands keep
their stdout and stderr; only successful, parseable JSON is published as a .json
result. Cleanup can close previous completed scheduled tabs under its existing
safety checks.
"""

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
from queue import SimpleQueue
import re
import socket
import subprocess
import sys
import tempfile
import time

import hourly_runtime
import hourly_state
import inbox_read
import json_output
import lead_decisions
import scan_sessions
from bash_timeout import LOAD_LIMIT
from rm_check import TOPIC_FIXER_JSON, split_topic_suffix, topic_check_outputs, topic_fixer_record

RECORD_KEYS = ("name", "command", "exit_code", "stdout", "stderr", "result", "error", "retry", "compared_to", "changes",
               "read_at", "next_offset")
DEFAULT_READS = Path("~/.cache/orca-session-triage/reads")
LOAD_TIMEOUT_FACTOR = 2.5
COMPARE = ("tracker-gaps", "asana99-status", "backlog-watch", "requests-needing-owner")
BACKLOG_ITEM = re.compile(r"^(?:返信|返信不要|担当確認|判定済み) [A-Z][A-Z0-9_]+-\d+\b")
BACKLOG_ELAPSED = re.compile(r" \| (?:\d+時間)?\d+分 \| ")
BACKLOG_PR_ELAPSED = re.compile(
    r"( \| PR [\w.-]+/[\w.-]+#\d+ (?:レビュー待ち|下書き|承認済み|修正依頼|open)) \d+(?:日|時間)(?= →| \||$)")
BACKLOG_CURSOR = Path("~/.cache/orca-briefs/backlog-watch/state.json")
ASANA_PARTIAL = re.compile(r"^Eotel へのレビュー依頼: 未取得", re.MULTILINE)
BACKLOG_READ_FAILURE = re.compile(r"backlog API.{0,30}(?:fail|失敗|取得でき|読み込め)|"
                                 r"failed to read.{0,30}backlog", re.IGNORECASE)
OPEN_PRS = ("fragment Linked on PullRequest { url state isDraft updatedAt headRefName "
            "headRepository { nameWithOwner } }")
ISSUE_LINK = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")


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
            env = {**os.environ, "TOPIC_CHECK_FIXER_JSON": "1"} if task.name == "topic-check" else None
            completed = subprocess.run(task.command, stdout=output, stderr=errors, text=True, timeout=timeout, env=env)
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
    return retry_read(task, directory, timeout, first, "high load timeout")


def retry_read(task: ReadTask, directory: Path, timeout: float, first: dict, reason: str) -> dict:
    if task.result_file and task.result_file.is_file():
        task.result_file.rename(task.result_file.with_suffix(".first-attempt.json"))
    result = read_task(task, directory, timeout, retry=True)
    result["retry"] = {"timeout": timeout, "reason": reason, "exit_code": result["exit_code"],
                       "first_exit_code": first["exit_code"], "first_stdout": first["stdout"],
                       "first_stderr": first["stderr"], "first_error": first["error"]}
    return result


def failed_host(result: dict) -> str | None:
    if result["exit_code"] in (None, 0, 124, 127) or result["retry"]:
        return None
    output = "\n".join(Path(result[field]).read_text(encoding="utf-8", errors="replace")
                       for field in ("stderr", "stdout"))
    backlog_host = os.environ.get("BACKLOG_SPACE") or "spindd.backlog.com"
    if re.search(r"error connecting to api\.github\.com\b", output, re.IGNORECASE):
        return "api.github.com"
    if BACKLOG_READ_FAILURE.search(output):
        return backlog_host
    if "URLError" in output and "nodename nor servname" in output:
        if result["name"] == "backlog-watch":
            return backlog_host
        return "app.asana.com" if result["name"] == "asana99-status" else "api.github.com"
    return None


def retry_network(tasks: list[ReadTask], finished: dict[str, dict], directory: Path, timeout: float,
                  backlog_before: bytes | None, reachable: dict[str, bool]) -> None:
    for task in tasks:
        first = finished[task.name]
        host = failed_host(first)
        if not host:
            continue
        if task.name == "backlog-watch" and (backlog_before is None or backlog_cursor() != backlog_before):
            first["error"] = "通知カーソルが更新済みか確認できないため、通信エラーを再試行しません"
            continue
        if host not in reachable:
            try:
                with socket.create_connection((host, 443), timeout=5):
                    reachable[host] = True
            except OSError as error:
                print(f"network: {host}: {error}", flush=True)
                reachable[host] = False
        if reachable[host]:
            result = retry_read(task, directory, timeout, first, f"network: {host}")
            finished[task.name] = result
            print_completion(result)


def earlier_reads(root: Path) -> list[tuple[Path, dict[str, dict]]]:
    history = []
    for path in root.glob("*/results.json"):
        if path.parent.is_symlink():
            continue
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
    if name == "requests-needing-owner":
        return {row["id"]: row for row in data["needs_owner"]}
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
        if name == "requests-needing-owner":
            closed = json.loads(path.read_text(encoding="utf-8"))["closed_issues"]
            result["changes"] = {"new": new, "closed_issues": [item["id"] for item in closed]}
        else:
            result["changes"] = {"new": new, "gone": gone, "changed": changed}
        if name == "backlog-watch":
            result["changes"].update(new=[current[key] for key in new], gone=[previous[key] for key in gone])


def print_read(result: dict, now: datetime) -> None:
    name = result["name"]
    if retry := result.get("retry"):
        print(f"{name}: retry exit {retry['first_exit_code']} → {retry['exit_code']}"
              f" ({retry['reason']}, timeout {retry['timeout']:g}s)")
    if changes := result.get("changes"):
        if name == "requests-needing-owner":
            requests = json.loads(Path(result["result"]).read_text(encoding="utf-8"))
            print(f"{name}: needs_owner {len(requests['needs_owner'])} | closed_issues {len(requests['closed_issues'])}")
        print(f"{name}: compared to {result.get('compared_to') or '前回の成功記録なし'}")
        for kind, rows in changes.items():
            if rows:
                print(f"  {kind} {len(rows)}: " + " | ".join(rows))
    if name == "topic-check":
        display: dict[str, str] = {}
        for path in topic_check_outputs(Path(result["stdout"]).parent):
            lines = path.read_text(encoding="utf-8").splitlines()
            structured = {}
            for line in lines:
                try:
                    record = topic_fixer_record(line, path)
                except ValueError as error:
                    print(f"topic-check: {error}", file=sys.stderr)
                    continue
                if record:
                    pr, workspace, _ = record
                    structured[pr] = workspace
            record_key = ""
            for line in lines:
                if line and not line[0].isspace() and not line.startswith(TOPIC_FIXER_JSON):
                    record_key = line
                if record_key not in structured and line.lstrip().startswith("直す作業場: "):
                    worktree, _ = split_topic_suffix(line.lstrip().removeprefix("直す作業場: "))
                    display.setdefault(worktree, line)
            for workspace in structured.values():
                display.setdefault(str(workspace), f"  直す作業場: {workspace}")
        for line in display.values():
            print(line)
    if result["exit_code"] != 0 or name not in ("open-asks", "stale-asks", "pr-followups"):
        return
    rows = json.loads(Path(result["result"]).read_text(encoding="utf-8"))
    if name == "stale-asks":
        if summary := hourly_state.summary(rows, now):
            print(summary)
    elif name == "pr-followups":
        print(f"{name}: 48時間以上更新なし {sum(row['stale'] for row in rows)}件"
              f" | 作業場なし {sum(not row['worktrees'] for row in rows)}件")
        for row in rows:
            status = ["Draft"] if row["draft"] else []
            status.extend(["48時間以上更新なし"] if row["stale"] else [])
            status.extend(["作業場なし"] if not row["worktrees"] else row["worktrees"])
            print(f"  {row['url']} | {' / '.join(status)} | " + " ".join(row["issues"]))
    else:
        for row in rows:
            text = " ".join((row.get("text") or "").split())
            status = inbox_read.open_ask_status(row)
            print(f"{row.get('ts') or '-'} {row.get('source') or '-'} id={row['id']} {text[:160]}"
                  + ("…" if len(text) > 160 else "") + (f" | {status}" if status else ""))


def linked_open_prs() -> dict[str, dict]:
    limit = hourly_state.ISSUE_LIMIT
    assigned, error = hourly_state.gh_json(["search", "issues", "--assignee", "Eotel", "--state", "open",
                                            "--limit", str(limit), "--json", "url"])
    if not isinstance(assigned, list) or len(assigned) >= limit:
        raise ValueError(error or "GitHub issue search did not return a complete result")
    repos: dict[str, set[int]] = {}
    for row in assigned:
        if found := ISSUE_LINK.fullmatch(row.get("url") or ""):
            repos.setdefault(found[1], set()).add(int(found[2]))
    prs: dict[str, dict] = {}
    for repo, refs in repos.items():
        owner, name = repo.split("/", 1)
        numbers = sorted(refs)
        for start in range(0, len(numbers), scan_sessions.GRAPHQL_CHUNK):
            part = numbers[start:start + scan_sessions.GRAPHQL_CHUNK]
            fields = " ".join(f"i{number}: issue(number: {number}) {{ url state "
                              + hourly_state.LINKED_PRS.format(n=scan_sessions.GRAPHQL_CHUNK) + " }" for number in part)
            query = f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }} {OPEN_PRS}"
            document = scan_sessions.graphql_repository(repo, query)
            if document is None:
                raise ValueError(f"Could not read linked PRs in {repo}")
            for number in part:
                issue = document.get(f"i{number}") or {}
                if issue.get("state") != "OPEN":
                    continue
                closed_by = (issue.get("closedByPullRequestsReferences") or {}).get("nodes") or []
                mentioned = ((event or {}).get("source")
                             for event in (issue.get("timelineItems") or {}).get("nodes") or [])
                for pr in [*closed_by, *mentioned]:
                    if isinstance(pr, dict) and pr.get("state") == "OPEN" and pr.get("url"):
                        prs.setdefault(pr["url"], {**pr, "issues": set()})["issues"].add(issue["url"])
    return prs


def pr_followups(rows: list[dict], now: datetime) -> list[dict]:
    prs = linked_open_prs()
    if not prs:
        return []
    checkouts = [(row, scan_sessions.github_of(row).casefold()) for row in rows]
    followups = []
    for url, pr in prs.items():
        head_repo = ((pr.get("headRepository") or {}).get("nameWithOwner") or "").casefold()
        branch = pr.get("headRefName")
        worktrees = []
        for row, repo in checkouts:
            details = row.get("git") or {}
            if (any((row.get(key) or {}).get("url") == url for key in ("linked_pr", "pr"))
                    or head_repo and repo == head_repo and branch
                    and (details.get("branch") or row.get("branch") or "").removeprefix("refs/heads/") == branch):
                worktrees.append(scan_sessions.row_identity(row))
        updated = scan_sessions.parse_time(pr.get("updatedAt"))
        stale = updated is not None and now - updated >= timedelta(hours=48)
        if stale or not worktrees:
            followups.append({"url": url, "draft": bool(pr.get("isDraft")), "stale": stale,
                              "updated_at": updated.astimezone(lead_decisions.JST).isoformat() if updated else None,
                              "issues": sorted(pr["issues"]), "worktrees": sorted(set(worktrees))})
    return sorted(followups, key=lambda pr: (bool(pr["worktrees"]), pr["updated_at"] or "", pr["url"]))


def print_completion(result: dict) -> None:
    details = [str(field) for field in (result.get("result") or result.get("stdout"), result.get("error"))
               if field]
    outcome = f"exit {result['exit_code']}" if result["exit_code"] is not None else "skip"
    print(" | ".join([f"{result['name']}: {outcome}", *details]), flush=True)


def read_inbox(directory: Path, timeout: float) -> dict:
    task = ReadTask("inbox", [str(Path(__file__).with_name("inbox_read.py")), "read", "--summary"])
    result = read_task(task, directory, timeout)
    if result["exit_code"] == 0:
        lines = Path(result["stdout"]).read_text(encoding="utf-8").splitlines()
        match = re.fullmatch(r"NEXT_OFFSET=(\d+)", lines[-1]) if lines else None
        if match:
            position = int(match[1])
            output = directory / "inbox.json"
            json_output.emit({"pane": os.environ.get("ORCA_PANE_KEY") or "",
                              "inbox": str(inbox_read.paths()[0].resolve()), "next_offset": position}, output)
            result.update(next_offset=position, result=str(output))
            print(f"NEXT_OFFSET={position}", flush=True)
        else:
            result.update(exit_code=1, error="NEXT_OFFSET is missing from the inbox read")
    print_completion(result)
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
        ReadTask("tracker-gaps", [str(tools / "project-status"), "--gaps", "--json"], True),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=hourly_runtime.workspace_path(
        os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")),
        help="the hourly workspace whose old scheduled tabs cleanup may close")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_READS,
                        help="parent of the unique run directory (default: %(default)s)")
    parser.add_argument("--state-file", type=Path, default=Path(hourly_state.DEFAULT_STATE))
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"))
    parser.add_argument("--bin-dir", type=Path, default=Path("~/.local/share/eotel-harness/current/bin"),
                        help="installed backlog-watch, asana99_status.py, topic and project-status commands")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per command (default: %(default)s)")
    parser.add_argument("--scan-prs", type=Path, metavar="SCAN_JSON",
                        help="read stale or unowned PRs referenced by assigned open issues, using these scanned worktrees")
    args = parser.parse_args(argv)
    if not args.scan_prs and (not args.workspace or args.timeout <= 0):
        parser.error("--workspace (or ORCA_WORKTREE_ID) and a positive --timeout are required")
    try:
        if args.scan_prs:
            json_output.emit(pr_followups(scan_sessions.saved_rows(str(args.scan_prs.expanduser())),
                                          datetime.now(timezone.utc)))
            return 0
        root = args.output_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        history = earlier_reads(root)
        now = datetime.now(timezone.utc)
        directory = Path(tempfile.mkdtemp(prefix=now.strftime("%Y%m%dT%H%M%SZ-"), dir=root))
        latest = directory / ".current"
        latest.symlink_to(directory, target_is_directory=True)
        latest.replace(root / "current")
        print(f"run: {directory}", flush=True)
        try:
            loaded = os.getloadavg()[0] > LOAD_LIMIT
        except OSError:
            loaded = False
        timeout = args.timeout * LOAD_TIMEOUT_FACTOR if loaded else args.timeout
        inbox = read_inbox(directory, timeout)
        tasks = tasks_for(args, directory)
        backlog_before = backlog_cursor()
        reachable: dict[str, bool] = {}
        reads_answers = {"lead-state", "unanswered", "pending", "scan"}
        finished: dict[str, dict] = {}
        completed: SimpleQueue[Future[dict]] = SimpleQueue()
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            def submit(task: ReadTask) -> None:
                pool.submit(read_with_retry, task, directory, timeout, loaded).add_done_callback(completed.put)

            for task in tasks:
                if task.name not in reads_answers:
                    submit(task)
            for _ in tasks:
                result = completed.get().result()
                finished[result["name"]] = result
                print_completion(result)
                if result["name"] == "open-asks":
                    for task in tasks:
                        if task.name in reads_answers:
                            submit(task)
        retry_network(tasks, finished, directory, timeout, backlog_before, reachable)
        results = [inbox, *(finished[task.name] for task in tasks)]
        scan = next(item for item in results if item["name"] == "scan")
        dependent = ("stale-asks", "requests-needing-owner", "pr-followups")
        if scan["exit_code"] == 0:
            stale_file = directory / "stale-asks.partial.json"
            command = [str(Path(__file__).with_name("hourly_state.py")), "--state-file", str(args.state_file.expanduser())]
            common = ["--from", str(scan["result"]), "--orca", args.orca]
            followups = [
                ReadTask("stale-asks", [*command, "stale-asks", *common, "--out", str(stale_file)], True, stale_file),
                ReadTask("requests-needing-owner", [*command, "requests-needing-owner", *common], True),
                ReadTask("pr-followups", [str(Path(__file__)), "--scan-prs", str(scan["result"])], True),
            ]
            with ThreadPoolExecutor(max_workers=len(followups)) as pool:
                after_scan = {}
                for future in as_completed(pool.submit(read_with_retry, task, directory, timeout, loaded)
                                           for task in followups):
                    result = future.result()
                    print_completion(result)
                    after_scan[result["name"]] = result
            retry_network(followups, after_scan, directory, timeout, backlog_before, reachable)
            results.extend(after_scan[task.name] for task in followups)
        else:
            for name in dependent:
                skipped = {**dict.fromkeys(RECORD_KEYS), "name": name, "error": "skipped: scan failed"}
                print_completion(skipped)
                results.append(skipped)
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
