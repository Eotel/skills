"""Offline behavior tests for the orca-session-triage command line tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from types import ModuleType
from typing import Literal, NotRequired, TextIO, TypedDict, Unpack

from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from threading import Barrier, Event
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request



class TextRunOptions(TypedDict):
    text: Literal[True]
    capture_output: NotRequired[bool]
    check: NotRequired[bool]
    timeout: NotRequired[float | None]
    cwd: NotRequired[str | Path]
    env: NotRequired[dict[str, str]]
    input: NotRequired[str | None]


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "orca-session-triage" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scan = load_script("scan_sessions")
rmcheck = load_script("rm_check")
merged_pr_head = load_script("merged_pr_head")
precheck = load_script("precheck")
hourly_runtime = sys.modules["hourly_runtime"]
hourly_state = load_script("hourly_state")
stopcheck = load_script("stop_check")
bash_timeout = load_script("bash_timeout")
inbox_service = load_script("inbox_watch_service")
inbox_read = load_script("inbox_read")
cleanup_hourly = load_script("cleanup_hourly_tabs")
hourly_runs = load_script("hourly_runs")
inbox_alert = load_script("inbox_alert")
close_tabs = importlib.import_module("close_tabs")
handoff = importlib.import_module("handoff")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def user(text: str | list[dict]) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def text(value: str) -> dict[str, str]:
    return {"type": "text", "text": value}


def each_ref(lookup: Callable[[str, object], dict | None]) -> Callable[[str, list], dict]:
    """A repository's PR lookup built from one that answers a single ref."""
    return lambda slug, refs: {ref: lookup(slug, ref) for ref in refs}


def pr_node(number: int, branch: str = "fix", state: str = "OPEN", updated_at: str = "2026-10-28T00:00:00Z", **fields: object) -> dict:
    """A PullRequest node as scan_sessions' GraphQL query returns it."""
    return {"number": number, "url": f"https://github.com/acme/app/pull/{number}", "state": state,
            "updatedAt": updated_at, "isCrossRepository": False, "headRefName": branch, "headRefOid": "aaa",
            "reviewDecision": None, "mergeStateStatus": "CLEAN", "mergedAt": None,
            "author": {"__typename": "User", "login": "author"},
            "reviewRequests": {"nodes": []}, "latestReviews": {"nodes": []},
            "commits": {"nodes": [{"commit": {"committedDate": "2026-10-27T00:00:00Z",
                                              "statusCheckRollup": None}}]}, **fields}


class FakeGitHub:
    """`gh` for a few repositories, keeping every call so a test can count what a run asked.

    `gh api graphql` answers each alias from the repository's PR nodes, newest first; a
    missing PR is a null alias with a NOT_FOUND error, and gh then exits 1 while the
    other aliases keep their data. An issue or PR read for its state answers from the
    PR nodes. A REST read answers from `activity` by endpoint.
    """

    def __init__(self, repos: dict[str, list[dict]], activity: dict[str, list[dict]] | None = None) -> None:
        self.repos, self.activity, self.calls = repos, activity or {}, []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[:3] != ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.activity.get(argv[-1], [])]), "")
        query = next(arg for arg in argv if arg.startswith("query=")).removeprefix("query=")
        if "fragment PrFacts" in query and "...PrFacts" not in query:
            return subprocess.CompletedProcess(argv, 1, "", "gh: Fragment PrFacts was defined, but not used")
        repository_match = re.search(r'repository\(owner: ("[^"]*"), name: ("[^"]*")\)', query)
        assert repository_match is not None
        owner, name = repository_match.groups()
        nodes = sorted(self.repos.get(json.loads(owner) + "/" + json.loads(name), []), key=lambda n: -n["number"])
        repository: dict = {"defaultBranchRef": {"name": "main"}}
        errors = []
        for alias, number in re.findall(r"(\w+): pullRequest\(number: (\d+)\)", query):
            repository[alias] = next((n for n in nodes if n["number"] == int(number)), None)
            if repository[alias] is None:
                errors.append({"type": "NOT_FOUND", "path": ["repository", alias]})
        for alias, branch in re.findall(r'(\w+): pullRequests\(headRefName: ("(?:[^"\\]|\\.)*")', query):
            repository[alias] = {"nodes": [n for n in nodes if n["headRefName"] == json.loads(branch)][:5]}
        for alias, number in re.findall(r"(\w+): issueOrPullRequest\(number: (\d+)\)", query):
            repository[alias] = next(({"state": n["state"]} for n in nodes if n["number"] == int(number)), None)
        body = {"data": {"repository": repository}, **({"errors": errors} if errors else {})}
        return subprocess.CompletedProcess(argv, 1 if errors else 0, json.dumps(body), "")

    def graphql(self) -> list[list[str]]:
        return [call for call in self.calls if call[:3] == ["gh", "api", "graphql"]]

    def rest(self) -> list[list[str]]:
        return [call for call in self.calls if call[:3] != ["gh", "api", "graphql"]]


class ClaudeTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)
        self.worktree = "/Users/me/orca/workspaces/app/fix.login_2"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def transcript(self, rows: list[dict], name: str = "s1.jsonl") -> Path:
        path = self.projects / "-Users-me-orca-workspaces-app-fix-login-2" / name
        write_jsonl(path, rows)
        return path

    def test_reads_final_answer_after_last_real_user_message(self) -> None:
        self.transcript([
            user("first request"),
            assistant(text("old answer")),
            user("second request"),
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
            user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
            assistant(text("working on it")),
            {**assistant(text("Done. Shall I merge it?")), "timestamp": "2026-10-01T01:08:00Z"},
        ])

        result = scan.read_claude(self.projects, self.worktree)

        self.assertEqual(result["last_user"], "second request")
        self.assertEqual(result["final_text"], "Done. Shall I merge it?")
        self.assertEqual(result["final_at"], "2026-10-01T01:08:00Z")
        self.assertEqual(result["pending_questions"], [])

    def test_a_compaction_summary_is_not_the_users_last_word(self) -> None:
        self.transcript([
            user("sync the partner master"),
            assistant(text("devel is filled; stage waits for the 12:00 import.")),
            {"type": "system", "subtype": "compact_boundary"},
            {"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
             "message": {"role": "user", "content": "This session is being continued from a previous conversation."}},
        ])

        result = scan.read_claude(self.projects, self.worktree)

        self.assertEqual((result["last_user"], result["final_text"]),
                         ("sync the partner master", "devel is filled; stage waits for the 12:00 import."))

    def test_lists_ask_user_question_without_a_result(self) -> None:
        answered = {"questions": [{"question": "Which base?", "options": []}]}
        pending = {"questions": [{"question": "Merge now?", "options": [{"label": "Yes"}]}]}
        self.transcript([
            user("ship it"),
            assistant({"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": answered}),
            user([{"type": "tool_result", "tool_use_id": "q1", "content": "main"}]),
            assistant({"type": "tool_use", "id": "q2", "name": "AskUserQuestion", "input": pending}),
        ])

        result = scan.read_claude(self.projects, self.worktree)

        self.assertEqual(result["pending_questions"], [pending])

    def test_skips_entries_whose_message_is_not_an_object(self) -> None:
        self.transcript([
            user("go"),
            {"type": "assistant", "message": None},
            {"type": "assistant", "message": "plain string"},
            assistant(text("still readable")),
        ])

        self.assertEqual(scan.read_claude(self.projects, self.worktree)["final_text"], "still readable")

    def test_ignores_lines_that_are_json_but_not_objects(self) -> None:
        path = self.transcript([user("go"), assistant(text("answer"))])
        with Path(path).open("a") as handle:
            handle.write("null\n5\n\"text\"\n")

        self.assertEqual(scan.read_claude(self.projects, self.worktree)["final_text"], "answer")

    def test_finds_the_truncated_directory_of_a_long_path(self) -> None:
        long_path = "/Users/me/" + "deep-directory/" * 16 + "wt"
        folder = self.projects / (scan.claude_project_dir("", long_path).name[:200] + "-x1y2z3")
        write_jsonl(folder / "s.jsonl", [dict(user("go"), cwd=long_path), assistant(text("found"))])
        write_jsonl(self.projects / (folder.name[:-6] + "other") / "s.jsonl",
                    [dict(user("go"), cwd=long_path + "-sibling"), assistant(text("wrong"))])

        self.assertEqual(scan.read_claude(self.projects, long_path)["final_text"], "found")

    def test_returns_none_without_a_transcript(self) -> None:
        self.assertIsNone(scan.read_claude(self.projects, "/nowhere"))

    def test_queued_input_replays_all_operations_and_reaches_the_scan_row(self) -> None:
        stamp = "2026-10-01T01:00:00Z"
        operations = [("dequeue", None), ("enqueue", "cleared"), ("popAll", None),
                      ("enqueue", "delivered"), ("enqueue", "first"), ("enqueue", "removed"),
                      ("dequeue", None), ("remove", "removed"), ("enqueue", "second")]
        path = self.transcript([{"type": "queue-operation", "operation": operation,
                                 "timestamp": stamp, **({"content": content} if content is not None else {})}
                                for operation, content in operations])
        now = scan.parse_time("2026-10-01T01:12:00Z")
        expected = [{"text": value, "enqueued_at": stamp, "minutes": 12} for value in ("first", "second")]

        self.assertEqual(scan.queued_input(path, now), expected)
        rows = scan.build_inventory([worktree(self.worktree)], [], self.projects, {}, "要判断", now=now.timestamp())
        self.assertEqual(rows[0]["queued_input"], expected)


def codex_row(ts: str, kind: str, payload: dict) -> dict:
    return {"timestamp": ts, "type": kind, "payload": payload}


def codex_user(ts: str, value: str) -> dict:
    return codex_row(ts, "response_item", {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": value}]})


def codex_assistant(ts: str, value: str) -> dict:
    return codex_row(ts, "response_item", {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": value}]})


def codex_ask(ts: str, call_id: str, questions: list[dict]) -> dict:
    return codex_row(ts, "response_item", {
        "type": "function_call", "name": "request_user_input_async", "call_id": call_id,
        "arguments": json.dumps({"questions": questions})})


class CodexRolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def rollout(self, name: str, cwd: str, rows: list[dict]) -> Path:
        path = self.root / "2026" / "10" / "01" / name
        write_jsonl(path, [codex_row("2026-10-01T00:00:00Z", "session_meta", {"cwd": cwd})] + rows)
        return path

    def test_groups_rollouts_by_cwd_and_keeps_unanswered_requests(self) -> None:
        self.rollout("rollout-a.jsonl", "/w/app/one", [
            codex_user("2026-10-01T01:00:00Z", "<environment_context>ignored</environment_context>"),
            codex_user("2026-10-01T01:00:01Z", "fix the login page"),
            codex_ask("2026-10-01T01:05:00Z", "c1", [{"title": "Who reviews?", "options": ["hdknr", "later"]}]),
            codex_ask("2026-10-01T01:06:00Z", "c2", [{"title": "SSH host?"}]),
            codex_row("2026-10-01T01:06:30Z", "response_item", {"type": "function_call_output", "call_id": "c1"}),
            codex_user("2026-10-01T01:07:00Z", "ssh app@host"),
            codex_assistant("2026-10-01T01:08:00Z", "Deploying now."),
        ])

        self.rollout("rollout-b.jsonl", "/w/app/two", [
            codex_user("2026-10-01T02:00:00Z", "#123 を直して"),
            codex_user("2026-10-01T02:00:01Z", "# AGENTS.md instructions for /w/app/two"),
        ])

        broken = self.rollout("rollout-c.jsonl", "/w/app/three", [])
        with Path(broken).open("a") as handle:
            handle.write("null\n[1, 2]\n")

        result = scan.read_codex([self.root])

        self.assertEqual(result["/w/app/three"][0]["pending_requests"], [])
        self.assertEqual(result["/w/app/two"][0]["last_user"], "#123 を直して")
        [session] = result["/w/app/one"]
        self.assertEqual(session["last_role"], "assistant")
        self.assertEqual(session["last_user"], "ssh app@host")
        self.assertEqual(session["last_assistant"], "Deploying now.")
        self.assertEqual(session["last_assistant_at"], "2026-10-01T01:08:00Z")
        self.assertEqual(session["pending_requests"], [{
            "asked_at": "2026-10-01T01:06:00Z",
            "questions": [{"title": "SSH host?"}],
            "answered_in_chat_later": True,
        }])

    def test_api_failure_in_rollout_is_cleared_by_a_later_assistant_message(self) -> None:
        path = self.rollout("rollout-error.jsonl", "/w/cut", [
            codex_user("2026-10-01T01:00:00Z", "finish the work"),
            codex_row("2026-10-01T01:01:00Z", "event_msg", {
                "type": "error", "message": "Selected model is at capacity"}),
        ])
        self.assertEqual(scan.summarize_rollout(path)[1]["api_error"], "Selected model is at capacity")
        with Path(path).open("a") as handle:
            handle.write(json.dumps(codex_assistant("2026-10-01T01:02:00Z", "Recovered.")) + "\n")
        self.assertIsNone(scan.summarize_rollout(path)[1]["api_error"])

    def test_a_codex_task_the_usage_limit_ended_waits_for_the_reset(self) -> None:
        limit = ("You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more credits "
                 "or try again at Nov 4th, 2026 3:35 PM.")
        path = self.rollout("rollout-limit.jsonl", "/w/cut", [
            codex_user("2026-11-03T05:40:00Z", "finish the work"),
            codex_assistant("2026-11-03T05:50:00Z", "Checking CI."),
            codex_row("2026-11-03T05:51:58Z", "event_msg", {
                "type": "task_complete", "last_agent_message": None, "error": {"message": limit}}),
        ])
        stop = scan.stop_reason({"claude": None, "codex": [scan.summarize_rollout(path)[1]]})
        self.assertEqual((stop["kind"], stop["action"], stop["resets"]), ("usage_limit", "wait", "Nov 4th, 2026 3:35 PM"))

        # A request after the reset starts a turn the limit did not end.
        with Path(path).open("a") as handle:
            handle.write(json.dumps(codex_user("2026-11-04T07:00:00Z", "continue")) + "\n")
        self.assertIsNone(scan.stop_reason({"claude": None, "codex": [scan.summarize_rollout(path)[1]]}))


def worktree(path: str, state: str | None = None, agent: str = "claude", comment: str = "", pr: dict | None = None, main: bool = False, status: str = "in-progress",
             turn: str | None = None) -> dict:
    """`turn` is the main agent's own state; Orca's `state` stays working while a background job runs."""
    return {
        "worktreeId": "repo::" + path, "path": path, "repo": "app", "branch": "refs/heads/x",
        "workspaceStatus": status, "comment": comment, "linkedPR": pr,
        "isMainWorktree": main,
        "agents": [{"agentType": agent, "state": state, **({"mainAgent": {"state": turn}} if turn else {})}]
        if state else [],
    }


def with_agents(row: dict, *agents: tuple[str, str, str | None]) -> dict:
    """The worktree row holding these Orca agents, each (type, state, main agent's state or None)."""
    return {**row, "agents": [{"agentType": kind, "state": state, **({"mainAgent": {"state": turn}} if turn else {})}
                              for kind, state, turn in agents]}


def terminal(path: str, agent: str | None, handle: str = "term_1", title: str | None = "t", preview: str = "", quiet_minutes: float = 0, orphaned: bool = False) -> dict:
    return {"worktreeId": "repo::" + path, "handle": handle, "agentIdentity": agent,
            "title": title, "connected": not orphaned, "orphaned": orphaned, "preview": preview,
            "lastOutputAt": (NOW - quiet_minutes * 60) * 1000}


NOW = 1_800_000_000
PROMPT = "app on main via python\n❯"


def done_agent(kind: str, pane: str, said: str = "", asked: str = "", minutes_ago: float = 0, interrupted: bool = False) -> dict:
    """An agent Orca shows as a done card: its turn ended `minutes_ago` in the pane `tab:leaf`."""
    return {"agentType": kind, "state": "done", "paneKey": pane, "interrupted": interrupted,
            "stateStartedAt": (NOW - minutes_ago * 60) * 1000,
            "lastAssistantMessage": said, "prompt": asked}


def in_pane(term: dict, pane: str) -> dict:
    tab, leaf = pane.split(":")
    return {**term, "tabId": tab, "leafId": leaf}


def verdict(asks: float | None = 0.0, status: str | None = "done", confidence: float | None = 0.9) -> dict:
    return {"asks": asks, "status": status, "confidence": confidence}


def judging(table: dict[str, dict]) -> Callable[[str], dict]:
    """A judge reading Jev's verdict from table, sure the work is done otherwise."""
    return lambda message: table.get(message, verdict())


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def claude_says(self, path: str, *rows: dict) -> None:
        write_jsonl(scan.claude_project_dir(self.projects, path) / "s.jsonl", list(rows))

    def inventory(self, worktrees: list[dict], terminals: list[dict], codex: dict | None = None, judge: Callable[[str], dict | None] | None = None, processes: dict | None = None, now: float = NOW) -> list[dict]:
        return scan.build_inventory(worktrees, terminals, self.projects, codex or {}, "要判断", now=now,
                                    judge=judge or judging({}), processes=processes)

    def classes(self, worktrees: list[dict], terminals: list[dict], codex: dict | None = None, judge: Callable[[str], dict | None] | None = None) -> dict:
        rows = self.inventory(worktrees, terminals, codex, judge)
        return {r["path"]: (r["class"], r["reasons"]) for r in rows}

    def closable(self, worktrees: list[dict], terminals: list[dict], codex: dict | None = None) -> dict:
        return {r["path"]: r["closable"] for r in self.inventory(worktrees, terminals, codex)}

    def test_a_finished_setup_shell_is_closable_while_the_agent_works(self) -> None:
        result = self.closable(
            [worktree("/w/a", "working")],
            [terminal("/w/a", "claude", "term_agent"),
             terminal("/w/a", None, "term_setup", title="Setup", preview=PROMPT, quiet_minutes=90)])

        self.assertEqual(result["/w/a"], ["term_setup"])

    def test_a_shell_at_a_starship_arrow_or_cross_after_direnv_output_is_closable(self) -> None:
        direnv = "direnv: error /w/a/.envrc is blocked. Run `direnv allow` to approve its content\napp on main\n"
        result = self.closable(
            [worktree("/w/a", "working")],
            [terminal("/w/a", "claude", "term_agent"),
             terminal("/w/a", None, "term_shell", preview=direnv + "➜", quiet_minutes=90),
             terminal("/w/a", None, "term_setup", title="Setup", preview=direnv + "✗ ", quiet_minutes=90)])

        self.assertEqual(result["/w/a"], ["term_shell", "term_setup"])

    def test_another_macs_shells_without_output_close_when_their_screen_ends_at_a_bare_prompt(self) -> None:
        # MBP16 lists 23 of its 24 shell tabs with neither preview nor output time.
        remote: dict = {**worktree("/remote/shells", "working"), "_environment": "macbook-pro-16"}
        terms = [{**terminal(remote["path"], None, handle), "_environment": "macbook-pro-16", "lastOutputAt": None}
                 for handle in ("idle", "busy")]
        screens = {("macbook-pro-16", "idle"): {"source": "screen", "tail": ["direnv: loading .envrc", "➜", ""]},
                   ("macbook-pro-16", "busy"): {"source": "screen", "tail": ["➜", "Receiving objects: 42%"]}}

        row = scan.build_inventory([remote], terms, self.projects, {}, "要判断", now=NOW, screens=screens)[0]

        self.assertEqual((row["closable"], row["meaningful_tabs"]), (["idle"], ["busy"]))

    def test_a_permission_prompt_on_screen_keeps_a_memory_save_reply_waiting(self) -> None:
        self.claude_says("/w/prompt", user("go"), assistant(text("Saved the session memory.")))
        tree: dict = {**worktree("/w/prompt", "waiting", status="completed"),
                "agents": [{**done_agent("claude", "tab1:leaf1"), "state": "waiting"}]}
        term = in_pane(terminal(tree["path"], "claude", "term_prompt"), "tab1:leaf1")
        question = "Do you want to make this edit to settings.py?"
        screen = {"source": "screen", "tail": ["Saved the session memory.", question,
                                                "  ❯ 1. Yes", "    2. No", "Esc to cancel"]}

        rows = scan.build_inventory([tree], [term], self.projects, {}, "要判断", now=NOW, judge=judging({}),
                                    screens={(None, term["handle"]): screen})

        self.assertEqual((rows[0]["class"], rows[0]["closable"]), ("waiting", []))
        self.assertEqual(scan.review_queue(rows)[0]["prompt_waiting"],
                         {"tab": "term_prompt", "question": question, "choices": ["1. Yes", "2. No"]})
        moved = {"source": "screen", "tail": [question, "  1. Yes", "❯ 2. No"]}
        self.assertEqual(scan.permission_prompt(moved, "term_prompt")["choices"], ["1. Yes", "2. No"])

    def test_a_permission_prompt_names_the_command_and_the_hooks_reason_it_asks_about(self) -> None:
        # 2026-10-28: svm-320-321-reply waited on this frame; the scan showed only "Do you want to proceed?".
        screen = {"source": "screen", "tail": [
            "─" * 80, " Bash command", " Post approved SVM-320 comment with notifications", "╌" * 80,
            " │ bee issue comment SVM-320 --notify 2305985 --notify 221939 --yes <",
            " │ $SP/svm320-reply.md 2>&1 | tail -5", "╌" * 80,
            " │ Hook PreToolUse:Bash requires confirmation for", " │ this command:",
            " │ Backlog に書き込むコマンドです。外部の人が読む文は、三浦さんが下書きを見てか", " │ ら投稿します。 [settings]",
            " settings.json to update hooks", "", " Do you want to proceed?", " ❯ 1. Yes", "   2. No", "",
            " Esc to cancel · Tab to amend"]}

        prompt = scan.permission_prompt(screen, "term_svm")

        self.assertEqual(prompt["target"], " / ".join([
            "Bash command: Post approved SVM-320 comment with notifications",
            "bee issue comment SVM-320 --notify 2305985 --notify 221939 --yes <…",
            "Hook: Backlog に書き込むコマンドです。外部の人が読む文は、三浦さんが下書きを見てから投稿します。"]))
        self.assertEqual(scan.waiting_reasons({"prompt_waiting": prompt, "claude": None, "codex": [], "agents": [],
                                               "comment": ""}, "要判断"),
                         ["permission prompt on screen: Do you want to proceed? — " + prompt["target"]])

    def test_a_question_selector_on_screen_waits_whatever_verdict_was_saved(self) -> None:
        # A Claude Orca keeps no agent of (hooks of a background session do not reach it) shows AskUserQuestion.
        tree = worktree("/w/report")
        footer = "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"
        term = terminal(tree["path"], "claude", "term_ask", preview="report on main")
        screen = {"source": "screen", "tail": [
            "⏺ 11月の表を作りました。", "─" * 40, "←  ☐ 不具合  ☐ Asana  ✔ Submit  →", "",
            "報告書の第2節の注記と第3節の11月の予定が矛盾します。どちらの決まりに", "しますか？", "",
            "❯ 1. 注記に合わせる (Recommended)", "     不具合の修正は工数に入れない", "  2. 予定に合わせる",
            "     緊急対応は工数に入れる", "  3. Type something.", "─" * 40, "  4. Chat about this", "", footer]}

        self.assertEqual(scan.screen_targets([tree], [term]), [term])
        rows = scan.build_inventory([tree], [term], self.projects, {}, "要判断", now=NOW, judge=judging({}),
                                    screens={(None, "term_ask"): screen})
        question = "報告書の第2節の注記と第3節の11月の予定が矛盾します。どちらの決まりにしますか？"
        self.assertEqual(rows[0]["prompt_waiting"], {
            "tab": "term_ask", "question": question,
            "choices": ["1. 注記に合わせる (Recommended)", "2. 予定に合わせる", "3. Type something.",
                        "4. Chat about this"]})
        saved = {"class": "blocked", "question": "空気環境測定の 24 万円を請求しますか？",
                 "read": scan.read_key(rows[0])}
        [shown] = scan.with_verdicts(rows, {scan.row_identity(rows[0]): saved})
        view = scan.row_view(shown)
        self.assertEqual((view["class"], view["question"]), ("waiting", question))

    def test_question_on_a_forked_live_session_carries_its_actual_asked_time(self) -> None:
        self.claude_says("/w/report", user("go"), {**assistant(text("Parent reply")),
                                                  "timestamp": "2026-11-02T01:00:00Z"})
        child = self.projects / "fork" / "child.jsonl"
        write_jsonl(child, [user("continue"), {**assistant({"type": "tool_use", "id": "q1", "name": "AskUserQuestion",
            "input": {"questions": [{"question": "どちらにしますか？"}]}}), "timestamp": "2026-11-09T02:00:00Z"}])
        screen = {"source": "screen", "tail": ["← ☐ 方針 ✔ Submit →", "どちらにしますか？", "❯ 1. 続ける",
                                                 "  2. 待つ", "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"]}
        [row] = scan.build_inventory([worktree("/w/report")], [terminal("/w/report", "claude", "term_q")],
                                     self.projects, {}, "要判断", now=NOW,
                                     live_transcripts={"/w/report": [child]}, screens={(None, "term_q"): screen})
        self.assertEqual(row["prompt_waiting"]["asked_at"], "2026-11-09T02:00:00Z")

    def test_a_running_claude_transcript_outside_the_worktree_is_read(self) -> None:
        sessions = Path(self.tmp.name) / "profile/sessions"
        sessions.mkdir(parents=True)
        def session(pid: int, session_id: str, cwd: str) -> None:
            (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": session_id, "cwd": cwd}))
        # The tab's Claude (cwd: the worktree) started a background Claude through its daemon, which moved away.
        session(1114, "tab", "/w/report")
        session(88175, "moved", "/ghq/work-reports/.claude/worktrees/readability")
        # Another tab's Claude moved its own cwd; only its shell is still in the worktree.
        session(70, "wandered", "/ghq/other")
        session(99, "gone", "/w/report")
        processes = {88175: (88141, "claude bg-spare"), 88141: (88093, "claude bg-pty-host"),
                     88093: (1114, "/opt/homebrew/bin/claude"), 1114: (990, "claude"), 990: (1, "-zsh"),
                     70: (60, "claude"), 60: (1, "-zsh"), 99: (1, "vim")}
        cwds = {88141: "/private/tmp/cc-daemon", 88093: "/Users/x", 990: "/w/report", 60: "/w/shell", 1: "/"}
        for cwd, session_id, said, at in (
                ("/ghq/work-reports/.claude/worktrees/readability", "moved", "どちらにしますか？", "2026-11-04T03:02:00Z"),
                ("/ghq/other", "wandered", "Done.", "2026-11-04T03:00:00Z"),
                ("/w/report", "tab", "Started.", "2026-11-02T01:46:39Z")):
            write_jsonl(scan.claude_project_dir(self.projects, cwd) / f"{session_id}.jsonl",
                        [user("go"), {**assistant(text(said)), "timestamp": at}])
        asked = assistant({"type": "tool_use", "id": "ask1", "name": "AskUserQuestion",
                           "input": {"questions": [{"question": "どちらにしますか？"}]}})
        moved = scan.claude_project_dir(self.projects, "/ghq/work-reports/.claude/worktrees/readability") / "moved.jsonl"
        with moved.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asked) + "\n")
        # Claude Code keeps rewriting the metadata of the transcript the tab's session continued from.
        yesterday = time.time() - 86400
        os.utime(moved, (yesterday, yesterday))

        live = scan.running_claude_transcripts(["/w/report", "/w/shell"], [sessions], self.projects, processes,
                                               lambda pids: {pid: cwds[pid] for pid in pids if pid in cwds})
        rows = scan.build_inventory([worktree("/w/report"), worktree("/w/shell")],
                                    [terminal("/w/report", "claude"), terminal("/w/shell", "claude", "term_2")],
                                    self.projects, {}, "要判断", now=NOW, judge=judging({}), live_transcripts=live)

        self.assertEqual({path: [p.name for p in found] for path, found in live.items()},
                         {"/w/report": ["moved.jsonl", "tab.jsonl"], "/w/shell": ["wandered.jsonl"]})
        report, shell = rows
        self.assertEqual((report["claude"]["transcript"], report["class"]), (str(moved), "waiting"))
        self.assertEqual(shell["claude"]["final_text"], "Done.")

    def test_a_session_in_a_checkout_nested_in_another_worktree_belongs_to_the_tab_that_started_it(self) -> None:
        # 2026-11-04: Orca listed work-reports' main checkout, and the background Claude worked in a worktree under
        # its .claude/worktrees, so the scan gave that session to the main checkout instead of montly-report.
        root = Path(self.tmp.name)
        main = root / "ghq/work-reports"
        nested = main / ".claude/worktrees/readability"
        nested.mkdir(parents=True)
        (main / ".git").mkdir()
        (nested / ".git").write_text("gitdir: ../../../.git/worktrees/readability\n")
        sessions = root / "profile/sessions"
        sessions.mkdir(parents=True)
        (sessions / "88175.json").write_text(json.dumps({"pid": 88175, "sessionId": "moved", "cwd": str(nested)}))
        write_jsonl(scan.claude_project_dir(self.projects, str(nested)) / "moved.jsonl", [user("go")])
        processes = {88175: (1114, "claude bg-spare"), 1114: (1, "claude")}

        live = scan.running_claude_transcripts([str(main), "/w/report"], [sessions], self.projects, processes,
                                               lambda pids: {1114: "/w/report", 1: "/"})

        self.assertEqual({path: [p.name for p in found] for path, found in live.items()},
                         {"/w/report": ["moved.jsonl"]})

    def test_a_session_run_from_outside_the_worktree_belongs_to_the_worktree_of_its_orca_tab(self) -> None:
        # 2026-11-09: montly-report's tab resumed Claude in a checkout outside the worktree, with no sessions/<pid>.json,
        # so the row showed an 11/02 message on another matter from the worktree's own transcript, not that day's report.
        resumed = "92c0a1e6-6483-42b4-865d-2439ed459003"
        sessions = Path(self.tmp.name) / "profile/sessions"
        sessions.mkdir(parents=True)
        (sessions / "70.json").write_text(json.dumps({"pid": 70, "sessionId": "notes", "cwd": "/ghq/other"}))
        # The resumed session is written under its Orca profile's own projects, which need not be ~/.claude's.
        for root, cwd, session_id, said, at in (
                (self.projects, "/w/report", "old", "11/02 の別件", "2026-11-02T01:00:00Z"),
                (sessions.parent / "projects", "/ghq/work-reports", resumed, "今日の報告", "2026-11-09T01:00:00Z"),
                (self.projects, "/w/notes", "old", "前のメモ", "2026-11-02T01:00:00Z"),
                (self.projects, "/ghq/other", "notes", "今日のメモ", "2026-11-09T01:00:00Z")):
            write_jsonl(scan.claude_project_dir(root, cwd) / f"{session_id}.jsonl",
                        [user("go"), {**assistant(text(said)), "timestamp": at}])
        ps = subprocess.CompletedProcess([], 0, (f"   80 claude --dangerously-skip-permissions --resume {resumed}\n"
                                                "   70 claude\n   60 -zsh\n"), "")
        trees = [worktree("/w/report"), worktree("/w/notes")]

        with patch.object(scan.subprocess, "run", return_value=ps), \
                patch.object(scan.rm_check, "process_handles", return_value={81: "term_report", 70: "term_notes"}):
            # The resumed Claude's own environment hides its tab; only its shell shows it.
            live = scan.running_claude_transcripts(["/w/report", "/w/notes"], [sessions], self.projects,
                                                   {80: (81, "claude"), 81: (1, "-zsh"), 70: (60, "claude"),
                                                    60: (1, "-zsh")},
                                                   lambda pids: {pid: "/ghq/other" for pid in pids},
                                                   tabs={"term_report": "/w/report", "term_notes": "/w/notes"})
        rows = scan.build_inventory(trees, [terminal("/w/report", "claude", "term_report"),
                                            terminal("/w/notes", "claude", "term_notes")],
                                    self.projects, {}, "要判断", now=NOW, judge=judging({}), live_transcripts=live)

        self.assertEqual([row["claude"]["final_text"] for row in rows], ["今日の報告", "今日のメモ"])

    def test_a_running_claude_without_an_orca_tab_exposes_its_session_state(self) -> None:
        sessions = Path(self.tmp.name) / "profile/sessions"
        sessions.mkdir(parents=True)
        (sessions / "73.json").write_text(json.dumps({"pid": 73, "sessionId": "active", "cwd": "/w/orphan",
                                                     "status": "waiting", "waitingFor": "user"}))
        details: dict[str, list[dict]] = {}
        live = scan.running_claude_transcripts(["/w/orphan"], [sessions], self.projects, {73: (1, "claude")},
            lambda pids: {}, tab_of=lambda pids: {73: scan.TabProcess("term_missing", None)}, session_details=details)
        [row] = scan.build_inventory([worktree("/w/orphan")], [], self.projects, {}, "要判断",
                                     live_transcripts=live, live_sessions=details)
        saved = Path(self.tmp.name) / "orphan.json"
        saved.write_text(json.dumps([row]))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--rows"]), 0)
        for word in ("tabs 0", "pid=73", "ORCA_TERMINAL_HANDLE=term_missing", "status=waiting", "waitingFor=user"):
            self.assertIn(word, out.getvalue())
        self.assertEqual(row["claude_sessions"], [{"pid": 73, "ORCA_TERMINAL_HANDLE": "term_missing",
                                                    "status": "waiting", "waitingFor": "user"}])

    def test_a_restart_that_dropped_background_waits_is_a_reason(self) -> None:
        line = "3 background shell command tasks didn't finish before the previous session ended"
        tree: dict = {**worktree("/w/lost"), "agents": [done_agent("claude", "tab1:leaf1")]}
        term = in_pane(terminal(tree["path"], "claude", "term_lost"), "tab1:leaf1")

        row = scan.build_inventory([tree], [term], self.projects, {}, "要判断", now=NOW,
                                   screens={(None, "term_lost"): {"source": "screen", "tail": [line]}})[0]

        self.assertIn("background waits lost when the session was replaced (tab term_lost, agent state since "
                      "2027-01-15T08:00:00Z): " + line, row["reasons"])

    def test_a_stalled_worktree_offers_its_agents_but_not_a_busy_or_recent_shell(self) -> None:
        self.claude_says("/w/b", user("go"), assistant(text("PR #2 is open for review.")))
        result = self.closable(
            [worktree("/w/b", "done", pr={"number": 2, "state": "open"})],
            [terminal("/w/b", "claude", "term_lead"), terminal("/w/b", "codex", "term_helper"),
             terminal("/w/b", None, "term_server", preview="ready on http://localhost:3000",
                      quiet_minutes=90),
             terminal("/w/b", None, "term_clone", preview="Receiving objects:  42%", quiet_minutes=90),
             terminal("/w/b", None, "term_psql", preview="psql (16.4)\ntaihei=#", quiet_minutes=90),
             terminal("/w/b", None, "term_node", preview="Welcome to Node.js\n>", quiet_minutes=90),
             terminal("/w/b", None, "term_typed", preview=PROMPT, quiet_minutes=5),
             {**terminal("/w/b", None, "term_unknown", preview=PROMPT), "lastOutputAt": None},
             {key: value for key, value in terminal("/w/b", None, preview=PROMPT, quiet_minutes=90).items()
              if key != "handle"}],
            {"/w/b": [{"rollout": "r", "last_user": "implement", "last_role": "assistant",
                       "last_assistant": "Implemented.", "pending_requests": []}]})

        self.assertEqual(result["/w/b"], ["term_lead", "term_helper"])

    def test_an_agent_orca_reports_waiting_is_never_closable(self) -> None:
        self.claude_says("/w/p", user("go"), assistant(text("Running the migration now.")))
        result = self.inventory([worktree("/w/p", "waiting")], [terminal("/w/p", "claude", "term_blocked")])

        self.assertEqual([(r["class"], r["reasons"], r["closable"]) for r in result],
                         [("waiting", ["Orca agent waiting"], [])])

    def test_finished_work_with_only_closable_tabs_has_no_meaningful_tab(self) -> None:
        self.claude_says("/w/m", user("go"), assistant(text("Merged.")))
        self.claude_says("/w/n", user("go"), assistant(text("Done; board set to completed.")))
        rows = self.inventory(
            [worktree("/w/m", "done", pr={"number": 1, "state": "merged"}),
             worktree("/w/n", "done", status="completed")],
            [terminal("/w/m", "claude", "term_m"),
             terminal("/w/m", None, "term_setup", title="Setup", preview=PROMPT, quiet_minutes=90),
             terminal("/w/n", "claude", "term_n"),
             terminal("/w/n", None, "term_dev", preview="ready on http://localhost:5173", quiet_minutes=90)])

        self.assertEqual([(r["class"], r["reasons"], r["meaningful_tabs"]) for r in rows], [
            ("finished", ["PR merged"], []),
            ("finished", ["board status completed"], ["term_dev"])])

    def test_a_completed_board_never_hides_an_open_pr_or_a_working_agent(self) -> None:
        self.claude_says("/w/o", user("go"), assistant(text("PR #3 is open.")))
        self.claude_says("/w/k", user("go"), assistant(text("Working on it.")))
        rows = self.inventory(
            [worktree("/w/o", "done", status="completed", pr={"number": 3, "state": "open"}),
             worktree("/w/k", "working", status="completed")],
            [terminal("/w/o", "claude", "term_o"), terminal("/w/k", "claude", "term_k")])

        self.assertEqual([r["class"] for r in rows], ["stalled", "working"])

    def test_a_tab_without_a_handle_still_counts_as_meaningful(self) -> None:
        self.claude_says("/w/h", user("go"), assistant(text("Merged.")))
        rows = self.inventory(
            [worktree("/w/h", "done", pr={"number": 1, "state": "merged"})],
            [terminal("/w/h", "claude", "term_h"),
             {key: value for key, value in terminal("/w/h", None, title="dev server").items()
              if key != "handle"}])

        self.assertEqual(rows[0]["meaningful_tabs"], ["(no handle) dev server"])

    def test_orphaned_terminal_records_are_ignored(self) -> None:
        result = self.inventory(
            [worktree("/w/c", None)],
            [terminal("/w/c", "codex", "term_gone", orphaned=True)])

        self.assertEqual([(r["class"], r["terminals"], r["closable"]) for r in result],
                         [("stalled", [], [])])

    def test_unreadable_orphaned_shell_is_not_a_meaningful_tab(self) -> None:
        stale = {**terminal("/w/s", None, "term_stale", title=None, orphaned=True),
                 "connected": True, "lastOutputAt": None}
        visible = {**terminal("/w/s", None, "term_visible", title=None, preview="server ready", orphaned=True),
                   "connected": True, "lastOutputAt": None}
        agent = {**terminal("/w/s", "codex", "term_agent", title=None, orphaned=True),
                 "connected": True, "lastOutputAt": None}
        rows = self.inventory([worktree("/w/s", None)], [stale, visible, agent])

        self.assertEqual(rows[0]["meaningful_tabs"], ["term_visible", "term_agent"])

    def test_each_agent_orca_marks_done_is_a_done_card_with_the_tab_that_closes_it(self) -> None:
        rows = self.inventory(
            [{**worktree("/w/d"), "agents": [
                done_agent("codex", "tab1:leaf1", said="PR #5 is open for review.", asked="fix #4",
                           minutes_ago=120),
                {"agentType": "claude", "state": "working", "paneKey": "tab2:leaf2"}]}],
            [in_pane(terminal("/w/d", "codex", "term_codex"), "tab1:leaf1"),
             in_pane(terminal("/w/d", "claude", "term_claude"), "tab2:leaf2")])

        self.assertEqual(rows[0]["done_cards"], [
            {"agent": "codex", "handle": "term_codex", "done_at": "2027-01-15T06:00:00Z",
             "interrupted": False, "prompt": "fix #4", "last_message": "PR #5 is open for review.",
             "screen_tail": [], "draft": None, "draft_text": None, "composer_empty": None}])

    def test_a_done_card_whose_tab_sleeps_has_no_handle(self) -> None:
        rows = self.inventory(
            [{**worktree("/w/s"), "agents": [done_agent("claude", "tab9:leaf9", interrupted=True)]}], [])

        self.assertEqual([(c["agent"], c["handle"], c["interrupted"]) for c in rows[0]["done_cards"]],
                         [("claude", None, True)])

    def test_a_reopened_tab_closes_its_card_when_it_is_the_only_tab_of_that_agent(self) -> None:
        reopened = {"tabId": "pty:new", "leafId": "pty:new"}
        rows = self.inventory(
            [{**worktree("/w/r"), "agents": [done_agent("claude", "tab1:leaf1"),
                                             done_agent("codex", "tab2:leaf2")],
              "unread": True}],
            [{**terminal("/w/r", "claude", "term_reopened"), **reopened},
             {**terminal("/w/r", "codex", "term_one"), **reopened},
             {**terminal("/w/r", "codex", "term_other"), **reopened}])

        self.assertEqual([(c["agent"], c["handle"]) for c in rows[0]["done_cards"]],
                         [("claude", "term_reopened"), ("codex", None)])
        self.assertTrue(rows[0]["unread"])

    def test_a_main_checkout_is_read_only_while_it_hosts_an_agent(self) -> None:
        rows = self.inventory(
            [{**worktree("/repo/hosting", main=True), "agents": [done_agent("codex", "tab1:leaf1")]},
             worktree("/repo/shell-only", main=True), worktree("/w/t", "working")],
            [in_pane(terminal("/repo/hosting", "codex", "term_main"), "tab1:leaf1"),
             terminal("/repo/shell-only", None, "term_shell"), terminal("/w/t", "claude", "term_t")])

        self.assertEqual([(r["path"], r["main"], [c["handle"] for c in r["done_cards"]]) for r in rows],
                         [("/repo/hosting", True, ["term_main"]), ("/w/t", False, [])])

    def test_done_cards_carry_the_screen_end_and_whether_the_composer_holds_a_draft(self) -> None:
        screen = {"source": "screen", "draft": "あとで送る返事", "tail": [
            "⏺ PR #5 を出しました。", "", "✻ Baked for 3m 2s", "─" * 40, "❯", "─" * 40,
            "  -- INSERT -- ⏵⏵ bypass permissions on (shift+tab to cycle)"]}
        rows = scan.build_inventory(
            [{**worktree("/w/d"), "agents": [done_agent("claude", "tab1:leaf1"), done_agent("codex", "tab9:leaf9")]}],
            [in_pane(terminal("/w/d", "claude", "term_claude"), "tab1:leaf1")],
            self.projects, {}, "要判断", now=NOW, judge=judging({}), screens={(None, "term_claude"): screen})

        cards = rows[0]["done_cards"]
        self.assertEqual([(c["handle"], c["draft"], c["screen_tail"]) for c in cards], [
            ("term_claude", True, ["⏺ PR #5 を出しました。", "✻ Baked for 3m 2s", "❯",
                                   "-- INSERT -- ⏵⏵ bypass permissions on (shift+tab to cycle)"]),
            (None, None, [])])
        self.assertEqual([(c["draft_text"], c["composer_empty"]) for c in cards], [("あとで送る返事", True), (None, None)])
        screen["tail"][4] = "❯ スマホのメニューボタンも流れないように直して"
        self.assertFalse(scan.composer_empty(screen))
        screen["tail"][4:5] = ["❯", "  2 行目から書いた指示"]
        self.assertFalse(scan.composer_empty(screen))

    def test_a_hook_that_fails_for_a_missing_file_is_named_with_its_host_and_path(self) -> None:
        # Another Mac lost two hook scripts overnight: every Bash call and stop printed this, and nobody noticed.
        remote: dict = {**worktree("/remote/lane-241", "working"), "_environment": "macbook-pro-16"}
        term = {**terminal("/remote/lane-241", "claude", "term_r", preview="\n".join([
            "⏺ CI is all green.", "  ⎿  PreToolUse:Bash hook error",
            "  ⎿  Failed with non-blocking status code: /bin/sh: /Users/me/.claude/hooks/git-hook-guard.sh: "
            "No such file or directory"])), "_environment": "macbook-pro-16"}
        screen = {"source": "screen", "tail": [
            "⏺ Ran 7 stop hooks", "  ⎿  Stop hook error: Failed with non-blocking status code: /bin/sh: "
            "/Users/me/.claude/hooks/ask-question-guard.sh: No such file or directory", "❯"]}
        # An agent that only talks about a missing file is not a failing hook.
        local = worktree("/w/app", "working")
        local_term = terminal("/w/app", "claude", "term_l", preview="⏺ ls: /tmp/x: No such file or directory")
        rows = scan.build_inventory([remote, local], [term, local_term], self.projects, {}, "要判断", now=NOW,
                                    screens={("macbook-pro-16", "term_r"): screen})

        by_path = {row["path"]: row for row in rows}
        self.assertEqual(by_path["/remote/lane-241"]["hook_errors"], [
            {"host": "macbook-pro-16", "path": "/Users/me/.claude/hooks/git-hook-guard.sh", "tab": "term_r"},
            {"host": "macbook-pro-16", "path": "/Users/me/.claude/hooks/ask-question-guard.sh", "tab": "term_r"}])
        self.assertIn("hook error on macbook-pro-16: /Users/me/.claude/hooks/git-hook-guard.sh: No such file or directory",
                      by_path["/remote/lane-241"]["reasons"])
        self.assertEqual(by_path["/w/app"]["hook_errors"], [])

    def test_a_remote_tab_without_agent_details_is_read_from_its_screen(self) -> None:
        # MBP16's terminal list gave test-338 no agent, title, or preview while a Claude waited after worker_done.
        remote: dict = {**worktree("/remote/test-338"), "_environment": "macbook-pro-16"}
        term = {**terminal("/remote/test-338", None, "term_r", title=None), "_environment": "macbook-pro-16",
                "orphaned": True, "connected": True, "lastOutputAt": None}
        screen = {"source": "screen", "tail": [
            "⏺ PR #345 のレビュー指摘を直して push し、", "  coordinator に worker_done を送りました。", "",
            "⏺ Bash(git push origin HEAD)", "  ⎿  Everything up-to-date", "",
            "⏺ serena - find_symbol (MCP)(name_path: \"main\")", "  ⎿  {\"result\": []}", "",
            "⏺ serena - search_for_pattern (MCP)(substring_pattern: \"worker_done\",", "  relative_path: \"e2e\")", "",
            "⏺ Searched for 2 patterns, read 3 files (ctrl+o to expand)", "",
            "✻ Baked for 5h 30m 24s · done 4:33 PM", "─" * 40, "❯", "─" * 40,
            "  -- INSERT -- ⏵⏵ bypass permissions on (shift+tab to cycle)"]}
        # test-338's reply began above the visible screen: the lines before its turn-end line stand in.
        # The same list names the agent of that tab on some reads, and the reply is still read.
        scrolled = {**worktree("/remote/test-338b"), "_environment": "macbook-pro-16"}
        scrolled_term = {**terminal("/remote/test-338b", "claude", "term_s"), "_environment": "macbook-pro-16"}
        scrolled_screen = {"source": "screen", "tail": [
            "    の計算値が原因です。", "  DB は残してあります。", "✻ Baked for 5h 30m 24s · done 4:33 PM", "❯"]}
        # A working agent Orca keeps no message of was "unstarted" for want of a transcript.
        working = {**worktree("/remote/masters-ui-125", "working"), "_environment": "macbook-pro-16"}
        working_term = {**terminal("/remote/masters-ui-125", "claude", "term_w"), "_environment": "macbook-pro-16"}
        said = []
        rows = scan.build_inventory([remote, scrolled, working], [term, scrolled_term, working_term], self.projects,
                                    {}, "要判断", now=NOW, judge=lambda message: said.append(message) or verdict(),
                                    screens={("macbook-pro-16", "term_r"): screen,
                                             ("macbook-pro-16", "term_s"): scrolled_screen,
                                             ("macbook-pro-16", "term_w"): screen})

        self.assertEqual(said, ["PR #345 のレビュー指摘を直して push し、\ncoordinator に worker_done を送りました。",
                                "の計算値が原因です。\nDB は残してあります。"])
        self.assertEqual(rows[0]["terminals"][0]["agent"], "claude")
        self.assertEqual((rows[0]["class"], rows[0]["reasons"]), ("stalled", ["final message says done (Jev 0.90)"]))
        self.assertEqual(rows[2]["class"], "working")

    def test_a_remote_codex_tab_is_read_from_its_reply_bullet_or_the_recap_under_its_turn_end(self) -> None:
        # 2026-11-04: MBP16's and the Mac mini's Codex tabs came back with an empty final message and question.
        def remote(name: str, handle: str) -> tuple[dict, dict]:
            return ({**worktree("/remote/" + name, agent="codex"), "_environment": "macbook-pro-16"},
                    {**terminal("/remote/" + name, None, handle, title=None), "_environment": "macbook-pro-16"})
        sentinel, sentinel_term = remote("sentinel", "term_m")
        textarea, textarea_term = remote("textarea", "term_t")
        # masters-741's setup shell: devenv's own bullets are no agent.
        setup, setup_term = remote("setup", "term_s")
        devenv = {"source": "screen", "tail": [
            "• Running devenv:enterShell", "✓ Running devenv:enterShell in 24.4ms", "• Running devenv:enterTest",
            "✓ Running devenv:enterTest in 458ns (no command)", "[bootstrap-worktree] done", "➜"]}
        replied = {"source": "screen", "tail": [
            "• Ran ps -axo pid,ppid,etime,args | rg 'find /Users/shiftone/ghq'",
            "  └ 44926 44924       01:23 /usr/bin/find /Users/shiftone/ghq -maxdepth 5 -iname *plinth*",
            "• Orca CLI本体で試しましたが、実画面取得は2回とも permission_denied でした。結果が食い違っていま",
            "  す。", "  次に必要なのは、get-app-state が成功する操作経路です。待機します。",
            "  Worked for 4m 28s • 11:43", "› Ask Codex to do anything", "  ? for shortcuts"]}
        recapped = {"source": "screen", "tail": [
            "  main の取り込み内容は未コミットで保持しました。", "• Hook failed", "  └ hook timed out after 10s",
            "  Worked for 22m 5s • 14:43", "  ↳ Recap: Textarea の全幅化は実装・push 済みで、PR #239 を作成し",
            "           ました。PR は未 merge のままです。", "› Ask Codex to do anything"]}
        said = []

        rows = scan.build_inventory([sentinel, textarea, setup], [sentinel_term, textarea_term, setup_term],
                                    self.projects, {}, "要判断", now=NOW,
                                    judge=lambda message: said.append(message) or verdict(),
                                    screens={("macbook-pro-16", "term_m"): replied,
                                             ("macbook-pro-16", "term_t"): recapped,
                                             ("macbook-pro-16", "term_s"): devenv})

        self.assertEqual(said, ["Orca CLI本体で試しましたが、実画面取得は2回とも permission_denied でした。結果が食い違っていま\n"
                                "す。\n次に必要なのは、get-app-state が成功する操作経路です。待機します。",
                                "Textarea の全幅化は実装・push 済みで、PR #239 を作成しました。PR は未 merge のままです。"])
        self.assertEqual([row["terminals"][0]["agent"] for row in rows], ["codex", "codex", None])
        # A reply is told from a command by its └ result or its line counts, not by its first word.
        self.assertEqual(scan.screen_message({"tail": [
            "• Added regression tests and pushed the fix. Shall I merge?", "• Edited app.py (+12 -3)", "    12 +import json",
            "  Worked for 1m 2s • 10:00"]}), "Added regression tests and pushed the fix. Shall I merge?")
        self.assertNotIn("orca_message", rows[2])

    def test_a_tab_orca_detached_but_still_runs_is_read(self) -> None:
        detached = {**terminal("/w/u", "claude", "term_detached"), "orphaned": True, "connected": True,
                    "tabId": "pty:u", "leafId": "pty:u"}
        rows = self.inventory([{**worktree("/w/u"), "agents": [done_agent("claude", "tab1:leaf1")]}], [detached])

        self.assertEqual(([t["handle"] for t in rows[0]["terminals"]], rows[0]["done_cards"][0]["handle"]),
                         (["term_detached"], "term_detached"))

    def test_detached_tabs_close_the_cards_of_their_recorded_panes(self) -> None:
        def detached(handle: str, pane: str) -> dict[str, object]:
            return {**terminal("/w/u", "claude", handle), "orphaned": True, "connected": True,
                    "tabId": f"pty:{handle}", "leafId": f"pty:{handle}", "recordedPaneKey": pane}
        rows = self.inventory([{**worktree("/w/u"), "agents": [done_agent("claude", "tab1:leaf1"),
                                                               done_agent("claude", "tab2:leaf2")]}],
                              [detached("term_one", "tab1:leaf1"), detached("term_two", "tab2:leaf2")])

        self.assertEqual([c["handle"] for c in rows[0]["done_cards"]], ["term_one", "term_two"])

    def test_waiting_comes_from_transcripts_and_the_decision_marker(self) -> None:
        self.claude_says("/w/a", user("go"), assistant(
            {"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": {"questions": []}}))
        self.claude_says("/w/b", user("go"), assistant(text("PR is up.\nShall I merge it?")))
        self.claude_says("/w/c", user("go"), assistant(text("Rebased on main and pushed.")))
        rows = self.inventory(
            [worktree("/w/a", "working"), worktree("/w/b", "done"),
             worktree("/w/c", "done", comment="要判断: which base?"),
             worktree("/w/old", "working", comment="要判断: which base?"),
             worktree("/w/main", main=True)],
            [terminal("/w/a", "claude"), terminal("/w/b", "claude"), terminal("/w/c", "claude"),
             terminal("/w/old", "claude")],
            judge=judging({"PR is up.\nShall I merge it?": verdict(0.9)}))
        result = {r["path"]: (r["class"], r["reasons"]) for r in rows}

        self.assertEqual(result["/w/a"], ("waiting", ["AskUserQuestion pending"]))
        self.assertEqual(result["/w/b"], ("waiting", ["final message asks (Jev 0.90)"]))
        self.assertEqual(result["/w/c"], ("waiting", ["decision marker in comment"]))
        # A waiting row asks its marked comment before its final message, even one that asks too.
        self.assertEqual(scan.row_view(next(r for r in rows if r["path"] == "/w/c"))["question"], "which base?")
        marked = scan.build_inventory(
            [worktree("/w/b", "done", comment="PR #3 is up. 要判断: which base?")], [terminal("/w/b", "claude")],
            self.projects, {}, "要判断", now=NOW, judge=judging({"PR is up.\nShall I merge it?": verdict(0.9)}))[0]
        self.assertEqual(scan.row_view(marked)["question"], "which base?")
        # An agent in its turn works past the decision it marked.
        self.assertEqual(result["/w/old"], ("working", ["old decision marker in comment: an agent is in its turn"]))
        self.assertNotIn("/w/main", result)

    def test_a_final_message_jev_judges_as_waiting_on_the_user_is_waiting(self) -> None:
        asks = "devel の通しは終わりました。\n調べるか issue にするかは、ご判断ください。"
        done = "確認しました。\n残りはありません。"
        self.claude_says("/w/j", user("go"), assistant(text(asks)))
        self.claude_says("/w/s", user("go"), assistant(text(done)))
        result = self.classes(
            [worktree("/w/j", "done", pr={"number": 1, "state": "merged"}),
             worktree("/w/s", "done", pr={"number": 2, "state": "merged"})],
            [terminal("/w/j", "claude", "term_j"), terminal("/w/s", "claude", "term_s")],
            judge=judging({asks: verdict(0.79), done: verdict(0.29)}))

        self.assertEqual(result["/w/j"], ("waiting", ["final message asks (Jev 0.79)"]))
        self.assertEqual(result["/w/s"], ("finished", ["PR merged"]))

    def test_a_final_message_jev_is_unsure_about_is_read_before_anything_closes(self) -> None:
        maybe = "PR は merge しました。\n残っている判断: 表記をどちらにそろえるかは別に決める必要があります。"
        leaning = "Once the other agent reports, I'll decide how to handle these."
        terse = "commit・push はしていません。"
        for path, message in (("/w/x", maybe), ("/w/y", leaning), ("/w/z", terse)):
            self.claude_says(path, user("go"), assistant(text(message)))
        result = self.inventory(
            [worktree("/w/x", "done", pr={"number": 1, "state": "merged"}), worktree("/w/y", "done"),
             worktree("/w/z", "done", pr={"number": 2, "state": "merged"})],
            [terminal("/w/x", "claude", "term_x"), terminal("/w/y", "claude", "term_y"),
             terminal("/w/z", "claude", "term_z")],
            judge=judging({maybe: verdict(0.3), leaning: verdict(0.69, "in_progress"),
                           terse: verdict(0.1, "done", 0.59)}))

        self.assertEqual([(r["class"], r["reasons"], r["closable"]) for r in result], [
            ("unsure", ["Jev unsure whether it asks (0.30): read the final message"], []),
            ("unsure", ["Jev unsure whether it asks (0.69): read the final message"], []),
            ("unsure", ["Jev unsure of the state (done 0.59): read the final message"], [])])

    def test_the_state_jev_reads_decides_what_an_idle_worktree_needs(self) -> None:
        says = {"/w/p": ("Codex に修正を渡しました。終わったら確かめます。", "in_progress", "open"),
                "/w/m": ("merge しました。#850 に取りかかります。", "in_progress", "merged"),
                "/w/r": ("レビューは hdknr に依頼しました。", "blocked_on_others", "open"),
                "/w/d": ("実装とテストが終わりました。", "done", "open"),
                "/w/f": ("merge して、残りはありません。", "done", "merged")}
        for path, (message, _, _) in says.items():
            self.claude_says(path, user("go"), assistant(text(message)))
        result = self.classes(
            [worktree(path, "done", pr={"number": 1, "state": pr}) for path, (_, _, pr) in says.items()],
            [terminal(path, "claude", "term_" + path[-1]) for path in says],
            judge=judging({message: verdict(0.05, status, 0.93) for message, status, _ in says.values()}))

        self.assertEqual(result, {
            "/w/p": ("stalled", ["final message says work continues, yet no agent runs (Jev 0.93)"]),
            "/w/m": ("stalled", ["final message says work continues, yet no agent runs (Jev 0.93)"]),
            "/w/r": ("stalled", ["final message waits on someone else (Jev 0.93)"]),
            "/w/d": ("stalled", ["final message says done (Jev 0.93)"]),
            "/w/f": ("finished", ["PR merged"])})

    def test_an_agent_whose_message_says_work_continues_keeps_its_tab(self) -> None:
        waiting_on_job = "push（pre-push の full pytest）の完了を待っています。"
        waiting_on_review = "レビューは hdknr に依頼しました。"
        self.claude_says("/w/j", user("go"), assistant(text(waiting_on_job)))
        self.claude_says("/w/r", user("go"), assistant(text(waiting_on_review)))
        rows = self.inventory([worktree("/w/j", "done", pr={"number": 1, "state": "open"}),
                               worktree("/w/r", "done", pr={"number": 2, "state": "open"})],
                              [terminal("/w/j", "claude", "term_j"), terminal("/w/r", "claude", "term_r")],
                              judge=judging({waiting_on_job: verdict(0.0, "in_progress", 0.96),
                                             waiting_on_review: verdict(0.0, "blocked_on_others", 0.96)}))

        self.assertEqual([(r["class"], r["closable"], r["meaningful_tabs"]) for r in rows],
                         [("stalled", [], ["term_j"]), ("stalled", ["term_r"], [])])

    def test_only_the_latest_final_message_is_judged(self) -> None:
        lead = "Codex の修正を確かめました。テストは通っています。"
        helper = "commit・push はしていません。"
        self.claude_says("/w/l", user("go"), {**assistant(text(lead)), "timestamp": "2026-10-01T02:00:00Z"})
        self.claude_says("/w/h", user("go"), {**assistant(text("Codex に渡しました。")),
                                              "timestamp": "2026-10-01T01:00:00Z"})
        codex = {path: [{"rollout": "r", "last_user": "fix", "last_role": "assistant", "last_assistant": helper,
                         "last_assistant_at": "2026-10-01T01:30:00Z", "pending_requests": []}]
                 for path in ("/w/l", "/w/h")}
        judged = []

        def judge(message: str) -> dict:
            judged.append(message)
            return verdict(0.1, "blocked_on_others")

        rows = self.inventory([worktree("/w/l", "done"), worktree("/w/h", "done")],
                              [terminal("/w/l", "claude"), terminal("/w/h", "claude")], codex, judge)

        self.assertEqual(judged, [lead, helper])
        self.assertEqual([r["jev"] for r in rows], [
            {"source": "claude", **verdict(0.1, "blocked_on_others")},
            {"source": "codex", **verdict(0.1, "blocked_on_others")}])

    def test_claude_speaks_for_the_worktree_when_either_time_is_missing(self) -> None:
        self.claude_says("/w/t", user("go"), assistant(text("Done.")))
        codex = {"/w/t": [{"rollout": "r", "last_user": "fix", "last_role": "assistant", "last_assistant": "ok",
                           "last_assistant_at": "2026-10-01T01:30:00Z", "pending_requests": []}]}

        rows = self.inventory([worktree("/w/t", "done")], [terminal("/w/t", "claude")], codex)

        self.assertEqual(rows[0]["jev"]["source"], "claude")

    def test_an_unjudged_final_message_is_read_before_anything_closes(self) -> None:
        self.claude_says("/w/u", user("go"), assistant(text("Merged.")))
        self.claude_says("/w/v", user("go"), assistant(text("Running the suite.")))
        judged = []

        def unavailable(message: str) -> None:
            judged.append(message)
            return None

        result = self.inventory(
            [worktree("/w/u", "done", pr={"number": 1, "state": "merged"}), worktree("/w/v", "working")],
            [terminal("/w/u", "claude", "term_u"), terminal("/w/v", "claude", "term_v")], judge=unavailable)

        self.assertEqual([(r["class"], r["reasons"], r["closable"]) for r in result], [
            ("unsure", ["Jev could not judge: read the final message"], []), ("working", [], [])])
        self.assertEqual(judged, ["Merged."])

    def test_a_question_is_found_while_only_a_background_job_keeps_the_agent_working(self) -> None:
        asks = "PR #220 を出しました。merge には許可が必要です。進めてよいですか。"
        self.claude_says("/w/bg", user("go"), assistant(text(asks)))

        result = self.classes([worktree("/w/bg", "working", turn="done")], [terminal("/w/bg", "claude")],
                              judge=judging({asks: verdict(0.9, "blocked_on_others")}))

        self.assertEqual(result["/w/bg"], ("waiting", ["final message asks (Jev 0.90)"]))

    def test_a_background_job_keeps_the_worktree_working_whatever_the_final_message_says(self) -> None:
        self.claude_says("/w/watch", user("go"), assistant(text("Merged. Watching the 12:00 import on stage.")))

        result = self.inventory(
            [worktree("/w/watch", "working", turn="done", pr={"number": 1, "state": "merged"}, status="completed")],
            [terminal("/w/watch", "claude", "term_w")])

        self.assertEqual([(r["class"], r["reasons"], r["closable"]) for r in result],
                         [("working", ["a background job runs after the agent's turn"], [])])

    def processes(self, table: dict[int, tuple[int, str]], now: float, clocks: dict[int, tuple[str, str]], tcp: str | Exception = "", path: str = "/w/p", elsewhere: tuple[int, ...] = ()) -> dict:
        """worktree_processes of `path` over a process table whose processes sit in /w/p, or /tmp for `elsewhere`;
        `clocks` maps a pid to ps's elapsed and CPU columns, and `tcp` is lsof's output, or the error it fails
        with."""
        ps = "".join(f"{pid} {elapsed} {cpu}\n" for pid, (elapsed, cpu) in clocks.items())

        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "lsof" and isinstance(tcp, Exception):
                raise tcp
            output = ps if args[0] == "ps" else tcp
            assert isinstance(output, str)
            return subprocess.CompletedProcess(args, 0, output, "")

        with patch.object(scan.rm_check, "process_table", return_value=table), \
                patch.object(scan.rm_check, "process_cwds",
                             return_value={pid: "/tmp" if pid in elsewhere else "/w/p" for pid in table}), \
                patch.object(scan.rm_check, "output_readers", return_value=set()), \
                patch.object(scan.subprocess, "run", side_effect=run):
            return scan.worktree_processes([path], Path(self.tmp.name) / "cpu.json", now)

    def test_a_wait_loop_left_running_keeps_a_worktree_without_an_agent_working(self) -> None:
        table = {9101: (1, "bash /tmp/wait-merge.sh"), 9102: (9101, "sleep 60"),
                 9103: (1, "node /Users/me/.claude/plugins/cache/codex/scripts/broker.mjs serve"),
                 9104: (1, "pnpm dev"), 9105: (9104, "sh -c while read -r line; do echo \"$line\"; done")}
        found = self.processes(table, NOW, {9101: ("01:00:00", "0:00.02"), 9102: ("00:20", "0:00.00")})

        [row] = self.inventory([worktree("/w/p")], [], processes=found)

        self.assertEqual((row["class"], [process["pid"] for process in row["processes"]]), ("working", [9101, 9102]))
        self.assertEqual(row["reasons"], [
            "a background job runs in the worktree: PID 9101 bash /tmp/wait-merge.sh (running 1h00m, CPU 0.02s)",
            "a background job runs in the worktree: PID 9102 sleep 60 (running 0h00m, CPU 0.0s)"])

        # A loop whose check hung for an hour no longer waits.
        hung = {9101: (1, "bash /tmp/wait-merge.sh"), 9106: (9101, "gh pr checks 5 --watch")}
        self.processes(hung, NOW - 3600, {9101: ("01:00:00", "0:00.02"), 9106: ("00:50:00", "0:00.30")})
        found = self.processes(hung, NOW, {9101: ("02:00:00", "0:00.02"), 9106: ("01:50:00", "0:00.30")})
        [row] = self.inventory([worktree("/w/p")], [], processes=found)
        self.assertEqual((row["class"], row["reasons"]), ("stalled", [scan.NO_SESSION_REASON]))

    def test_background_work_whose_cpu_time_stays_still_for_an_hour_or_whose_connection_closed_is_stalled(self) -> None:
        self.claude_says("/w/p", user("go"), assistant(text("Watching the run.")))
        table = {9201: (1, "-zsh"), 9202: (9201, "claude"), 9203: (9202, "node mcp-server.js"),
                 9204: (9202, "/bin/zsh -c source snapshot.sh && eval 'gh run watch 7'"), 9205: (9204, "gh run watch 7")}
        clocks = {9204: ("02:00:00", "0:00.01"), 9205: ("02:00:00", "0:00.40")}

        def classify(found: dict, now: float = NOW) -> tuple[str, list[str]]:
            [row] = self.inventory([worktree("/w/p", "working", turn="done")], [terminal("/w/p", "claude")],
                                   processes=found, now=now)
            return row["class"], row["reasons"]

        # The scan's clock and ps's elapsed time put one start on either side of a minute.
        self.assertEqual(classify(self.processes(table, NOW + 29.6, clocks), NOW + 29.6), ("working", [
            "a background job runs after the agent's turn: PID 9204 /bin/zsh -c source snapshot.sh && eval 'gh run "
            "watch 7' (running 2h00m, CPU 0.01s); PID 9205 gh run watch 7 (running 2h00m, CPU 0.4s)"]))
        # A scan of another worktree keeps the history, and a socket read that times out keeps the CPU times.
        self.processes(table, NOW + 1800, {}, path="/w/q")
        an_hour_later = self.processes(table, NOW + 3630.1,
                                       {pid: ("03:00:00", cpu) for pid, (_, cpu) in clocks.items()},
                                       tcp=subprocess.TimeoutExpired("lsof", 30))
        self.assertEqual(classify(an_hour_later, NOW + 3630.1)[0], "stalled")
        self.assertTrue(classify(an_hour_later, NOW + 3630.1)[1][0].startswith("裏の作業が止まっている: PID 9204"))

        (Path(self.tmp.name) / "cpu.json").unlink()
        closed = self.processes(table, NOW, clocks, tcp="p9205\nf5\nn10.0.0.2:52000->140.82.112.6:443\nTST=CLOSE_WAIT\n")
        self.assertEqual(classify(closed)[0], "stalled")
        self.assertIn("TCP closed: 10.0.0.2:52000->140.82.112.6:443", classify(closed)[1][0])

        # A script waiting on a sleep it started in another directory still waits.
        (Path(self.tmp.name) / "cpu.json").unlink()
        script = {9201: (1, "-zsh"), 9202: (9201, "claude"), 9206: (9202, "bash wait.sh"),
                  9207: (9206, "sleep 7200")}
        for now, elapsed in ((NOW, "01:00:00"), (NOW + 3600, "02:00:00")):
            found = self.processes(script, now, {9206: (elapsed, "0:00.01"), 9207: ("00:30:00", "0:00.00")},
                                   elsewhere=(9207,))
        self.assertEqual(classify(found, NOW + 3600)[0], "working")

    def test_an_unsure_question_behind_a_background_job_is_read(self) -> None:
        maybe = "PR は出しました。表記をどちらにそろえるかは別に決める必要があります。"
        self.claude_says("/w/m", user("go"), assistant(text(maybe)))
        self.claude_says("/w/n", user("go"), assistant(text("Watching CI.")))

        result = self.classes(
            [worktree("/w/m", "working", turn="done"), worktree("/w/n", "working", turn="done")],
            [terminal("/w/m", "claude", "term_m"), terminal("/w/n", "claude", "term_n")],
            judge=judging({maybe: verdict(0.5, "in_progress"), "Watching CI.": verdict(0.1, "in_progress", 0.4)}))

        self.assertEqual(result["/w/m"], ("unsure", ["Jev unsure whether it asks (0.50): read the final message"]))
        self.assertEqual(result["/w/n"], ("working", ["a background job runs after the agent's turn"]))

    def test_agents_sharing_a_worktree_are_read_together(self) -> None:
        asks = "merge してよいですか。"
        for path in ("/w/turn", "/w/wait", "/w/pair"):
            self.claude_says(path, user("go"), assistant(text(asks)))
        judged = []

        def judge(message: str) -> dict:
            judged.append(message)
            return verdict(0.1, "in_progress")

        result = {r["path"]: (r["class"], r["reasons"], r["closable"]) for r in self.inventory(
            [with_agents(worktree("/w/turn"), ("claude", "working", "done"), ("claude", "working", "working")),
             with_agents(worktree("/w/wait"), ("claude", "working", "done"), ("claude", "waiting", None)),
             with_agents(worktree("/w/pair", pr={"number": 1, "state": "merged"}),
                         ("claude", "done", "done"), ("codex", "working", "done"))],
            [terminal("/w/turn", "claude", "term_t"), terminal("/w/wait", "claude", "term_w"),
             terminal("/w/pair", "claude", "term_p")], judge=judge)}

        self.assertEqual(result["/w/turn"], ("working", [], []))
        self.assertEqual(result["/w/wait"], ("waiting", ["Orca agent waiting"], []))
        self.assertEqual(result["/w/pair"], ("working", ["a background job runs after the agent's turn"], []))
        self.assertEqual(judged, [asks, asks])

    def test_an_agent_without_a_readable_main_state_counts_as_in_its_turn(self) -> None:
        self.claude_says("/w/odd", user("go"), assistant(text("merge してよいですか。")))
        odd = worktree("/w/odd", "working")
        for main_agent in (None, {"state": None}, "done", ["done"], True):
            with self.subTest(main_agent=main_agent):
                row = {**odd, "agents": [{**odd["agents"][0], "mainAgent": main_agent}]}

                result = self.classes([row], [terminal("/w/odd", "claude")],
                                      judge=judging({"merge してよいですか。": verdict(0.9)}))

                self.assertEqual(result["/w/odd"], ("working", []))

    def test_a_codex_final_message_that_asks_is_waiting(self) -> None:
        asks = "PR #1 is green.\nPR #1 をマージしてよいですか？\nAGENTS.md で確認が必要と定めているため、ここで確認しています。"
        codex = {"/w/q": [{"rollout": "r", "last_user": "ship it", "last_role": "assistant",
                           "last_assistant": asks, "pending_requests": []}],
                 "/w/r": [{"rollout": "r", "last_user": "マージしてよいですか？", "last_role": "user",
                           "last_assistant": "Shall I merge?", "pending_requests": []}]}
        result = self.classes(
            [worktree("/w/q", "done", agent="codex", pr={"number": 1, "state": "merged"}),
             worktree("/w/r", "working", agent="codex")],
            [terminal("/w/q", "codex"), terminal("/w/r", "codex")], codex, judge=judging({asks: verdict(0.8)}))

        self.assertEqual(result["/w/q"], ("waiting", ["Codex final message asks (Jev 0.80)"]))
        self.assertEqual(result["/w/r"], ("working", []))

    def test_codex_requests_unstarted_agents_and_merged_work(self) -> None:
        pending = {"asked_at": "t", "questions": [], "answered_in_chat_later": False}
        stale = {"asked_at": "t", "questions": [], "answered_in_chat_later": True}
        codex = {
            "/w/d": [{"rollout": "r1", "last_user": "x", "last_assistant": "y", "pending_requests": [pending]}],
            "/w/e": [{"rollout": "r2", "last_user": "x", "last_assistant": "y", "pending_requests": [stale]}],
        }
        self.claude_says("/w/g", user("go"), assistant(text("Merged and deployed.")))
        self.claude_says("/w/h", user("go"), assistant(text("PR is open for review.")))
        result = self.classes(
            [worktree("/w/d", "working", agent="codex"), worktree("/w/e", "working", agent="codex"),
             worktree("/w/f", None), worktree("/w/g", "done", pr={"number": 1, "state": "merged"}),
             worktree("/w/h", "done", pr={"number": 2, "state": "open"})],
            [terminal("/w/d", "codex"), terminal("/w/e", "codex"), terminal("/w/f", "codex"),
             terminal("/w/g", "claude"), terminal("/w/h", "claude")],
            codex, judging({"PR is open for review.": verdict(0.1, "blocked_on_others")}))

        self.assertEqual(result["/w/d"], ("waiting", ["Codex request pending"]))
        self.assertEqual(result["/w/e"], ("working", []))
        self.assertEqual(result["/w/f"], ("unstarted", ["codex terminal without a recent transcript"]))
        self.assertEqual(result["/w/g"], ("finished", ["PR merged"]))
        self.assertEqual(result["/w/h"], ("stalled", ["final message waits on someone else (Jev 0.90)"]))

    def test_a_turn_cut_off_by_an_api_error_is_stalled_even_behind_a_background_job(self) -> None:
        # 2026-10-06 the Mac slept mid-response; a CI monitor kept Orca at working and the lead sat for 16 hours.
        cut = "API Error: Your computer went to sleep mid-response. The response above may be incomplete."
        self.claude_says("/w/cut", user("go"), assistant(text("critic の判定待ちです。")),
                         {**assistant(text(cut)), "isApiErrorMessage": True})
        judged = []

        def judge(message: str) -> dict:
            judged.append(message)
            return verdict(0.1, "done" if message == "Merged." else "in_progress", 1.0)

        self.claude_says("/w/merged", user("go"), assistant(text("merge しました。")),
                         {**assistant(text(cut)), "isApiErrorMessage": True})
        self.claude_says("/w/resumed", user("go"), {**assistant(text(cut)), "isApiErrorMessage": True},
                         assistant(text("Merged.")))

        result = {r["path"]: (r["class"], r["reasons"], r["closable"]) for r in self.inventory(
            [worktree("/w/cut", "working", turn="done"),
             worktree("/w/merged", "done", pr={"number": 1, "state": "merged"}),
             worktree("/w/resumed", "done", pr={"number": 2, "state": "merged"})],
            [terminal("/w/cut", "claude", "term_c"), terminal("/w/merged", "claude", "term_m"),
             terminal("/w/resumed", "claude", "term_r")], judge=judge)}

        # A cut-off turn is woken, never offered for closing, even after its PR merged.
        self.assertEqual(result["/w/cut"], ("stalled", ["turn ended with an API error: " + cut], []))
        self.assertEqual(result["/w/merged"], ("stalled", ["turn ended with an API error: " + cut], []))
        self.assertEqual(result["/w/resumed"][0], "finished")
        self.assertEqual(judged, ["Merged."])

    def test_a_turn_the_api_ended_says_whether_to_wake_it_wait_for_the_reset_or_ask_a_person(self) -> None:
        # 2026-11-04: an EPM lane hit its weekly limit at 15:26 while Orca still showed it working; a DNS outage and
        # an expired login stopped other lanes, and every one was woken the same way.
        def ended(path: str, line: str, kind: str) -> None:
            self.claude_says(path, user("go"), {**assistant(text(line)), "isApiErrorMessage": True, "error": kind})
        ended("/w/limit", "You've hit your weekly limit · resets Nov 7 at 8pm (Asia/Tokyo)", "rate_limit")
        ended("/w/login", "Login expired · Please run /login", "authentication_failed")
        ended("/w/dns", "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)",
              "server_error")
        ended("/w/flagged", "API Error: Opus 5.5's safeguards flagged this session", "invalid_request")

        rows = {r["path"]: r for r in self.inventory(
            [worktree("/w/limit", "working"), worktree("/w/login", "done"), worktree("/w/dns", "done"),
             worktree("/w/flagged", "done")],
            [terminal("/w/limit", "claude", "term_l"), terminal("/w/login", "claude", "term_g"),
             terminal("/w/dns", "claude", "term_d"), terminal("/w/flagged", "claude", "term_f")])}

        self.assertEqual({path: (row["class"], row["stop"]["kind"], row["stop"]["action"], row["stop"]["resets"])
                          for path, row in rows.items()}, {
            "/w/limit": ("stalled", "usage_limit", "wait", "Nov 7 at 8pm (Asia/Tokyo)"),
            "/w/login": ("stalled", "login", "human", None),
            "/w/dns": ("stalled", "connection", "wake", None),
            "/w/flagged": ("stalled", "other", "human", None)})
        self.assertEqual(rows["/w/limit"]["reasons"], ["usage limit; resets Nov 7 at 8pm (Asia/Tokyo)"])

        # The lane went on in Codex after Claude's limit: Claude's failure no longer stops it.
        ended("/w/handed", "You've hit your weekly limit · resets Nov 7 at 8pm (Asia/Tokyo)", "rate_limit")
        codex = {"/w/handed": [{"rollout": "r", "last_user": "continue", "last_role": "user", "last_assistant": None,
                                "last_assistant_at": None, "pending_requests": []}]}
        [handed] = self.inventory([worktree("/w/handed", "working", agent="codex")],
                                  [terminal("/w/handed", "codex", "term_x")], codex)
        self.assertEqual((handed["class"], handed["stop"]), ("working", None))
        # And back: Codex failed earlier, then Claude, running in its place, failed too.
        ended("/w/back", "API Error: Can't reach the API server (ENOTFOUND)", "server_error")
        failed = {"/w/back": [{**codex["/w/handed"][0], "api_error": "■ Selected model is at capacity"}]}
        [back] = self.inventory([worktree("/w/back", "working")], [terminal("/w/back", "claude", "term_b")], failed)
        self.assertEqual((back["class"], back["stop"]["agent"], back["stop"]["kind"]), ("stalled", "claude", "connection"))

    def test_a_claude_tab_whose_screen_shows_its_turn_running_is_working_and_not_judged(self) -> None:
        # 2026-11-11: tohoku-review-color-flow-docs was read as finished while its tab showed Composing… for 17 min.
        self.claude_says("/w/busy", user("go"), assistant(text("スクリーンショットを撮りました。")))
        self.claude_says("/w/ghost", user("go"), assistant(text("Merged.")))
        running = {"source": "screen", "tail": [
            "⏺ スクリーンショットを撮りました。",
            "· Composing… (17m 47s · ↓ 11.9k tokens · thinking more with xhigh effort)",
            "Update available! Run: brew upgrade claude-code@latest", "─" * 40, "❯", "─" * 40]}
        # A resized terminal can leave an old status line above the turn's end.
        ghost = {"source": "screen", "tail": ["✢ Honking… (1m 24s · ↓ 3.3k tokens)", "⏺ Merged.",
                                              "✻ Cooked for 2m 3s · done 10:00", "❯"]}
        judged = []
        rows = {r["path"]: r for r in scan.build_inventory(
            [worktree("/w/busy", "done"), worktree("/w/ghost", "done", pr={"number": 1, "state": "merged"})],
            [terminal("/w/busy", "claude", "term_b"), terminal("/w/ghost", "claude", "term_g")], self.projects, {},
            "要判断", now=NOW, judge=lambda message: judged.append(message) or verdict(),
            screens={(None, "term_b"): running, (None, "term_g"): ghost})}

        self.assertEqual((rows["/w/busy"]["class"], rows["/w/busy"]["reasons"]), ("working", [
            "the tab's screen shows Claude working: "
            "· Composing… (17m 47s · ↓ 11.9k tokens · thinking more with xhigh effort)"]))
        self.assertEqual(rows["/w/busy"]["closable"], [])
        self.assertEqual(rows["/w/ghost"]["class"], "finished")
        self.assertEqual(judged, ["Merged."])
        self.assertEqual([entry["path"] for entry in scan.review_queue(list(rows.values()))], [])

    def test_a_usage_limit_is_past_once_the_tab_shows_a_later_turn(self) -> None:
        # 2026-11-11: cvm-etl-devel-role resumed in another account and merged #3691 at 14:04, yet its transcript
        # here still ended at the weekly limit, so the scan called it stalled until the limit reset.
        limit = "You've hit your weekly limit · resets 2am (Asia/Tokyo)"
        # Claude Code tags a limit worded in a way the rule does not know yet; its tab shows the turn before it.
        reworded = "Claude is paused for this plan until 2am (Asia/Tokyo)"
        for path, said in (("/w/resumed", limit), ("/w/limited", limit), ("/w/reworded", reworded),
                           ("/w/wrapped", limit)):
            self.claude_says(path, user("go"), {**assistant(text(said)), "isApiErrorMessage": True,
                                                "error": "rate_limit"})
        resumed = {"source": "screen", "tail": [
            "⏺ PR #3691 を merge し、devel の ETL ホストで fetch しました。", "  確かめていないのは次の 2 つです。",
            "✻ Cooked for 4m 14s · done 14:04", "─" * 40, "❯", "─" * 40, "  ⏵⏵ bypass permissions on"]}
        # The limit's own turn ends with a turn-end line too, with no reply between.
        limited = {"source": "screen", "tail": [
            "❯ 続けて", "  ⎿  " + limit, "     /usage-credits to finish what you’re working on.",
            "✻ Baked for 1s · done 9:02 · 1 monitor still running", "─" * 40, "❯", "─" * 40]}
        stopped = {"source": "screen", "tail": [
            "⏺ テストを足しました。", "✻ Cooked for 2m 3s · done 8:40", "❯ 続けて", "  ⎿  " + reworded,
            "✻ Baked for 1s · done 9:02", "─" * 40, "❯", "─" * 40]}
        # A narrow pane wraps the notice: neither line shows it alone.
        wrapped = {"source": "screen", "tail": [
            "⏺ テストを足しました。", "✻ Cooked for 2m 3s · done 8:40", "❯ 続けて", "  ⎿  You've hit your weekly",
            "     limit · resets 2am (Asia/Tokyo)", "✻ Baked for 1s · done 9:02", "❯"]}
        judged = []
        rows = {r["path"]: r for r in scan.build_inventory(
            [worktree("/w/resumed", "done", pr={"number": 3691, "state": "merged"}), worktree("/w/limited", "done"),
             worktree("/w/reworded", "done"), worktree("/w/wrapped", "done")],
            [terminal("/w/resumed", "claude", "term_r"), terminal("/w/limited", "claude", "term_l"),
             terminal("/w/reworded", "claude", "term_w"), terminal("/w/wrapped", "claude", "term_n")], self.projects,
            {}, "要判断", now=NOW, judge=lambda message: judged.append(message) or verdict(),
            screens={(None, "term_r"): resumed, (None, "term_l"): limited, (None, "term_w"): stopped,
                     (None, "term_n"): wrapped})}

        self.assertIsNone(rows["/w/resumed"]["stop"])
        self.assertEqual(rows["/w/resumed"]["class"], "finished")
        self.assertIn("the transcript's usage limit is past: the tab's screen shows a later turn "
                      "(✻ Cooked for 4m 14s · done 14:04)", rows["/w/resumed"]["reasons"])
        self.assertEqual(judged, ["PR #3691 を merge し、devel の ETL ホストで fetch しました。\n確かめていないのは次の 2 つです。"])
        for path in ("/w/limited", "/w/reworded", "/w/wrapped"):
            self.assertEqual((rows[path]["class"], rows[path]["stop"]["kind"]), ("stalled", "usage_limit"))

    def test_a_past_connection_error_does_not_stall_a_tab_showing_a_new_reply_and_running_line(self) -> None:
        error = "API Error: Your computer went to sleep mid-response. The response above may be incomplete."
        progress = "✽ Transmuting… (14m 25s · ↓ 2.7k tokens · thinking with xhigh effort)"
        screens = {
            "resumed": [error, "⏺ 検査を続けています。", progress],
            "scrolled": ["⏺ 検査を続けています。", progress],
            "stopped": ["⏺ 前の検査です。", progress, error, "❯"],
            "new_error": [error, "⏺ 検査を続けています。", progress, "⎿  API Error: Can't reach the API server", "❯"],
            "finished": [error, "⏺ 検査を続けています。", "✻ Cooked for 4m 14s · done 14:04", "❯"]}
        for name in screens:
            self.claude_says(f"/w/{name}", user("go"), {**assistant(text(error)), "isApiErrorMessage": True,
                                                      "error": "server_error"})
        rows = {row["path"]: row for row in scan.build_inventory(
            [worktree(f"/w/{name}", "done") for name in screens],
            [terminal(f"/w/{name}", "claude", f"term_{name}") for name in screens], self.projects, {},
            "要判断", now=NOW, judge=lambda message: verdict(),
            screens={(None, f"term_{name}"): {"source": "screen", "tail": lines} for name, lines in screens.items()})}
        for name in ("resumed", "scrolled"):
            self.assertEqual(rows[f"/w/{name}"]["class"], "working")
            self.assertIsNone(rows[f"/w/{name}"]["stop"])
            self.assertIsNone(rows[f"/w/{name}"]["claude"]["api_error"])
        for name in ("stopped", "new_error", "finished"):
            self.assertEqual(rows[f"/w/{name}"]["class"], "stalled")
            self.assertEqual(rows[f"/w/{name}"]["stop"]["kind"], "connection")

    def test_a_local_codex_tab_without_a_recent_rollout_is_judged_from_its_screen(self) -> None:
        # 2026-11-11: four uv-migration Codex lanes were "unstarted" while their tabs showed them waiting on a secret.
        reply = "CI は Ruff 成功、残る3 job は引き続き CORELIB_PAT 設定待ちです。"
        ended = datetime.fromtimestamp(NOW - 600)  # the tab prints the local clock
        screen = {"source": "screen", "tail": [
            "• Ran gh pr checks 764", "  └ lint pass", "• " + reply, "", f"  Worked for 14m 48s • {ended:%H:%M}",
            "› Ask Codex to do anything", "  GPT-6.1-Sol xhigh · ~/orca/workspaces/app/uv"]}
        # A lane Codex took over from Claude: the older Claude transcript's last word is not how it stands.
        self.claude_says("/w/took", user("go"), {**assistant(text("Done. PR #9 is merged.")),
                                                 "timestamp": "2027-01-15T05:00:00Z"})
        # Of two Codex tabs, the newer turn's reply stands, whichever tab Orca lists last.
        older = {"source": "screen", "tail": [
            "• Done. PR #9 is merged.", "", f"  Worked for 3m 2s • {datetime.fromtimestamp(NOW - 3600):%H:%M}",
            "› Ask Codex to do anything"]}
        # A tab idle for three days shows an old turn: its clock is read on the day of its last output.
        # A tab that runs a later turn under an old completion is not judged from that completion.
        busy = {"source": "screen", "tail": [*older["tail"][:3], "› 続けて", "• Working (12s • esc to interrupt)"]}
        blocker = "PR #10 のレビュー待ちです。"
        self.claude_says("/w/idle", user("go"), {**assistant(text(blocker)), "timestamp": "2027-01-15T06:00:00Z"})
        judged = []
        rows = {r["path"]: r for r in scan.build_inventory(
            [worktree("/w/uv", "done", agent="codex"),
             worktree("/w/took", "done", agent="codex", pr={"number": 9, "state": "merged"}),
             worktree("/w/two", "done", agent="codex", pr={"number": 9, "state": "merged"}),
             worktree("/w/idle", "done", agent="codex"), worktree("/w/busy", "working", agent="codex")],
            [terminal("/w/uv", "codex", "term_u"), terminal("/w/took", "codex", "term_t"),
             terminal("/w/two", "codex", "term_new"), terminal("/w/two", "codex", "term_old"),
             terminal("/w/idle", "codex", "term_i", quiet_minutes=3 * 24 * 60), terminal("/w/busy", "codex", "term_b")],
            self.projects, {},
            "要判断", now=NOW, judge=lambda message: judged.append(message) or verdict(0.1, "blocked_on_others"),
            screens={(None, "term_u"): screen, (None, "term_t"): screen, (None, "term_new"): screen,
                     (None, "term_old"): older, (None, "term_i"): older, (None, "term_b"): busy})}
        row = rows["/w/uv"]

        # The line gives the minute only: its last second keeps a reply after a merge in that minute.
        self.assertEqual(row["orca_message"], {"text": reply, "at": "2027-01-15T07:50:59Z", "from": "screen"})
        self.assertNotIn("orca_message", rows["/w/busy"])
        self.assertEqual([rows[path]["class"] for path in ("/w/uv", "/w/took", "/w/two", "/w/idle")], ["stalled"] * 4)
        self.assertEqual(judged, [reply] * 3 + [blocker])
        self.assertEqual([entry["path"] for entry in scan.review_queue(list(rows.values()))],
                         ["/w/uv", "/w/took", "/w/two", "/w/idle"])
        # Its tab is read even while Orca shows the Codex working.
        busy = [worktree("/w/uv", "working", agent="codex")]
        self.assertEqual([t["handle"] for t in scan.screen_targets(busy, [terminal("/w/uv", "codex", "term_u")], set())],
                         ["term_u"])
        self.assertEqual(scan.screen_targets(busy, [terminal("/w/uv", "codex", "term_u")], {"/w/uv"}), [])

    def test_another_macs_lane_read_from_orcas_copy_is_told_apart_by_its_text(self) -> None:
        # Orca's copy of a remote lane's last message carries no Claude Code error tag.
        def stop(line: str) -> tuple[str, str, str | None] | None:
            found = scan.stop_reason({"claude": None, "codex": [], "orca_message": {"text": line}})
            return found and (found["kind"], found["action"], found["resets"])

        self.assertEqual(stop("API Error: Opus 5.5's safeguards flagged this session"), ("other", "human", None))
        self.assertEqual(stop("Not logged in · Please run /login"), ("login", "human", None))
        self.assertEqual(stop("You've hit your monthly spend limit · raise it at claude.ai/settings/usage · "
                              "your session limit resets 2pm (Asia/Tokyo)"), ("usage_limit", "wait", "2pm (Asia/Tokyo)"))
        self.assertEqual(stop("API Error: Connection lost mid-response. The response above may be incomplete."),
                         ("connection", "wake", None))

    def test_codex_capacity_failure_on_screen_is_stalled_even_with_a_merged_pr(self) -> None:
        failure = "■ Selected model is at capacity"
        codex = {"/w/cut": [{"rollout": "r", "last_user": "finish", "last_role": "assistant",
                              "last_assistant": "Done.", "last_assistant_at": "2026-10-01T01:00:00Z",
                              "pending_requests": []}]}
        rows = {r["path"]: r for r in self.inventory(
            [worktree("/w/cut", "done", agent="codex", pr={"number": 1, "state": "merged"}),
             worktree("/w/resumed", "working", agent="codex")],
            [terminal("/w/cut", "codex", preview="Done.\n" + failure + "\n"),
             terminal("/w/resumed", "codex", preview=failure + "\n› try again\n• Retrying the build.\n")],
            {**codex, "/w/resumed": codex["/w/cut"]})}
        row = rows["/w/cut"]
        self.assertEqual((row["class"], row["reasons"], row["closable"]),
                         ("stalled", ["turn ended with an API error: " + failure], []))
        # The tab still shows the earlier turn's failure while a new turn runs.
        self.assertEqual((rows["/w/resumed"]["class"], rows["/w/resumed"]["stop"]), ("working", None))

    def test_codex_stream_disconnect_on_screen_is_a_connection_to_wake(self) -> None:
        [row] = self.inventory([worktree("/w/cut", "done", agent="codex")],
                               [terminal("/w/cut", "codex", preview="■ stream disconnected before completion\n")])
        self.assertEqual(row["class"], "stalled")
        self.assertEqual((row["stop"]["kind"], row["stop"]["action"]), ("connection", "wake"))
        self.assertFalse(row["closable"])

    def test_one_shot_schedule_lost_with_its_session_is_reported_but_fired_and_cancelled_jobs_are_not(self) -> None:
        self.claude_says("/w/scheduled", user("go"),
            assistant({"type": "tool_use", "id": "cron1", "name": "CronCreate",
                       "input": {"cron": "30 18 9 11 *", "prompt": "Release status", "recurring": False}}),
            user([{"type": "tool_result", "tool_use_id": "cron1",
                   "content": "Scheduled one-shot task abc12345 (30 18 9 11 *). Session-only."}]),
            assistant(text("Scheduled.")))
        [row] = self.inventory([worktree("/w/scheduled", "done")], [])
        self.assertTrue(any("予約が消えた:" in reason and "Release status" in reason for reason in row["reasons"]))
        self.assertEqual(row["class"], "stalled")
        path = Path(row["claude"]["transcript"])
        with path.open("a") as stream:
            stream.write(json.dumps(user("Release status")) + "\n")
        [fired] = self.inventory([worktree("/w/scheduled", "done")], [])
        self.assertFalse(fired.get("lost_schedules"))

    def test_a_lead_that_left_its_workers_reports_unread_is_stalled_or_says_so_with_its_question(self) -> None:
        # 2026-10-06 a lead ended its turn while workers ran; their reports sat unread for 20 hours.
        def at(minutes_ago: float) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes_ago * 60))

        def message(kind: str, minutes_ago: float, read: int = 0, run: str = "run_a") -> dict:
            return {"run_id": run, "to_handle": "run:" + run, "type": kind, "read": read,
                    "created_at": at(minutes_ago), "subject": kind}

        mail = scan.unread_worker_mail(
            [message("worker_done", 200), message("worker_done", 50), message("question", 40),
             message("worker_done", 45, read=1), message("heartbeat", 30), message("escalation", 2),
             message("worker_done", 20, run="run_b")],
            [{"id": "run_a", "coordinator_handle": "term_lead"},
             {"id": "run_b", "coordinator_handle": "term_asks"}])
        asks = "#470 の扱いを返事ください。"
        self.claude_says("/w/lead", {**user("go"), "timestamp": at(60)}, assistant(text("worker の報告を待ちます。")))
        # A timestamp without a zone is UTC, like Orca's.
        self.claude_says("/w/asks", {**user("go"), "timestamp": at(60).rstrip("Z")}, assistant(text(asks)))

        result = scan.build_inventory(
            [worktree("/w/lead", "done"), worktree("/w/asks", "done")],
            [terminal("/w/lead", "claude", "term_lead"), terminal("/w/asks", "claude", "term_asks")],
            self.projects, {}, "要判断", now=NOW, judge=judging({asks: verdict(0.9, "blocked_on_others")}),
            mail=mail, processes={"/w/lead": [{"pid": 9301, "parent": 1, "command": "sleep 60", "elapsed": 5.0,
                                               "cpu": 0.0, "still_since": None, "closed": []}]})

        # A wait still running in the lead's worktree does not hide its unread reports.
        self.assertEqual({r["path"]: (r["class"], r["reasons"], r["closable"]) for r in result}, {
            "/w/lead": ("stalled", [f"2 orchestration messages unread since its last turn began (oldest {at(50)})"],
                        []),
            "/w/asks": ("waiting", ["final message asks (Jev 0.90)",
                                    f"1 orchestration message unread since its last turn began (oldest {at(20)})"],
                        []),
        })

    def test_a_worktree_no_agent_ever_worked_in_says_so(self) -> None:
        result = self.classes([worktree("/w/shell", None, pr={"number": 3, "state": "open"}, status="in-review")],
                              [terminal("/w/shell", None)])

        self.assertEqual(result["/w/shell"], ("stalled", ["no agent session in this worktree"]))


def jev_answer(asks: float | str, status: str = "done", confidence: float = 0.9) -> io.BytesIO:
    return io.BytesIO(json.dumps({"model": "jev-1.13.0", "answers": {
        "asks_user": {"type": "noul", "noul": asks},
        "status": {"type": "choice", "choice": status, "confidence": confidence,
                   "probabilities": {status: confidence}}}}).encode())


class CutShort(io.BytesIO):
    """A response whose connection closes mid-body."""

    def read(self, *args: object) -> bytes:
        raise http.client.IncompleteRead(b"{")


class JevTests(unittest.TestCase):
    def test_asks_jev_whether_the_tail_of_a_message_waits_on_the_user_and_how_the_work_stands(self) -> None:
        sent = []

        def opener(request: urllib.request.Request, timeout: float) -> io.BytesIO:
            assert isinstance(request.data, bytes)
            sent.append((request.full_url, request.get_header("Authorization"), json.loads(request.data)))
            return jev_answer(0.86, "blocked_on_others", 0.62)

        judge = scan.jev_judge("key-1", opener=opener)

        self.assertEqual(judge("経緯。" * 3000 + "\nやるかどうか指示をください。"),
                         {"asks": 0.86, "status": "blocked_on_others", "confidence": 0.62})
        url, authorization, body = sent[0]
        self.assertEqual((url, authorization, body["model"]),
                         ("https://api.typesafe.ai/v1/systemone", "Bearer key-1", "jev-latest"))
        self.assertEqual(body["questions"]["asks_user"]["type"], "noul")
        self.assertEqual(body["questions"]["status"]["type"], "choice")
        self.assertEqual(sorted(body["questions"]["status"]["criteria"]),
                         ["blocked_on_others", "done", "in_progress"])
        self.assertTrue(body["state"].endswith("やるかどうか指示をください。"))
        self.assertEqual(len(body["state"]), scan.JEV_MAX_CHARS)

    def test_a_failed_jev_call_judges_nothing_and_says_why(self) -> None:
        failures = [urllib.error.HTTPError("u", 401, "Unauthorized", Message(), None),
                    TimeoutError("timed out"), urllib.error.URLError("offline"),
                    io.BytesIO(b"not json"), io.BytesIO(b'{"answers": {}}'), jev_answer(float("nan")),
                    jev_answer(1.5), jev_answer("nan"), CutShort(), jev_answer(0.1, "asks_user"),
                    jev_answer(0.1, "done", float("nan")), jev_answer(0.1, "done", 1.2)]
        for failure in failures:
            def opener(request: urllib.request.Request, timeout: float, failure: Exception | io.BytesIO = failure) -> io.BytesIO:
                if isinstance(failure, Exception):
                    raise failure
                return failure

            err = io.StringIO()
            with redirect_stderr(err):
                self.assertIsNone(scan.jev_judge("key-1", opener=opener)("m"))
            self.assertIn("Jev could not judge", err.getvalue())


class NextStepFactTests(unittest.TestCase):
    def test_graphql_retries_transient_failures_and_reports_stderr_when_exhausted(self) -> None:
        success = subprocess.CompletedProcess([], 0, '{"data":{"repository":{"name":"app"}}}', '')
        for failure in (subprocess.CompletedProcess([], 1, '', 'connection reset'),
                        subprocess.CompletedProcess([], 1, '<html>bad gateway</html>', 'upstream unavailable'),
                        OSError('connection refused')):
            with self.subTest(failure=str(failure)), patch.object(scan.subprocess, "run", side_effect=[failure, success]), \
                    patch.object(scan.time, "sleep"):
                self.assertEqual(scan.graphql_repository("acme/app", "query"), {"name": "app"})
        with patch.object(scan.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, '', 'TLS failed')), \
                patch.object(scan.time, "sleep"), redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(scan.graphql_repository("acme/app", "query"))
        self.assertIn("TLS failed", err.getvalue())

    def test_a_saved_verdict_is_read_again_once_everything_it_waits_on_closed(self) -> None:
        # servicean-87 stayed blocked on masters#731→#732 and masters-ui#125→#126 after all four merged.
        rows = [{"path": "/w/sp", "repo": "taihei-service-project", "class": "stalled", "linked_pr": None,
                 "jev": {"asks": 0.1}, "claude": {"final_at": "t1"}, "codex": [], "reasons": []},
                {"path": "/w/m", "repo": "taihei-coresys-masters", "class": "working", "linked_pr": None},
                {"path": "/w/ui", "repo": "taihei-masters-ui", "class": "working", "linked_pr": None}]
        gits = {"/w/sp": {"branch": "sp-87", "head": "aaa", "dirty": 0, "unpushed": 0,
                          "github": "spin-dd/taihei-service-project"}}

        def scan_with(masters_732: str, verdicts: dict) -> dict:
            github = FakeGitHub({"spin-dd/taihei-coresys-masters": [pr_node(731, state="MERGED"),
                                                                     pr_node(732, state=masters_732)],
                                 "spin-dd/taihei-masters-ui": [pr_node(125, state="MERGED"),
                                                                pr_node(126, state="MERGED")]})
            with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
                return scan.add_next_step_facts(rows, git_lookup=gits.get, verdicts=verdicts,
                                                pr_lookup=partial(scan.gh_prs, cache_dir=Path(cache)))[0]

        # The scan joins the inbox reports, which the verdict's read includes, after these facts.
        inbox = {"done": {"at": "2026-10-28T01:00:00Z", "host": "mbp", "text": "done"}}
        verdicts = {"/w/sp": {"read": scan.read_key({**scan_with("OPEN", {}), "inbox": inbox}), "class": "blocked",
                              "waits_on": "hdknr（masters#731→#732 のレビュー）、三浦さん（masters-ui#125→#126 の merge）"}}
        self.assertEqual(scan.waited_items("masters#731 and #732", "spin-dd/x", {"taihei-coresys-masters"}),
                         [("spin-dd/taihei-coresys-masters", 731), ("spin-dd/taihei-coresys-masters", 732)])
        waiting = {**scan_with("OPEN", verdicts), "inbox": inbox}
        self.assertEqual((scan.review_queue([waiting], verdicts), scan.with_verdicts([waiting], verdicts)[0]["verdict"]["class"]),
                         ([], "blocked"))

        closed = {**scan_with("MERGED", verdicts), "inbox": inbox}
        self.assertEqual([entry["path"] for entry in scan.review_queue([closed], verdicts)], ["/w/sp"])
        self.assertNotIn("verdict", scan.with_verdicts([closed], verdicts)[0])
        self.assertEqual(closed["reasons"], ["everything the saved verdict waits on closed: "
                                             "spin-dd/taihei-coresys-masters#731, spin-dd/taihei-coresys-masters#732, "
                                             "spin-dd/taihei-masters-ui#125, spin-dd/taihei-masters-ui#126"])

    def test_summarizes_review_merge_and_check_state_of_a_pr(self) -> None:
        data = {"number": 11263, "state": "OPEN", "reviewDecision": "APPROVED",
                "reviewRequests": [{"login": "k-mizokami"}, {"name": "backend"}],
                "latestReviews": [{"author": {"login": "hdknr"}, "state": "APPROVED"},
                                  {"author": None, "state": "COMMENTED"}],
                "mergeStateStatus": "CLEAN", "headRefName": "fix-a", "headRefOid": "abc123", "mergedAt": None,
                "url": "https://github.com/acme/app/pull/11263",
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"},
                                      {"status": "COMPLETED", "conclusion": "SUCCESS"},
                                      {"status": "IN_PROGRESS", "conclusion": ""},
                                      {"state": "FAILURE"}]}

        self.assertEqual(scan.summarize_pr(data), {
            "number": 11263, "url": "https://github.com/acme/app/pull/11263", "state": "OPEN",
            "head": "fix-a", "head_oid": "abc123", "review": "APPROVED",
            "requested": ["k-mizokami", "backend"], "reviews": ["hdknr:APPROVED", "ghost:COMMENTED"],
            "latest_human_review": None, "latest_human_activity": None,
            "last_commit_at": None, "reply_after_last_commit": False,
            "merge_state": "CLEAN", "merged_at": None,
            "checks": {"SUCCESS": 2, "IN_PROGRESS": 1, "FAILURE": 1}})

    def test_latest_human_review_ignores_newer_bot_reviews(self) -> None:
        reviews = [
            {"user": {"login": "reviewer", "type": "User"}, "state": "APPROVED",
             "submitted_at": "2026-10-21T01:00:00Z"},
            {"user": {"login": "review-bot[bot]", "type": "Bot"}, "state": "COMMENTED",
             "submitted_at": "2026-10-21T04:00:00Z"},
            {"user": {"login": "reviewer", "type": "User"}, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-10-21T02:00:00Z"},
        ]

        pr = scan.summarize_pr({"number": 42}, reviews)

        self.assertEqual(pr["latest_human_review"],
                         {"author": "reviewer", "state": "CHANGES_REQUESTED",
                          "submitted_at": "2026-10-21T02:00:00Z"})

    def test_pr_authors_comment_is_not_an_external_review(self) -> None:
        review = {"user": {"login": "author", "type": "User"}, "state": "COMMENTED",
                  "submitted_at": "2026-10-21T07:39:59Z"}

        pr = scan.summarize_pr({"number": 42, "author": {"login": "author"}}, [review])

        self.assertIsNone(pr["latest_human_review"])

    def test_each_human_comment_surface_returns_the_wait_to_us_after_the_last_commit(self) -> None:
        data = {"number": 42, "author": {"login": "Author"},
                "commits": [{"committedDate": "2026-10-21T02:00:00Z"}]}
        reviewer = {"user": {"login": "reviewer", "type": "User"},
                    "state": "COMMENTED", "submitted_at": "2026-10-21T02:01:00Z"}
        conversation = {"user": {"login": "reviewer", "type": "User"},
                        "created_at": "2026-10-21T02:02:00Z"}
        line = {"user": {"login": "reviewer", "type": "User"},
                "created_at": "2026-10-21T02:03:00Z"}
        for reviews, comments, lines, kind in (([reviewer], [], [], "review"),
                                               ([], [conversation], [], "comment"),
                                               ([], [], [line], "line_comment")):
            with self.subTest(kind=kind):
                pr = scan.summarize_pr(data, reviews, comments, lines)
                self.assertTrue(pr["reply_after_last_commit"])
                self.assertEqual(pr["latest_human_activity"]["kind"], kind)
        bot = {"user": {"login": "bot", "type": "Bot"},
               "created_at": "2026-10-21T03:00:00Z"}
        author = {"user": {"login": "author", "type": "User"},
                  "created_at": "2026-10-21T04:00:00Z"}
        earlier = {**conversation, "created_at": "2026-10-21T01:00:00Z"}
        pr = scan.summarize_pr(data, [], [bot, author, earlier], [])
        self.assertFalse(pr["reply_after_last_commit"])
        self.assertEqual(pr["latest_human_activity"]["at"], earlier["created_at"])

    def test_remote_pr_comment_marks_the_row_as_our_turn(self) -> None:
        row = {"path": "/remote/w", "class": "stalled", "environment": "macbook-pro-16",
               "branch": "refs/heads/fix-a", "linked_pr": None,
               "orca_message": {"text": "PR https://github.com/acme/app/pull/42", "at": "2026-10-21T01:00:00Z"},
               "reasons": ["review pending"], "jev": {"source": "orca", "status": "blocked_on_others"}}
        pr = {"number": 42, "head_oid": "aaa", "state": "OPEN", "reply_after_last_commit": True}
        result = scan.add_next_step_facts([row], pr_lookup=each_ref(lambda slug, ref: pr))[0]
        self.assertEqual(result["git"]["github"], "acme/app")
        self.assertIsNone(result["pr"]["head_matches"])
        self.assertEqual(result["waits_on"], "self")
        self.assertIn("human PR response after the latest commit", result["reasons"])

    def test_gh_prs_reads_human_reviews_and_keeps_pr_when_review_lookup_fails(self) -> None:
        review = {"user": {"login": "reviewer", "type": "User"}, "state": "CHANGES_REQUESTED",
                  "submitted_at": "2026-10-21T02:00:00Z"}
        github = FakeGitHub({"acme/app": [pr_node(42)]}, {"repos/acme/app/pulls/42/reviews": [review]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github) as run:
            self.assertEqual(scan.gh_prs("acme/app", [42], cache_dir=Path(cache))[42]["latest_human_review"]["state"],
                             "CHANGES_REQUESTED")
            self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list],
                             [scan.GH_TIMEOUT] * 4)
            self.assertEqual(run.call_args_list[1].args[0],
                             ["gh", "api", "--paginate", "--slurp", "repos/acme/app/pulls/42/reviews"])
        for failure in (subprocess.CompletedProcess([], 1, ""), subprocess.TimeoutExpired("gh", 15)):
            def failing_reviews(argv: list[str], failure: Exception | subprocess.CompletedProcess[str] = failure, **kwargs: object) -> subprocess.CompletedProcess[str]:
                if not argv[-1].endswith("/reviews"):
                    return github(argv, **kwargs)
                github.calls.append(argv)
                if isinstance(failure, Exception):
                    raise failure
                return failure

            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as cache, \
                    patch.object(scan.subprocess, "run", side_effect=failing_reviews):
                github.calls.clear()
                self.assertEqual(scan.gh_prs("acme/app", [42], cache_dir=Path(cache))[42]["state"], "OPEN")
                # A failed read is not kept, so the next run reads it again.
                scan.gh_prs("acme/app", [42], cache_dir=Path(cache))
                self.assertEqual(sum(call[-1].endswith("/reviews") for call in github.calls), 2)

    def test_a_failed_or_missing_gh_degrades_to_no_pr(self) -> None:
        with tempfile.TemporaryDirectory() as cache, redirect_stderr(io.StringIO()) as err:
            with patch.object(scan.subprocess, "run", side_effect=FileNotFoundError("gh")):
                self.assertEqual(scan.gh_prs("acme/app", [7], cache_dir=Path(cache)), {7: None})
            with patch.object(scan.subprocess, "run", side_effect=scan.subprocess.TimeoutExpired("gh", 30)):
                self.assertEqual(scan.gh_prs("acme/app", [7], cache_dir=Path(cache)), {7: None})
            limited = scan.subprocess.CompletedProcess(
                [], 1, stdout='{"errors": [{"type": "RATE_LIMITED"}]}', stderr="API rate limit exceeded")
            with patch.object(scan.subprocess, "run", return_value=limited):
                self.assertEqual(scan.gh_prs("acme/app", ["fix-a"], cache_dir=Path(cache)), {"fix-a": None})
        # The rows stay without PR facts; stderr says which repository's query failed and why.
        self.assertIn("acme/app", err.getvalue())
        self.assertIn("API rate limit exceeded", err.getvalue())

    def test_a_run_asks_github_once_per_repository_and_rereads_activity_only_for_updated_prs(self) -> None:
        # 2026-10-28: a `gh pr view` per PR plus three REST reads on every run used up the shared
        # GraphQL budget. Before this change these 6 PRs in 2 repositories cost 6 + 18 calls each run.
        refs = [("acme/app", 7), ("acme/app", 8), ("acme/app", "fix-c"),
                ("acme/lib", 3), ("acme/lib", 4), ("acme/lib", 5)]
        rows = [{"path": f"/w/{index}", "class": "stalled",
                 "linked_pr": {"number": ref} if isinstance(ref, int) else None}
                for index, (_, ref) in enumerate(refs)]
        gits = {f"/w/{index}": {"branch": ref if isinstance(ref, str) else f"b{index}", "head": "aaa",
                                "dirty": 0, "unpushed": 0, "github": slug}
                for index, (slug, ref) in enumerate(refs)}
        app = [pr_node(7), pr_node(8), pr_node(9, "fix-c")]
        github = FakeGitHub({"acme/app": app, "acme/lib": [pr_node(3), pr_node(4), pr_node(5)]})
        reply = {"user": {"login": "reviewer", "type": "User"}, "created_at": "2026-10-28T01:00:00Z"}

        with tempfile.TemporaryDirectory() as cache:
            def run() -> tuple[list[dict], tuple[int, int]]:
                github.calls.clear()
                with patch.object(scan.subprocess, "run", side_effect=github):
                    result = scan.add_next_step_facts(
                        rows, git_lookup=gits.get, pr_lookup=partial(scan.gh_prs, cache_dir=Path(cache)))
                return [row["pr"] for row in result], (len(github.graphql()), len(github.rest()))

            first, calls = run()
            self.assertEqual([pr["number"] for pr in first], [7, 8, 9, 3, 4, 5])
            self.assertEqual(calls, (2, 18))
            # Nothing changed on GitHub: the reviews and comments come from the last run.
            self.assertEqual(run(), (first, (2, 0)))
            # A comment moves its PR's updatedAt, so only that PR is read again.
            app[1] = pr_node(8, updated_at="2026-10-28T01:00:00Z")
            github.activity["repos/acme/app/issues/8/comments"] = [reply]
            prs, calls = run()
            self.assertEqual(calls, (2, 3))
            self.assertEqual(prs[1]["latest_human_activity"]["at"], reply["created_at"])
            # A damaged copy is read again, not trusted.
            (Path(cache) / "acme/lib/3.json").write_text("{")
            self.assertEqual(run()[1], (2, 3))

    def test_a_repository_with_more_prs_than_one_query_holds_is_read_25_at_a_time(self) -> None:
        github = FakeGitHub({"acme/app": [pr_node(number) for number in range(1, 31)]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
            found = scan.gh_prs("acme/app", list(range(1, 31)), cache_dir=Path(cache))
        self.assertEqual(sorted(pr["number"] for pr in found.values()), list(range(1, 31)))
        self.assertEqual([len(re.findall(r"pullRequest\(", call[-1])) for call in github.graphql()], [25, 5])

    def test_a_missing_pr_leaves_the_others_in_its_query(self) -> None:
        github = FakeGitHub({"acme/app": [pr_node(7)]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
            found = scan.gh_prs("acme/app", [7, 404], cache_dir=Path(cache))
        self.assertEqual((found[7]["number"], found[404]), (7, None))
        self.assertEqual((len(github.graphql()), len(github.rest())), (1, 3))

    def test_a_branch_finds_its_pr_as_gh_pr_view_does(self) -> None:
        # gh puts an open PR first and the rest newest first, skips a fork's branch of the same
        # name, and skips a closed or merged PR whose head is the default branch.
        github = FakeGitHub({"acme/app": [
            pr_node(10, "fix-a", "CLOSED"), pr_node(11, "fix-a", "OPEN"), pr_node(12, "fix-a", "MERGED"),
            pr_node(21, "fix-b", "MERGED"), pr_node(22, "fix-b", "CLOSED"),
            pr_node(31, "fork", isCrossRepository=True), pr_node(41, "main", "MERGED")]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
            found = scan.gh_prs("acme/app", ["fix-a", "fix-b", "fork", "main"], cache_dir=Path(cache))
        self.assertEqual({ref: pr and pr["number"] for ref, pr in found.items()},
                         {"fix-a": 11, "fix-b": 22, "fork": None, "main": None})
        self.assertEqual(len(github.graphql()), 1)

    def test_names_are_checked_before_they_go_into_a_query(self) -> None:
        github = FakeGitHub({"acme/app": [pr_node(5, 'say-"hi"')]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
            found = scan.gh_prs("acme/app", ['say-"hi"', "two words", "x\ny"], cache_dir=Path(cache))
            self.assertEqual(scan.gh_prs('acme/app") { x', [7], cache_dir=Path(cache)), {7: None})
        self.assertEqual({ref: pr and pr["number"] for ref, pr in found.items()},
                         {'say-"hi"': 5, "two words": None, "x\ny": None})
        [query] = github.graphql()
        self.assertNotIn("two words", query[-1])

    def test_a_pr_read_by_graphql_summarizes_as_its_gh_pr_view_json_did(self) -> None:
        checks = [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                  {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
                  {"__typename": "StatusContext", "state": "FAILURE"}]
        node = pr_node(11263, "fix-a", reviewDecision="APPROVED", headRefOid="abc123",
                       author={"__typename": "Bot", "login": "renovate"},
                       reviewRequests={"nodes": [
                           {"requestedReviewer": {"__typename": "User", "login": "k-mizokami"}},
                           {"requestedReviewer": {"__typename": "Team", "name": "backend"}}]},
                       latestReviews={"nodes": [{"author": {"login": "hdknr"}, "state": "APPROVED"},
                                                {"author": None, "state": "COMMENTED"}]},
                       commits={"nodes": [{"commit": {"committedDate": "2026-10-21T02:00:00Z",
                                                      "statusCheckRollup": {"contexts": {"nodes": checks}}}}]})
        # What `gh pr view --json` printed for the same PR: a bot author as app/<login>, and every commit.
        view = {"number": 11263, "url": node["url"], "state": "OPEN", "author": {"is_bot": True, "login": "app/renovate"},
                "headRefName": "fix-a", "headRefOid": "abc123", "reviewDecision": "APPROVED",
                "reviewRequests": [{"__typename": "User", "login": "k-mizokami"},
                                   {"__typename": "Team", "name": "backend", "slug": "backend"}],
                "latestReviews": [{"author": {"login": "hdknr"}, "state": "APPROVED"},
                                  {"author": None, "state": "COMMENTED"}],
                "mergeStateStatus": "CLEAN", "mergedAt": None,
                "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                                      {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""},
                                      {"__typename": "StatusContext", "state": "FAILURE"}],
                "commits": [{"committedDate": "2026-10-20T00:00:00Z"}, {"committedDate": "2026-10-21T02:00:00Z"}]}
        # A person whose login is the bot's name is not the bot that wrote the PR.
        comment = {"user": {"login": "renovate", "type": "User"}, "created_at": "2026-10-21T03:00:00Z",
                   "body": "見ました"}
        github = FakeGitHub({"acme/app": [node]}, {"repos/acme/app/issues/11263/comments": [comment]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
            found = scan.gh_prs("acme/app", [11263], cache_dir=Path(cache))[11263]
        self.assertEqual(found, scan.summarize_pr(view, [], [comment], []))
        self.assertTrue(found["reply_after_last_commit"])


    def test_reads_uncommitted_and_unpushed_work_and_the_github_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin, work = Path(tmp) / "origin.git", Path(tmp) / "work"
            git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
            git(tmp, "clone", "-q", str(origin), str(work))
            git(work, "config", "user.email", "t@example.com")
            git(work, "config", "user.name", "t")
            for name in ("a.txt", "b.txt"):
                (work / name).write_text(name)
                git(work, "add", name)
                git(work, "commit", "-q", "-m", name)
                if name == "a.txt":
                    git(work, "push", "-q", "-u", "origin", "main")
            (work / "notes.txt").write_text("draft")
            git(work, "remote", "set-url", "origin", "git@github.com:acme/app.git")

            self.assertEqual(scan.git_state(str(work)),
                             {"branch": "main", "head": git(work, "rev-parse", "HEAD"),
                              "head_at": git(work, "log", "-1", "--format=%cI"),
                              "dirty": 1, "unpushed": 1, "github": "acme/app"})
            self.assertIsNone(scan.git_state(str(Path(tmp) / "missing")))

        self.assertEqual(scan.github_slug("https://github.com/spin-dd/taihei-report.git"), "spin-dd/taihei-report")
        self.assertIsNone(scan.github_slug("https://git.disroot.org/usvdh/collect.git"))


    def test_a_pr_merged_after_the_final_message_ends_the_wait_it_reported(self) -> None:
        numbers = {name: number for number, name in enumerate(
            ("merged-later", "codex-merged-later", "merged-before", "continues", "other-head", "codex-took-over"), 7)}

        def stalled(path: str, said_at: str, status: str = "blocked_on_others", source: str = "claude") -> dict:
            transcripts = ({"claude": {"final_text": "PR を出しました。レビュー待ちです。", "final_at": said_at},
                            "codex": []} if source == "claude" else
                           {"claude": None, "codex": [{"last_assistant": "PR を出しました。",
                                                       "last_assistant_at": said_at}]})
            return {"path": path, "class": "stalled",
                    "linked_pr": {"number": numbers[path.rsplit("/", 1)[-1]], "state": "open"},
                    "reasons": ["final message waits on someone else (Jev 0.90)"],
                    "jev": {"source": source, "asks": 0.1, "status": status, "confidence": 0.9}, **transcripts}

        rows = [stalled("/w/merged-later", "2026-10-06T07:25:00.000Z"),
                stalled("/w/codex-merged-later", "2026-10-06T07:25:00.000Z", source="codex"),
                stalled("/w/merged-before", "2026-10-06T07:40:00.000Z"),
                stalled("/w/continues", "2026-10-06T07:25:00.000Z", status="in_progress"),
                stalled("/w/other-head", "2026-10-06T07:25:00.000Z"),
                # Jev read a Codex tab's reply, newer than both Claude's last word and the merge.
                {**stalled("/w/codex-took-over", "2026-10-06T07:25:00.000Z"),
                 "jev": {"source": "orca", "asks": 0.1, "status": "blocked_on_others", "confidence": 0.9},
                 "orca_message": {"text": "CORELIB_PAT 設定待ちです。", "at": "2026-10-06T07:50:00Z", "from": "screen"}}]
        git = {"branch": "fix-a", "head": "aaa", "dirty": 0, "unpushed": 0, "github": "acme/app"}
        heads = {numbers["other-head"]: "zzz"}

        def pr_lookup(slug: str, ref: object) -> dict:
            return {"number": ref, "state": "MERGED", "merged_at": "2026-10-06T07:35:27Z",
                    "head_oid": heads.get(ref, "aaa")}

        def git_lookup(path: str) -> dict:
            return git

        result = {r["path"]: (r["class"], r["reasons"])
                  for r in scan.add_next_step_facts(rows, git_lookup=git_lookup, pr_lookup=each_ref(pr_lookup))}

        self.assertEqual(result["/w/merged-later"], ("finished", ["PR merged after the final message"]))
        self.assertEqual(result["/w/codex-merged-later"], ("finished", ["PR merged after the final message"]))
        self.assertEqual(result["/w/merged-before"][0], "stalled")
        self.assertEqual(result["/w/continues"][0], "stalled")
        self.assertEqual(result["/w/other-head"][0], "stalled")
        self.assertEqual(result["/w/codex-took-over"][0], "stalled")

    def test_a_merged_pr_finishes_a_row_no_agent_ran_in(self) -> None:
        # Orca's PR link is often missing or stale; the lookup by branch sees the merge.
        def row(path: str, reasons: list[str], jev: dict | None = None, link: dict | None = None) -> dict:
            return {"path": path, "class": "stalled", "linked_pr": link, "reasons": reasons, "jev": jev,
                    "claude": None, "codex": []}

        rows = [row("/w/no-session", ["no agent session in this worktree"]),
                row("/w/done", ["final message says done (Jev 1.00)"],
                    jev={"source": "claude", "asks": 0.0, "status": "done", "confidence": 1.0},
                    link={"number": 7, "state": "open"}),
                row("/w/continues", ["final message says work continues (Jev 0.90)"],
                    jev={"source": "claude", "asks": 0.1, "status": "in_progress", "confidence": 0.9}),
                row("/w/no-session-other-head", ["no agent session in this worktree"]),
                row("/w/no-session-open", ["no agent session in this worktree"])]
        heads = {"no-session-other-head": "zzz"}
        states = {"no-session-open": "OPEN"}

        def git_lookup(path: str) -> dict:
            return {"branch": path.rsplit("/", 1)[-1], "head": "aaa", "dirty": 0,
                    "unpushed": 0, "github": "acme/app"}

        def pr_lookup(slug: str, ref: object) -> dict:
            return {"number": 7, "state": states.get(ref, "MERGED"), "merged_at": "2026-10-06T07:35:27Z",
                    "head_oid": heads.get(ref, "aaa")}

        result = {r["path"]: (r["class"], r["reasons"])
                  for r in scan.add_next_step_facts(rows, git_lookup=git_lookup, pr_lookup=each_ref(pr_lookup))}

        self.assertEqual(result["/w/no-session"], ("finished", ["PR merged"]))
        # Jev's "done" is a fast read: a merged row it judged waits for the careful one.
        self.assertEqual(result["/w/done"][0], "stalled")
        self.assertEqual(result["/w/continues"][0], "stalled")
        self.assertEqual(result["/w/no-session-other-head"][0], "stalled")
        self.assertEqual(result["/w/no-session-open"], ("finished", ["no local work beyond open PR head"]))

    def test_open_pr_cleanup_uses_published_head_not_upstream(self) -> None:
        names = ("clean", "human-replied", "dirty", "unpushed", "other-head", "unknown-unpushed")
        rows = [{"path": "/w/" + name, "class": "stalled", "reasons": [scan.NO_SESSION_REASON],
                 "linked_pr": None, "claude": None, "codex": []} for name in names]

        def git_lookup(path: str) -> dict:
            name = path.rsplit("/", 1)[-1]
            return {"branch": name, "head": "aaa", "dirty": int(name == "dirty"),
                    "unpushed": None if name == "unknown-unpushed" else int(name == "unpushed"),
                    "github": "acme/app"}

        def pr_lookup(slug: str, ref: object) -> dict:
            return {"number": 7, "state": "OPEN", "head_oid": "bbb" if ref == "other-head" else "aaa",
                    "reply_after_last_commit": ref == "human-replied"}

        result = {row["path"].rsplit("/", 1)[-1]: row for row in scan.add_next_step_facts(
            rows, git_lookup=git_lookup, pr_lookup=each_ref(pr_lookup))}

        self.assertEqual({name: (row["class"], row.get("open_pr_cleanup", False)) for name, row in result.items()},
                         {"clean": ("finished", True), "human-replied": ("finished", True),
                          "dirty": ("stalled", False), "unpushed": ("finished", True),
                          "other-head": ("stalled", False), "unknown-unpushed": ("finished", True)})

    def test_open_pr_cleanup_keeps_a_checkout_another_lane_committed_or_reviewed_in_lately(self) -> None:
        now = datetime.now(timezone.utc)
        hours_ago = (now - timedelta(hours=2)).isoformat()
        head_at = {"committed": (now - timedelta(minutes=5)).isoformat(), "reviewed": hours_ago, "idle": hours_ago}
        rows = [{"path": "/w/" + name, "class": "stalled", "reasons": [scan.NO_SESSION_REASON],
                 "linked_pr": None, "claude": None, "codex": []} for name in head_at]
        numbers = {name: index for index, name in enumerate(head_at, 1)}

        with tempfile.TemporaryDirectory() as reviews, patch.dict(os.environ, {"TRIAGE_REVIEW_DIR": reviews}):
            for name in ("reviewed", "idle"):
                log = Path(reviews) / f"acme-app-{numbers[name]}.log"
                log.write_text("codex review\n")
                if name == "idle":
                    older = now.timestamp() - 7200
                    os.utime(log, (older, older))
            result = {row["path"].rsplit("/", 1)[-1]: row for row in scan.add_next_step_facts(
                rows,
                git_lookup=lambda path: {"branch": path.rsplit("/", 1)[-1], "head": "aaa", "dirty": 0,
                                         "unpushed": 0, "github": "acme/app",
                                         "head_at": head_at[path.rsplit("/", 1)[-1]]},
                pr_lookup=each_ref(lambda slug, ref: {"number": numbers[str(ref)], "state": "OPEN", "head_oid": "aaa"}))}

        self.assertEqual({name: row.get("open_pr_cleanup", False) for name, row in result.items()},
                         {"committed": False, "reviewed": False, "idle": True})
        for name in ("committed", "reviewed"):
            self.assertNotEqual(result[name]["class"], "finished")
            self.assertTrue(any(reason.startswith("別の担当が使用中（") for reason in result[name]["reasons"]))

    def test_open_pr_cleanup_keeps_worktrees_with_keep_notes(self) -> None:
        comments = ("parallel-tests-b6: PR #3568 のレビュー対応中（消さないでください）",
                    "parallel-devenv: ベンチマークの比較に使うので残す")
        for comment in comments:
            with self.subTest(comment=comment):
                row = {"path": "/w/keep", "class": "stalled", "reasons": [scan.NO_SESSION_REASON],
                       "linked_pr": None, "claude": None, "codex": [], "comment": comment}
                result = scan.add_next_step_facts(
                    [row], git_lookup=lambda path: {"branch": "keep", "head": "aaa", "dirty": 0,
                                                   "unpushed": 0, "github": "acme/app"},
                    pr_lookup=lambda slug, refs: {"keep": {"number": 7, "state": "OPEN", "head_oid": "aaa"}})[0]

                self.assertFalse(result.get("open_pr_cleanup", False))
                self.assertNotEqual(result["class"], "finished")
                self.assertIn(comment, " ".join(result["reasons"]))

    def test_open_pr_cleanup_preserves_a_historical_transcript_question(self) -> None:
        rows = [
            {"path": "/w/history", "class": "waiting", "reasons": ["final message asks"],
             "agents": [], "terminals": [], "linked_pr": None, "claude": {"final_text": "merge?"}},
            {"path": "/w/agent", "class": "stalled", "reasons": [scan.NO_SESSION_REASON],
             "agents": [{"type": "codex"}], "terminals": [], "linked_pr": None},
            {"path": "/w/agent-tab", "class": "stalled", "reasons": [scan.NO_SESSION_REASON],
             "agents": [], "terminals": [{"agent": "codex"}], "linked_pr": None},
        ]

        def git_lookup(path: str) -> dict:
            return {"branch": path.rsplit("/", 1)[-1], "head": "aaa", "dirty": 0,
                    "unpushed": 1, "github": "acme/app"}

        def pr_lookup(slug: str, ref: object) -> dict:
            return {"number": 7, "state": "OPEN", "head_oid": "aaa"}

        result = {row["path"].rsplit("/", 1)[-1]: row for row in scan.add_next_step_facts(
            rows, git_lookup=git_lookup, pr_lookup=each_ref(pr_lookup))}

        self.assertEqual(result["history"]["class"], "waiting")
        self.assertTrue(result["history"]["open_pr_cleanup"])
        self.assertNotIn("open_pr_cleanup", result["agent"])
        self.assertNotIn("open_pr_cleanup", result["agent-tab"])

    def test_completed_board_without_agent_still_checks_open_pr_for_cleanup(self) -> None:
        row = {"path": "/w/completed", "class": "finished", "reasons": ["board status completed"],
               "agents": [], "terminals": [{"agent": None, "handle": "shell1"}],
               "meaningful_tabs": ["shell1"], "linked_pr": None}

        result = scan.add_next_step_facts(
            [row],
            git_lookup=lambda path: {"branch": "completed", "head": "aaa", "dirty": 0,
                                     "unpushed": 1, "github": "acme/app"},
            pr_lookup=each_ref(lambda slug, ref: {"number": 7, "state": "OPEN", "head_oid": "aaa"}))[0]

        self.assertEqual(result["class"], "finished")
        self.assertEqual(result["meaningful_tabs"], ["shell1"])
        self.assertTrue(result["open_pr_cleanup"])
        self.assertTrue(result["pr"]["head_matches"])

    def test_the_review_queue_holds_every_row_a_reader_must_class(self) -> None:
        # Jev reads fast and shallow: every row it judged from a final message gets
        # one careful read, and the verdict is kept until that message changes.
        def row(path: str, cls: str, pr: dict | None = None, jev: bool = True, final_at: str = "t1",
                **extra: object) -> dict:
            return {"path": path, "repo": "app", "class": cls, "reasons": ["r"], "comment": "c",
                    "jev": {"asks": 0.5} if jev else None, "pr": pr, "git": {"dirty": 0},
                    "claude": {"last_user": "u", "final_text": "f", "final_at": final_at, "transcript": "/x.jsonl"},
                    "codex": [{"last_assistant": "old", "last_assistant_at": "t0"},
                              {"last_assistant": "new", "last_assistant_at": "t1", "rollout": "/r.jsonl"}],
                    "terminals": [{"handle": "term_1"}], **extra}

        rows = [row("/w/unsure", "unsure"),
                row("/w/merged-but-continues", "stalled", pr={"state": "MERGED", "number": 3}),
                row("/w/blocked-by-jev", "stalled", pr={"state": "OPEN", "number": 4}),
                row("/w/asks-by-jev", "waiting"),
                row("/w/no-message", "stalled", jev=False),
                row("/w/finished", "finished", pr={"state": "MERGED", "number": 5}),
                row("/w/working", "working"),
                row("/w/read-before", "stalled", final_at="t1"),
                row("/w/changed-since", "stalled", final_at="t2")]
        verdicts = {"/w/read-before": {"read": scan.read_key(row("/w/read-before", "stalled")), "class": "blocked"},
                    "/w/changed-since": {"read": scan.read_key(row("/w/changed-since", "stalled")),
                                         "class": "finished"}}

        queue = scan.review_queue(rows, verdicts)

        self.assertEqual([e["path"] for e in queue], ["/w/unsure", "/w/merged-but-continues", "/w/blocked-by-jev",
                                                       "/w/asks-by-jev", "/w/changed-since"])
        self.assertEqual(queue[0], {
            "path": "/w/unsure", "repo": "app", "environment": None, "class": "unsure", "reasons": ["r"],
            "waits_on": None, "comment": "c",
            "jev": {"asks": 0.5}, "pr": None, "git": {"dirty": 0},
            "orca_message": None, "prompt_waiting": None,
            "claude": {"last_user": "u", "final_text": "f", "final_at": "t1", "transcript": "/x.jsonl"},
            "codex": {"last_assistant": "new", "last_assistant_at": "t1", "rollout": "/r.jsonl"},
            "final": {"source": "claude", "at": "t1", "text": "f"}, "final_at": "t1", "read": scan.read_key(rows[0])})

    def test_a_queued_row_carries_its_newest_inbox_reports_and_which_report_came_last(self) -> None:
        # The Orca comment has no time; a done report newer than the final message must reach the reader.
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            write_jsonl(inbox, [
                {"ts": "2026-10-27T12:00:00+0900", "kind": "done", "repo": "app", "worktree": "qa", "text": "古い完了"},
                {"ts": "2026-10-27T13:14:00+0900", "kind": "done", "repo": "app", "worktree": "qa",
                 "text": "復旧と一時 RDS の削除が済みました"},
                {"ts": "2026-10-27T13:20:00+0900", "kind": "info", "repo": "app", "worktree": "qa", "text": "経過"},
                {"ts": "2026-10-27T13:30:00+0900", "kind": "done", "repo": "other", "worktree": "qa", "text": "別の repo"},
            ])
            reports = scan.inbox_reports(inbox, Path(root) / "no-answers.jsonl")
        row = {"path": "/w/app/qa", "repo": "app", "class": "stalled", "reasons": ["r"], "jev": {"asks": 0.1},
               "comment": "一時RDS の利用可能待ち", "pr": None, "git": {"dirty": 0}, "codex": [],
               "claude": {"final_text": "RDS の作成を待っています", "final_at": "2026-10-27T03:00:00Z"}}
        before = {"/w/app/qa": {"read": scan.read_key(row), "class": "in_progress"}}

        [queued] = scan.review_queue(scan.with_inbox(rows=[row], reports=reports), before)

        self.assertEqual(queued["inbox"], {"done": {"at": "2026-10-27T13:14:00+0900", "host": None,
                                                    "text": "復旧と一時 RDS の削除が済みました"}})
        self.assertEqual(queued["newest_report"], {"source": "inbox_done", "at": "2026-10-27T13:14:00+0900"})

    def test_rows_a_reader_classes_get_git_and_pr_facts(self) -> None:
        # A waiting row is read too: its decision marker may be answered by a merge.
        rows = [{"path": "/w/linked", "class": "stalled", "linked_pr": {"number": 7, "state": "open"}},
                {"path": "/w/branch", "class": "stalled", "linked_pr": None},
                {"path": "/w/forgejo", "class": "unsure", "linked_pr": None},
                {"path": "/w/asks", "class": "waiting", "linked_pr": {"number": 8, "state": "open"}},
                {"path": "/w/busy", "class": "working", "linked_pr": {"number": 9, "state": "open"}}]
        gits = {"/w/linked": {"branch": "fix-a", "head": "aaa", "dirty": 0, "unpushed": 0, "github": "acme/app"},
                "/w/branch": {"branch": "fix-b", "head": "bbb", "dirty": 2, "unpushed": 1, "github": "acme/app"},
                "/w/forgejo": {"branch": "fix-c", "head": "ccc", "dirty": 0, "unpushed": 0, "github": None},
                "/w/asks": {"branch": "fix-d", "head": "aaa", "dirty": 0, "unpushed": 0, "github": "acme/app"}}
        asked = []

        def pr_lookup(slug: str, ref: object) -> dict:
            asked.append((slug, ref))
            return {"number": ref, "head_oid": "aaa"}

        result = scan.add_next_step_facts(rows, git_lookup=gits.get, pr_lookup=each_ref(pr_lookup))

        self.assertCountEqual(asked, [("acme/app", 7), ("acme/app", "fix-b"), ("acme/app", 8)])
        self.assertEqual([(r.get("git"), r.get("pr")) for r in result], [
            (gits["/w/linked"], {"number": 7, "head_oid": "aaa", "head_matches": True}),
            (gits["/w/branch"], {"number": "fix-b", "head_oid": "aaa", "head_matches": False}),
            (gits["/w/forgejo"], None),
            (gits["/w/asks"], {"number": 8, "head_oid": "aaa", "head_matches": True}), (None, None)])
        self.assertNotIn("git", rows[0])

    def test_a_repository_timeout_does_not_hold_other_repositories_and_a_duplicate_pr_is_read_once(self) -> None:
        rows = [{"path": path, "class": "unsure", "linked_pr": {"number": number}}
                for path, number in (("/w/slow", 7), ("/w/fast", 8), ("/w/fast-again", 8))]
        gits = {"/w/slow": {"branch": "a", "head": "aaa", "github": "acme/slow"},
                "/w/fast": {"branch": "b", "head": "aaa", "github": "acme/app"},
                "/w/fast-again": {"branch": "c", "head": "aaa", "github": "acme/app"}}
        fast_started = Event()
        calls = []

        def pr_lookup(slug: str, refs: list[object]) -> dict:
            calls.append((slug, refs))
            if slug == "acme/slow":
                parallel = fast_started.wait(2)
                if not parallel:
                    raise AssertionError("the other repository's PR lookup did not start")
                raise subprocess.TimeoutExpired("gh", 15)
            fast_started.set()
            return {8: {"number": 8, "head_oid": "aaa", "state": "OPEN"}}

        result = scan.add_next_step_facts(rows, git_lookup=gits.get, pr_lookup=pr_lookup)

        self.assertEqual([row["path"] for row in result], [row["path"] for row in rows])
        self.assertIsNone(result[0]["pr"])
        self.assertEqual([row["pr"]["number"] for row in result[1:]], [8, 8])
        self.assertCountEqual(calls, [("acme/slow", [7]), ("acme/app", [8])])


class ScanCommandTests(unittest.TestCase):
    def test_open_pr_cleanup_summary_provides_one_batch_command_with_nested_and_scratch_options(self) -> None:
        saved = self.dir / "scan.json"
        saved.write_text(json.dumps([{"path": "/w/app/finished", "repo": "app", "class": "stalled",
                                      "open_pr_cleanup": True, "pr": {"number": 10}, "terminals": [],
                                      "claude": None, "codex": [], "reasons": []}]))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--summary"]), 0)
        command = next(line.strip() for line in out.getvalue().splitlines() if "rm-check-batch" in line)
        args = shlex.split(command)
        self.assertIn("--detach-nested", args)
        self.assertIn("--archive-scratch", args)
        self.assertIn("--remove-ok", args)
        self.assertEqual(args[args.index("--from") + 1], str(saved))
        self.assertIn("/w/app/finished 10", args)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # A host's real Jev key and verdict store must never reach a test run.
        for hermetic in (patch.dict(os.environ, {"TYPESAFE_API_KEY": "", "TRIAGE_INBOX": "",
                                                "TRIAGE_REVIEW_DIR": str(self.dir / "reviews"),
                                                "TRIAGE_ASK_ANSWERS": str(self.dir / "no-answers.jsonl")}),
                         patch.object(scan, "JEV_KEY_FILE", str(self.dir / "no-key")),
                         patch.object(scan, "HOURLY_STATE", self.dir / "no-state.json"),
                         patch.object(scan, "LEAD_DECISIONS", self.dir / "no-decisions.jsonl"),
                         patch.object(scan, "INBOX_FILE", str(self.dir / "no-inbox.jsonl")),
                         patch.object(scan, "VERDICTS_FILE", str(self.dir / "verdicts.json")),
                         patch.object(scan, "PR_ACTIVITY_DIR", str(self.dir / "pr-activity"))):
            hermetic.start()
            self.addCleanup(hermetic.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_expand_roots_accepts_absolute_patterns_with_spaces(self) -> None:
        accounts = self.dir / "Application Support" / "accounts"
        (accounts / "first" / "sessions").mkdir(parents=True)
        (accounts / "first" / "nested" / "sessions").mkdir(parents=True)
        (accounts / "second" / "sessions").mkdir(parents=True)
        (accounts / ".hidden" / "sessions").mkdir(parents=True)
        (accounts / "third").mkdir()
        (accounts / "third" / "sessions").write_text("not a directory")
        pattern = str(accounts / "*" / "sessions")

        self.assertEqual(sorted(scan.expand_roots([pattern])), sorted([
            str(accounts / "first" / "sessions"),
            str(accounts / "second" / "sessions"),
        ]))
        self.assertEqual(sorted(scan.expand_roots([str(accounts / "**" / "sessions")])),
                         sorted(scan.expand_roots([pattern])))

    def test_expand_roots_accepts_literal_current_and_absolute_roots(self) -> None:
        self.assertEqual(scan.expand_roots([".", "./", "/"]), [".", ".", "/"])

    def saved_scan(self) -> tuple[Path, Path]:
        """Rows in the shapes that were misread by hand: null titles, a codex list, a verdict, an Orca message."""
        checkout = self.dir / "dotfiles-73"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                        "git@github.com:Eotel/chezmoi-dotfiles.git"], check=True)
        rows = [
            {"path": "/w/app/fix-login", "repo": "app", "environment": None, "class": "stalled",
             "reasons": ["final message"], "comment": None,
             "terminals": [{"handle": "term_1", "agent": "claude", "title": None}],
             "claude": {"final_text": "PR #10 を出しました。\nmerge してよいですか", "final_at": "2026-10-27T01:00:00Z",
                        "last_user": "直して", "pending_questions": []},
             "codex": [], "git": {"github": "Eotel/app"}, "pr": {"number": 10, "state": "OPEN"},
             "verdict": {"class": "waiting", "question": "PR #10 を\nmerge するか", "waits_on": None,
                         "next_step": None},
             "done_cards": [{"agent": "claude", "handle": "term_1", "interrupted": False,
                             "last_message": "PR #10 を出しました。\nmerge してよいですか",
                             "screen_tail": ["✻ Baked for 3m", "❯"], "draft": False}],
             "closable": ["term_2"], "meaningful_tabs": ["term_1"]},
            {"path": str(checkout), "repo": "dotfiles", "environment": None, "class": "unsure",
             "reasons": [], "comment": "作業中", "terminals": [],
             "claude": {"final_text": "古い発言", "final_at": "2026-10-27T01:00:00Z", "last_user": "古い依頼"},
             "codex": [{"last_assistant": "前の回", "last_assistant_at": "2026-10-27T00:00:00Z",
                        "last_role": "assistant"},
                       {"last_assistant": "設定を反映しました", "last_assistant_at": "2026-10-27T02:00:00Z",
                        "last_role": "assistant", "last_user": "設定を反映して"}]},
            {"path": "/remote/w/app", "repo": "app", "environment": "mbp16", "class": "waiting",
             "reasons": [], "comment": "", "terminals": [{"handle": None, "agent": None, "title": None}],
             "claude": None, "codex": [], "orca_message": {"text": "どちらにしますか", "at": "2026-10-27T03:00:00Z"},
             "done_cards": [{"agent": "codex", "handle": None, "interrupted": True, "last_message": ""}]},
            {"path": "/w/app/fix-login", "repo": "app", "environment": "mbp16", "class": "working",
             "reasons": [], "comment": "", "terminals": [], "claude": None, "codex": []},
        ]
        path = self.dir / "scan.json"
        path.write_text(json.dumps(rows, ensure_ascii=False))
        return path, checkout

    def test_a_done_card_newer_than_the_transcript_is_the_final_message_and_row_lists_cards_in_full(self) -> None:
        # 2026-11-09: montly-report's row showed an 11/02 message on another matter while its card held that day's
        # report, and --row had no done_cards, so reading the card took hand-written Python.
        report, prompt = "今日の報告です。" + "続き" * 200, "月報を出して。" + "詳しく" * 100

        def lane(name: str, said: str, said_at: str, card: str, card_at: str) -> dict:
            return {"path": f"/w/app/{name}", "repo": "app", "environment": None, "class": "unsure",
                    "reasons": [], "comment": "", "terminals": [], "codex": [],
                    "claude": {"final_text": said, "final_at": said_at, "last_user": "go"},
                    "done_cards": [{"agent": "claude", "handle": "term_" + name, "done_at": card_at,
                                    "interrupted": False, "prompt": prompt, "last_message": card}]}
        saved = self.dir / "scan.json"
        rows = [lane("same", "同じ報告", "2026-11-09T01:00:00.400Z", "同じ報告", "2026-11-09T01:00:01Z"),
                lane("report", "11/02 の別件", "2026-11-02T01:00:00Z", report, "2026-11-09T03:00:00Z"),
                lane("older", "新しい発言", "2026-11-09T02:00:00Z", "古い card", "2026-11-09T00:00:00Z")]
        saved.write_text(json.dumps(rows))
        # orca-row-classify reads only the queue, so the queue carries the message its read key stands for.
        self.assertEqual([entry["final"]["text"] for entry in scan.review_queue(rows)], ["同じ報告", report, "新しい発言"])

        def final(name: str) -> dict:
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scan.main(["--from", str(saved), "--row", name]), 0)
            return json.loads(out.getvalue())

        shown = final("report")
        self.assertEqual(shown["final"], {"source": "card", "at": "2026-11-09T03:00:00Z", "text": report})
        self.assertEqual([(card["prompt"], card["last_message"]) for card in shown["done_cards"]],
                         [(prompt, report)])
        self.assertEqual([final(name)["final"]["source"] for name in ("same", "older")], ["claude", "claude"])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--rows", "--sort", "final"]), 0)
        self.assertEqual([line.split(" | ")[0] for line in out.getvalue().splitlines()], ["report", "older", "same"])

    def test_wake_names_each_lane_the_api_stopped_with_its_tab_and_what_it_needs(self) -> None:
        def stopped(path: str, environment: str | None, handle: str, kind: str, action: str, error: str, resets: str | None = None) -> dict:
            return {"path": path, "repo": "app", "environment": environment, "class": "stalled", "reasons": [],
                    "comment": "", "claude": None, "codex": [], "terminals": [{"handle": handle, "agent": "claude"}],
                    "stop": {"kind": kind, "action": action, "agent": "claude", "error": error, "resets": resets}}
        saved = self.dir / "scan.json"
        saved.write_text(json.dumps([
            stopped("/w/app/limit", None, "term_l", "usage_limit", "wait", "You've hit your weekly limit",
                    "Nov 7 at 8pm (Asia/Tokyo)"),
            stopped("/w/app/dns", None, "term_d", "connection", "wake", "API Error: Can't reach the API server"),
            # Orca's copy of another Mac's message names no agent: its tab is the agent's, never a shell's.
            {**stopped("/w/app/login", "mbp16", "term_g", "login", "human", "Login expired · Please run /login"),
             "terminals": [{"handle": "term_sh", "agent": None}, {"handle": "term_g", "agent": "claude"}],
             "stop": {"kind": "login", "action": "human", "agent": None, "error": "Login expired · Please run /login",
                      "resets": None}},
            {**stopped("/w/app/two", "mbp16", "term_a", "connection", "wake", "API Error: Connection lost"),
             "terminals": [{"handle": "term_a", "agent": "claude"}, {"handle": "term_b", "agent": "codex"}],
             "stop": {"kind": "connection", "action": "wake", "agent": None, "error": "API Error: Connection lost",
                      "resets": None}},
            {"path": "/w/app/done", "repo": "app", "environment": None, "class": "finished", "reasons": [],
             "comment": "", "claude": None, "codex": [], "terminals": []},
        ], ensure_ascii=False))

        with patch.object(scan.time, "time", return_value=datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp()), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--wake"]), 0)

        self.assertEqual(out.getvalue().splitlines(), [
            "limit | term_l | 利用上限: Nov 7 at 8pm (Asia/Tokyo) まで待つ | You've hit your weekly limit",
            "dns | term_d | 通信断: 起こす | API Error: Can't reach the API server",
            "mbp16::login | term_g | ログイン切れ: 人が /login する | Login expired · Please run /login",
            "mbp16::two | 不明（term_a, term_b） | 通信断: 起こす | API Error: Connection lost"])

    def test_wake_checks_expired_resets_and_names_tabs_that_need_reopening(self) -> None:
        saved = self.dir / "scan.json"
        stop = {"kind": "usage_limit", "action": "wait", "agent": "claude", "error": "usage limit"}
        saved.write_text(json.dumps([
            {"path": "/w/old", "class": "stalled", "stop": {**stop, "resets": "Nov 7 at 8pm (Asia/Tokyo)"},
             "terminals": [{"handle": "term_a", "agent": "claude"}]},
            {"path": "/w/sleeping", "class": "stalled", "stop": {**stop, "resets": "2026-11-08T20:00:00+09:00"}},
            {"path": "/w/future", "class": "stalled", "stop": {**stop, "resets": "2026-11-10T20:00:00+09:00"}},
            {"path": "/w/unknown", "class": "stalled", "stop": {**stop, "resets": "sometime"}},
        ]))
        with patch.object(scan.time, "time", return_value=datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp()), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--wake"]), 0)
        lines = out.getvalue().splitlines()
        self.assertIn("解除済み・起こせる", lines[0])
        self.assertIn("開き直し", lines[1])
        self.assertIn("まで待つ", lines[2])
        self.assertIn("まで待つ", lines[3])

    def test_wake_honors_the_latest_answer_across_a_rows_wake_keys(self) -> None:
        import lead_decisions

        saved = self.dir / "scan.json"
        saved.write_text(json.dumps([{"path": "/w/app/limit", "repo": "app", "class": "stalled",
                                      "stop": {"kind": "usage_limit", "action": "wait", "agent": "claude",
                                               "error": "usage limit", "resets": "2020-01-01T00:00:00Z"}}]))
        log = self.dir / "decisions.jsonl"
        lead_decisions.append(log,
            lead_decisions.Decision("old", "wake:limit::first", "起こす", "2026-11-11T10:00:00+09:00"),
            lead_decisions.Decision("new", "wake:app/limit::weekly-limit", "起こさない。返答待ち", "2026-11-12T11:00:00+09:00"),
            lead_decisions.Decision("other", "wake:limit-other::later", "起こす", "2026-11-13T10:00:00+09:00"))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--wake", "--decisions-file", str(log)]), 0)
        self.assertIn("2026-11-12T11:00:00+09:00 起こさない。返答待ち", out.getvalue())
        self.assertNotIn("起こせる", out.getvalue())

        lead_decisions.append(log, lead_decisions.Decision(
            "resume", "wake:/w/app/limit::resume", "起こす", "2026-11-13T11:00:00+09:00"))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--wake", "--decisions-file", str(log)]), 0)
        self.assertIn("起こせる", out.getvalue())

    def test_wake_selects_the_tab_running_the_stopped_claude_transcript(self) -> None:
        saved = self.dir / "scan.json"
        saved.write_text(json.dumps([{"path": "/w/app/stopped", "class": "stalled",
            "claude": {"transcript": "/projects/w/stopped-session.jsonl"},
            "stop": {"kind": "connection", "action": "wake", "agent": "claude",
                     "error": "API Error: Connection lost", "resets": None},
            "terminals": [{"handle": handle, "agent": "claude"} for handle in ("term_a", "term_b", "term_c")]}]))
        sessions = self.dir / "sessions"
        sessions.mkdir()
        write_jsonl(sessions / "20.json", [{"pid": 20, "sessionId": "stopped-session", "cwd": "/elsewhere"}])
        write_jsonl(sessions / "30.json", [{"pid": 30, "sessionId": "another-session", "cwd": "/w/app/stopped"}])
        with patch.object(scan, "claude_session_dirs", return_value=[sessions]), \
                patch.object(scan, "process_table", return_value={20: (10, "claude"), 10: (1, "zsh"), 30: (1, "claude")}), \
                patch.object(scan.rm_check, "process_handles", return_value={10: "term_b", 30: "term_c"}), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--wake"]), 0)
        self.assertIn("stopped | term_b |", out.getvalue())
        self.assertNotIn("不明", out.getvalue())

    def test_time_only_reset_stays_on_the_date_implied_by_the_failure(self) -> None:
        saved = self.dir / "scan.json"
        saved.write_text(json.dumps([{"path": "/w/limit", "class": "stalled", "claude": {
            "final_at": "2026-11-08T14:00:00Z", "final_text": "usage limit"},
            "stop": {"kind": "usage_limit", "action": "wait", "agent": "claude", "error": "usage limit",
                     "resets": "2am (Asia/Tokyo)"}}]))
        for moment, expired in (("2026-11-08T14:30:00+00:00", False),
                                ("2026-11-08T18:00:00+00:00", True),
                                ("2026-11-09T16:00:00+00:00", True)):
            with self.subTest(moment=moment), patch.object(scan.time, "time", return_value=datetime.fromisoformat(moment).timestamp()), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scan.main(["--from", str(saved), "--wake"]), 0)
            self.assertEqual("解除済み・起こせる" in out.getvalue(), expired)


    def test_summary_names_each_missing_hook_file_with_its_host(self) -> None:
        saved, _ = self.saved_scan()
        rows = json.loads(saved.read_text())
        rows[2]["hook_errors"] = [{"host": "mbp16", "path": "/Users/me/.claude/hooks/git-hook-guard.sh",
                                   "tab": "term_9"}]
        saved.write_text(json.dumps(rows, ensure_ascii=False))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--summary"]), 0)

        self.assertIn("hook errors: 1\n  mbp16 /Users/me/.claude/hooks/git-hook-guard.sh (mbp16::app term_9)",
                      out.getvalue())

    def test_rows_print_one_line_per_saved_row_without_rescanning(self) -> None:
        saved, checkout = self.saved_scan()
        with patch.object(scan, "collect_worktrees_and_terminals", side_effect=AssertionError("rescanned")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--rows"]), 0)

        self.assertEqual(out.getvalue().splitlines(), [
            "fix-login | waiting (scan: stalled) | Eotel/app#10 OPEN | tabs 1 | 2026-10-27T01:00:00Z"
            " | PR #10 を merge するか | reasons final message | git.dirty null | git.unpushed null",
            "dotfiles-73 | unsure | Eotel/chezmoi-dotfiles | tabs 0 | 2026-10-27T02:00:00Z | 設定を反映しました"
            " | reasons - | git.dirty null | git.unpushed null",
            "mbp16::app | waiting | - | tabs 1 | 2026-10-27T03:00:00Z | どちらにしますか"
            " | reasons - | git.dirty null | git.unpushed null",
            "mbp16::fix-login | working | - | tabs 0 | - | "
            " | reasons - | git.dirty null | git.unpushed null",
        ])

    def test_rows_sort_final_puts_newest_messages_first_and_missing_times_last(self) -> None:
        saved, _ = self.saved_scan()
        with redirect_stdout(io.StringIO()) as out:
            code = scan.main(["--from", str(saved), "--rows", "--sort", "final"])
        self.assertEqual(code, 0)
        self.assertEqual([line.split(" | ")[0] for line in out.getvalue().splitlines()],
                         ["mbp16::app", "dotfiles-73", "fix-login", "mbp16::fix-login"])

    def test_cleanup_excludes_undelivered_destinations_and_rows_with_removal_answers(self) -> None:
        saved = self.dir / "scan.json"
        state, decisions = self.dir / "state.json", self.dir / "decisions.jsonl"
        rows = [{"path": f"/w/{name}", "repo": "app", "class": "finished", "terminals": [], "reasons": [],
                 "closable": [f"term_{name}"], "meaningful_tabs": [], "claude": None, "codex": []}
                for name in ("promise", "named", "absolute", "remote", "done", "local")]
        rows[3]["environment"] = "mbp16"
        saved.write_text(json.dumps(rows))
        state.write_text(json.dumps({"handoffs": {
            "h": {"to": "app/promise", "delivered_at": None, "task": "結果を渡す"},
            "d": {"to": "app/done", "delivered_at": "2026-11-09T01:00:00Z", "task": "済み"}}}))
        write_jsonl(decisions, [{"id": str(i), "decision": key, "answer": "結果が出るまで残す",
                                "decided_at": "2026-11-09T01:00:00Z"}
                               for i, key in enumerate(("remove:app/named", "remove:/w/absolute",
                                                        "remove:mbp16::/w/remote", "remove:local::/w/local"))])
        opts = ["--from", str(saved), "--state-file", str(state), "--decisions-file", str(decisions)]
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main([*opts, "--rm-specs"]), 0)
        self.assertEqual(out.getvalue().strip(), "/w/done -")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(opts), 0)
        shown = json.loads(out.getvalue())
        self.assertNotEqual(shown[0]["class"], "finished")
        self.assertEqual(shown[0]["closable"], [])
        self.assertEqual(shown[1]["removal_decision"]["answer"], "結果が出るまで残す")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main([*opts, "--summary"]), 0)
        self.assertIn("結果が出るまで残す", out.getvalue())
        self.assertIn("2026-11-09T01:00:00Z", out.getvalue())

    def test_pending_delivery_preserves_a_waiting_verdict_and_its_question(self) -> None:
        saved, state = self.dir / "scan.json", self.dir / "state.json"
        saved.write_text(json.dumps([{"path": "/w/promise", "repo": "app", "class": "unsure", "reasons": [],
            "claude": None, "codex": [], "terminals": [], "closable": ["t"],
            "verdict": {"class": "waiting", "question": "公開しますか？"}}]))
        state.write_text(json.dumps({"handoffs": {"h": {"to": "app/promise", "delivered_at": None}}}))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--state-file", str(state),
                                         "--rows", "--class", "waiting"]), 0)
        self.assertIn("公開しますか？", out.getvalue())
        self.assertIn("waiting (scan: unsure)", out.getvalue())

    def test_rows_class_filter_uses_the_verdict_and_keeps_dirty_and_unpushed_values(self) -> None:
        saved, _ = self.saved_scan()
        rows = json.loads(saved.read_text())
        rows[0]["git"].update({"dirty": 0, "unpushed": 0})
        rows[2]["git"] = {"dirty": 2, "unpushed": 3}
        rows[2]["reasons"] = ["a question\non screen", "needs a reply"]
        saved.write_text(json.dumps(rows))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--rows", "--class", "waiting"]), 0)

        lines = out.getvalue().splitlines()
        self.assertEqual([line.split(" | ")[0] for line in lines], ["fix-login", "mbp16::app"])
        self.assertIn("git.dirty 0 | git.unpushed 0", lines[0])
        self.assertIn("reasons a question on screen; needs a reply | git.dirty 2 | git.unpushed 3", lines[1])

    def test_cards_print_each_done_card_and_tab_on_its_own_line_named_by_the_worktree(self) -> None:
        saved, _ = self.saved_scan()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--cards"]), 0)

        # 2026-11-11: the hourly tab passed a card's handle to `orca terminal read` as a positional argument and got
        # "Unknown command"; the card and meaningful lines now carry the command.
        self.assertEqual(out.getvalue().splitlines(), [
            "fix-login | waiting (scan: stalled) | card term_1 claude | read: orca terminal read --terminal term_1"
            " | draft - | composer unknown | PR #10 を出しました。 merge してよいですか | screen: ✻ Baked for 3m ⏎ ❯",
            "fix-login | waiting (scan: stalled) | closable term_2",
            "fix-login | waiting (scan: stalled) | meaningful term_1 | read: orca terminal read --terminal term_1",
            f"fix-login | waiting (scan: stalled) | close: {SCRIPTS / 'close_tabs.py'} --from {saved} term_2",
            "mbp16::app | waiting | card - codex interrupted | draft unknown | composer unknown | - | screen: -",
        ])

    def test_cards_show_the_start_of_the_draft_beside_whether_the_screen_composer_is_empty(self) -> None:
        saved, _ = self.saved_scan()
        rows = json.loads(saved.read_text())
        suggestion = "The next step isn't obvious, so I'll leave this one blank. " + "x" * 20
        excerpt = scan.draft_excerpt({"draft": suggestion})
        rows[0]["done_cards"] = [{"handle": "suggestion", "draft": True, "draft_text": excerpt, "composer_empty": True},
                                 {"handle": "saved-before", "draft": True}]
        saved.write_text(json.dumps(rows))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--cards"]), 0)

        self.assertEqual(excerpt, suggestion[:60] + "…")
        self.assertIn("draft " + excerpt + " | composer empty", out.getvalue().splitlines()[0])
        self.assertIn("draft yes | composer unknown", out.getvalue().splitlines()[1])

    def test_waiting_worktrees_and_remote_shells_missing_output_get_screens(self) -> None:
        waiting = worktree("/w/waiting", "waiting")
        working = worktree("/w/working", "working")
        remote: dict = {**worktree("/remote/working", "working"), "_environment": "macbook-pro-16"}
        remote["agents"][0]["lastAssistantMessage"] = "Saved session memory."
        terms = [terminal(waiting["path"], "claude", "waiting"),
                 terminal(working["path"], "claude", "working"),
                 terminal(working["path"], None, "local_shell"),
                 {**terminal(remote["path"], None, "remote_shell"), "lastOutputAt": None,
                  "_environment": "macbook-pro-16"},
                 {**terminal(remote["path"], None, "remote_preview", preview=PROMPT), "lastOutputAt": None,
                  "_environment": "macbook-pro-16"},
                 {**terminal(remote["path"], "claude", "remote_agent"), "lastOutputAt": None,
                  "_environment": "macbook-pro-16"}]

        self.assertEqual([term["handle"] for term in scan.screen_targets([waiting, working, remote], terms)],
                         ["waiting", "remote_shell"])

    def test_row_prints_one_row_with_its_newest_final_message_and_plain_strings(self) -> None:
        saved, checkout = self.saved_scan()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--row", "dotfiles-73"]), 0)
        row = json.loads(out.getvalue())
        self.assertEqual((row["path"], row["github"], row["class"], row["comment"]),
                         (str(checkout), "Eotel/chezmoi-dotfiles", "unsure", "作業中"))
        self.assertEqual(row["final"], {"source": "codex", "at": "2026-10-27T02:00:00Z",
                                        "text": "設定を反映しました"})
        self.assertEqual(row["last_user"], "設定を反映して")
        self.assertEqual(row["transcripts"], {"claude": [], "codex": []})
        with redirect_stdout(io.StringIO()) as help_text, self.assertRaises(SystemExit) as stopped:
            scan.parse_args(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertTrue(set(row).issubset(set(re.findall(r"\b\w+\b", help_text.getvalue()))))
        self.assertNotIn("row_identity", row)

        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--row", "/w/app/fix-login"]), 0)
        row = json.loads(out.getvalue())
        self.assertEqual((row["identity"], row["class"], row["question"], row["comment"], row["tabs"]),
                         ("/w/app/fix-login", "waiting", "PR #10 を\nmerge するか", "",
                          [{"handle": "term_1", "agent": "claude", "title": ""}]))
        self.assertEqual(row["final"]["text"], "PR #10 を出しました。\nmerge してよいですか")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(scan.main(["--from", str(saved), "--row", "nothing"]), 1)
        self.assertIn("nothing", err.getvalue())
        self.assertIn("mbp16::app", err.getvalue())

    def test_row_accepts_summary_names_and_unique_remote_names_and_lists_ambiguous_candidates(self) -> None:
        saved, checkout = self.saved_scan()
        for name, path, environment in (("dotfiles/dotfiles-73", str(checkout), ""),
                                         ("app", "/remote/w/app", "mbp16"),
                                         ("mbp16::app/fix-login", "/w/app/fix-login", "mbp16")):
            with self.subTest(name=name), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scan.main(["--from", str(saved), "--row", name]), 0)
                shown = json.loads(out.getvalue())
                self.assertEqual((shown["path"], shown["environment"]), (path, environment))
        for name in ("fix-login", "app/fix-login"):
            with self.subTest(name=name), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(scan.main(["--from", str(saved), "--row", name]), 1)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("mbp16::fix-login", err.getvalue())
                self.assertIn("candidates:", err.getvalue())

    def test_row_lists_both_agents_transcript_paths(self) -> None:
        saved, _ = self.saved_scan()
        rows = json.loads(saved.read_text())
        claude = self.dir / "claude.jsonl"
        rollouts = [self.dir / "rollout-1.jsonl", self.dir / "rollout-2.jsonl"]
        rows[0]["claude"]["transcript"] = str(claude)
        rows[0]["codex"] = [{"rollout": str(path)} for path in rollouts]
        saved.write_text(json.dumps(rows))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--row", "/w/app/fix-login"]), 0)
        self.assertEqual(json.loads(out.getvalue())["transcripts"],
                         {"claude": [str(claude)], "codex": [str(path) for path in rollouts]})
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--row", "/w/app/fix-login", "--transcripts"]), 0)
        self.assertEqual(out.getvalue().splitlines(),
                         [f"claude {claude}"] + [f"codex {path}" for path in rollouts])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            scan.parse_args(["--from", str(saved), "--transcripts"])
        self.assertEqual(stopped.exception.code, 2)

    def test_row_shows_the_completed_pr_review_beside_the_merge_question(self) -> None:
        saved, _ = self.saved_scan()
        rows = json.loads(saved.read_text())
        rows[0]["pr"]["head_oid"] = "abcdef123" + "0" * 31
        saved.write_text(json.dumps(rows))
        directory = self.dir / "reviews"
        directory.mkdir()
        review = directory / "Eotel-app-10.md"
        stamp = review.with_suffix(".reviewed.json")
        verdicts = ["**判定：merge 可（Approve）**", "**直してから（Request changes）**。対象 head abcdef123"]
        for verdict in verdicts:
            for head in ("abcdef123", "987654321"):
                with self.subTest(verdict=verdict, head=head):
                    review.write_text("\n\n" + verdict + "\n\n本文\n")
                    stamp.write_text(json.dumps({"head": head, "kind": "cloud",
                                                 "sha256": hashlib.sha256(review.read_bytes()).hexdigest()}))
                    with redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(scan.main(["--from", str(saved), "--row", "/w/app/fix-login"]), 0)
                    row = json.loads(out.getvalue())
                    self.assertTrue(row["merge_question"])
                    self.assertEqual(row["review"], {"head": head, "verdict": verdict, "path": str(review),
                                                     "head_matches": head == "abcdef123",
                                                     "changes_requested": head == "abcdef123" and "直してから" in verdict})

        rows[0]["linked_pr"] = rows[0].pop("pr")
        rows[0]["linked_pr"].pop("head_oid")
        rows[0].pop("verdict")
        rows[0]["comment"] = "要判断: PR #10をmergeするか"
        saved.write_text(json.dumps(rows))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--row", "/w/app/fix-login"]), 0)
        row = json.loads(out.getvalue())
        self.assertTrue(row["merge_question"])
        self.assertIsNone(row["review"]["head_matches"])
        self.assertFalse(row["review"]["changes_requested"])

        review.write_text("**判定：直してから（Request changes）**\n書きかけ")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--row", "/w/app/fix-login"]), 0)
        self.assertIsNone(json.loads(out.getvalue())["review"])

    def test_cleanup_views_print_rm_specs_and_executable_rm_checks_using_verdicts_by_path(self) -> None:
        saved, _ = self.saved_scan()
        rows = json.loads(saved.read_text())
        rows[0]["open_pr_cleanup"] = True
        rows[1]["class"] = "unsure"
        rows[1]["meaningful_tabs"] = ["keep"]
        rows[2]["class"] = "finished"
        rows[2]["meaningful_tabs"] = []
        rows[3]["class"] = "unsure"
        rows[3]["orca_message"] = {"text": "finished on another Mac", "at": "2026-10-27T04:00:00Z"}
        rows[3]["linked_pr"] = {"number": 12}
        rows[3]["meaningful_tabs"] = []
        saved.write_text(json.dumps(rows))
        store = Path(scan.VERDICTS_FILE)
        store.write_text(json.dumps({
            rows[1]["path"]: {"class": "finished", "read": scan.read_key(rows[1])},
            scan.row_identity(rows[3]): {"class": "finished", "read": scan.read_key(rows[3])}}))
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(scan.main(["--from", str(saved), "--rm-specs"]), 0)
        self.assertEqual(out.getvalue().splitlines(), ["/w/app/fix-login 10"])
        self.assertIn("2 rows on another Mac left out", err.getvalue())

        for view in ("--summary", "--cards"):
            with self.subTest(view=view), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scan.main(["--from", str(saved), view]), 0)
                for row in rows:
                    number = (row.get("pr") or row.get("linked_pr") or {}).get("number")
                    if view == "--summary" and row.get("open_pr_cleanup"):
                        expected = [str(scan.RM_CHECK_BATCH), "--from", str(saved.absolute()),
                                    "--detach-nested", "--archive-scratch", "--remove-ok", f"{row['path']} {number or '-'}"]
                    elif row.get("environment"):
                        expected = [str(scan.RM_CHECK_BATCH), "--host", row["environment"], f"{row['path']} {number or '-'}"]
                    else:
                        expected = [str(SCRIPTS / "rm_check.py"), "--worktree", row["path"],
                                    *(["--pr", str(number)] if number else []), "--from", str(saved.absolute())]
                    self.assertIn(shlex.join(expected), out.getvalue())

    def test_rm_specs_for_named_rows_include_finished_rows_whose_tabs_remain(self) -> None:
        # 2026-11-11: --rm-specs gave 2 of the 8 rows to check; the hourly tab built the other 6 specs by hand, and
        # a zsh loop handed each to rm_check.py without its PR ("--pr: expected one argument").
        def row(name: str, klass: str, **fields: object) -> dict:
            return {"path": f"/w/app/{name}", "repo": "app", "environment": None, "class": klass, "reasons": [],
                    "terminals": [], "claude": None, "codex": [], "meaningful_tabs": [f"term_{name}"], **fields}
        saved = self.dir / "scan.json"
        saved.write_text(json.dumps([row("tabs", "finished", pr={"number": 7}), row("busy", "stalled"),
                                     row("far", "finished", environment="mbp16")]))

        def specs(*names: str) -> tuple[int, str, str]:
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                code = scan.main(["--from", str(saved), "--rm-specs", *(f"--row={name}" for name in names)])
            return code, out.getvalue(), err.getvalue()

        self.assertEqual(specs("tabs", "app/tabs"), (0, "/w/app/tabs 7\n", ""))
        code, out, err = specs("tabs", "busy", "mbp16::far")
        self.assertEqual((code, out), (1, "/w/app/tabs 7\n"))
        self.assertIn("busy: left out: class stalled is neither finished nor open PR cleanup", err)
        self.assertIn(f"mbp16::far: left out: on another Mac: {scan.RM_CHECK_BATCH} --host mbp16 '/w/app/far -'", err)
        self.assertEqual(specs("tabs", "nothing")[:2], (1, ""))

    def test_summary_counts_the_class_rows_class_keeps_beside_the_scans_count(self) -> None:
        # 2026-11-11: --summary said unsure=22, and --rows --class unsure printed nothing: saved verdicts had
        # classed those rows.
        saved, _ = self.saved_scan()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--summary"]), 0)
        self.assertEqual(out.getvalue().splitlines()[0],
                         "classes: stalled=0 (scan: 1), unsure=1, waiting=2 (scan: 1), working=1")

    def test_automation_workspaces_are_left_out_even_with_removal_approval(self) -> None:
        rows = [{"path": "/w/app/scheduled", "repo": "app", "class": "finished", "open_pr_cleanup": True,
                 "terminals": [], "meaningful_tabs": [], "removal_decision": {
                     "answer": "消してよい", "decided_at": "2026-11-14T00:00:00Z"}}]
        payload = {"automations": [
            {"name": "Daily review", "workspaceMode": "existing", "enabled": False,
             "workspaceId": "repo-id::/w/app/scheduled"},
            {"name": "New workspace", "workspaceMode": "new_per_run", "workspaceId": "repo-id::/w/app/scheduled"}]}
        with patch.object(hourly_runtime, "orca_result", return_value=payload):
            rows = scan.with_automation_workspaces(rows, "orca")
        saved = self.dir / "scan.json"
        saved.write_text(json.dumps(rows))
        for named in ([], ["--row=scheduled"]):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                code = scan.main(["--from", str(saved), "--rm-specs", *named])
            self.assertEqual(code, int(bool(named)))
            self.assertEqual(out.getvalue(), "")
            self.assertIn("自動実行『Daily review』の作業場", err.getvalue())
            self.assertNotIn("New workspace", err.getvalue())

    def test_removal_specs_honor_approval_but_keep_retained_rows_and_print_scan_values(self) -> None:
        saved = self.dir / "scan.json"
        decisions = self.dir / "decisions.jsonl"
        names = ("approved", "keep", "defer", "conditional", "negated", "not-ready", "after", "unknown")
        rows = [{"path": f"/w/app/{name}", "repo": "app", "class": "unsure", "meaningful_tabs": ["term_idle"]}
                for name in names]
        saved.write_text(json.dumps(rows))
        write_jsonl(decisions, [{"id": name, "decision": f"remove:local::/w/app/{name}", "answer": answer,
                                "decided_at": "2026-11-14T00:00:00Z"}
                               for name, answer in (("approved", "指揮: rm_check が ok なら作業場を消してよい"),
                                                    ("keep", "結果が出るまで残す。その後に消す"),
                                                    ("defer", "保留。使っている人が終えたら消してよい"),
                                                    ("conditional", "PR が merge されたら消す"),
                                                    ("negated", "削除するな"),
                                                    ("not-ready", "not ready to remove"),
                                                    ("after", "remove it after the experiment finishes"))])
        for named in ([], [f"--row={name}" for name in names]):
            with self.subTest(named=bool(named)), redirect_stdout(io.StringIO()) as out, \
                    redirect_stderr(io.StringIO()) as err:
                code = scan.main(["--from", str(saved), "--decisions-file", str(decisions), "--rm-specs", *named])
            self.assertEqual(code, int(bool(named)))
            self.assertEqual(out.getvalue(), "/w/app/approved -\n")
            for expected in ("class=unsure", "open_pr_cleanup=False", "removal_decision=", "結果が出るまで残す", "保留"):
                self.assertIn(expected, err.getvalue())

    def test_scan_includes_paired_mac_and_keeps_local_rows_if_it_is_unreachable(self) -> None:
        local = {"worktrees": [worktree("/local/w", "done")],
                 "hostScope": {"omittedHostSelectors": [
                     {"hostId": "remote-1", "selector": "--environment macbook-pro-16"}]}}
        remote = {"worktrees": [worktree("/remote/enhancements-9-mbp16", "done", agent="codex")]}
        environments = [{"id": "remote-1", "name": "macbook-pro-16",
                         "preferredEndpointId": "ws-1", "endpoints": [
                             {"id": "ws-1", "endpoint": "ws://remote.example:6768"}]}]
        calls = []

        def payload(orca: str, args: list[str], json_file: str | None = None, timeout: float | None = None) -> dict:
            calls.append(args)
            if args == ["environment", "list"]:
                return {"environments": environments}
            return remote if "--environment" in args else local

        with patch.object(scan, "orca_payload", side_effect=payload), \
                patch.object(scan, "orca_result", return_value=[]):
            trees, terminals = scan.collect_worktrees_and_terminals("orca")
        self.assertEqual([w["path"] for w in trees], ["/local/w", "/remote/enhancements-9-mbp16"])
        self.assertEqual(trees[1]["_environment"], "macbook-pro-16")
        self.assertEqual(terminals, [])
        self.assertIn(["worktree", "ps", "--environment", "macbook-pro-16"], calls)

        with patch.object(scan, "orca_payload", side_effect=[local, subprocess.CalledProcessError(1, "orca")]), \
                patch.object(scan, "orca_result", side_effect=[[], environments]), \
                patch.object(scan.socket, "create_connection", side_effect=ConnectionRefusedError("refused")), \
                redirect_stderr(io.StringIO()) as err:
            trees, _ = scan.collect_worktrees_and_terminals("orca")
        self.assertEqual([w["path"] for w in trees], ["/local/w"])
        self.assertIn("macbook-pro-16", err.getvalue())
        self.assertIn("ws://remote.example:6768", err.getvalue())
        self.assertIn("TCP port 6768 reachable from this Mac: no", err.getvalue())

        with patch.object(scan, "orca_payload", side_effect=payload), \
                patch.object(scan, "orca_result", side_effect=[[], environments,
                    subprocess.CalledProcessError(1, "orca")]), \
                patch.object(scan.socket, "create_connection", side_effect=ConnectionRefusedError("refused")), \
                redirect_stderr(io.StringIO()) as err:
            scan.collect_worktrees_and_terminals("orca")
        self.assertIn("cannot read terminals", err.getvalue())
        self.assertIn("ws://remote.example:6768", err.getvalue())

    def test_screens_are_read_from_the_server_that_issued_each_handle(self) -> None:
        calls = []

        def orca(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stderr="", stdout=json.dumps(
                {"result": {"terminal": {"source": "screen", "tail": [cmd[cmd.index("--terminal") + 1]]}}}))

        terms = [{"handle": "term_l"}, {"handle": "term_r", "_environment": "macbook-pro-16"}]
        with patch.object(scan.subprocess, "run", side_effect=orca):
            screens = scan.read_screens("orca", terms)

        self.assertEqual(screens, {(None, "term_l"): {"source": "screen", "tail": ["term_l"]},
                                   ("macbook-pro-16", "term_r"): {"source": "screen", "tail": ["term_r"]}})
        self.assertIn(["orca", "terminal", "read", "--terminal", "term_r", "--screen", "--environment",
                       "macbook-pro-16", "--json"], calls)

    def test_removed_host_id_is_named_when_no_environment_selector_exists(self) -> None:
        local = {"worktrees": [], "hostScope": {"omittedHostSelectors": [
            {"hostId": "runtime:deleted-id", "selector": None}]}}
        with patch.object(scan, "orca_payload", return_value=local), \
                patch.object(scan, "orca_result", side_effect=[[], []]), \
                redirect_stderr(io.StringIO()) as err:
            scan.collect_worktrees_and_terminals("orca")
        self.assertIn("runtime:deleted-id", err.getvalue())
        self.assertIn("not in orca environment list", err.getvalue())

    def test_remote_orca_final_message_is_classified_without_a_local_transcript(self) -> None:
        remote: dict = {**worktree("/remote/w", "done", agent="codex"), "_environment": "macbook-pro-16"}
        remote["agents"][0].update({"lastAssistantMessage": "PR を出しました。レビュー待ちです。",
                                     "updatedAt": 1792567646152})
        term = {**terminal("/remote/w", "codex"), "_environment": "macbook-pro-16"}
        rows = scan.build_inventory([remote], [term], str(self.dir / "claude"), {}, "要判断",
                                    judge=lambda message: verdict(0.1, "blocked_on_others"))
        self.assertEqual(rows[0]["class"], "stalled")
        self.assertEqual(rows[0]["jev"]["source"], "orca")
        self.assertEqual(rows[0]["environment"], "macbook-pro-16")

    def test_reads_orca_json_files_and_skips_old_rollouts(self) -> None:
        ps = self.dir / "ps.json"
        ps.write_text(json.dumps({"result": {"worktrees": [worktree("/w/new", "working", agent="codex"),
                                                           worktree("/w/old", None)]}}))
        terms = self.dir / "terms.json"
        terms.write_text(json.dumps({"result": {"terminals": [terminal("/w/new", "codex"),
                                                              terminal("/w/old", "codex", "term_2")]}}))
        sessions = self.dir / "sessions"
        write_jsonl(sessions / "rollout-new.jsonl", [codex_row("t", "session_meta", {"cwd": "/w/new"})])
        old = sessions / "rollout-old.jsonl"
        write_jsonl(old, [codex_row("t", "session_meta", {"cwd": "/w/old"})])
        two_days_ago = old.stat().st_mtime - 48 * 3600
        os.utime(old, (two_days_ago, two_days_ago))

        out = io.StringIO()
        with redirect_stdout(out):
            code = scan.main(["--ps-json", str(ps), "--terminals-json", str(terms),
                              "--claude-projects", str(self.dir / "claude"),
                              "--codex-sessions", str(sessions), "--since-hours", "24"])

        self.assertEqual(code, 0)
        rows = {r["path"]: r["class"] for r in json.loads(out.getvalue())}
        self.assertEqual(rows, {"/w/new": "working", "/w/old": "unstarted"})


    def test_each_row_carries_when_the_scan_started_for_the_tool_that_closes_its_tabs(self) -> None:
        ps = self.dir / "ps.json"
        ps.write_text(json.dumps({"result": {"worktrees": [worktree("/w/new", "working", agent="codex")]}}))
        terms = self.dir / "terms.json"
        terms.write_text(json.dumps({"result": {"terminals": [terminal("/w/new", "codex")]}}))
        before = datetime.now().astimezone()

        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            scan.main(["--ps-json", str(ps), "--terminals-json", str(terms), "--claude-projects",
                       str(self.dir / "claude"), "--codex-sessions", str(self.dir / "none")])

        started = datetime.fromisoformat(json.loads(out.getvalue())[0]["scanned_at"])
        self.assertTrue(before.replace(microsecond=0) <= started <= datetime.now().astimezone())

    def test_writes_the_review_queue_when_asked(self) -> None:
        ps = self.dir / "ps.json"
        ps.write_text(json.dumps({"result": {"worktrees": [worktree("/w/idle", "done")]}}))
        terms = self.dir / "terms.json"
        terms.write_text(json.dumps({"result": {"terminals": [terminal("/w/idle", "claude")]}}))
        claude = self.dir / "claude"
        write_jsonl(scan.claude_project_dir(claude, "/w/idle") / "s.jsonl",
                    [user("go"), assistant(text("PR を出しました。"))])
        queue = self.dir / "triage" / "review-queue.json"

        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch.dict(os.environ, env, clear=True):
            scan.main(["--ps-json", str(ps), "--terminals-json", str(terms), "--claude-projects", str(claude),
                       "--codex-sessions", str(self.dir / "none"), "--jev-key-file", str(self.dir / "no-key"),
                       "--review-out", str(queue)])

        written = json.loads(queue.read_text())
        self.assertEqual([r["path"] for r in written["rows"]], ["/w/idle"])
        self.assertEqual(written["rows"][0]["claude"]["final_text"], "PR を出しました。")
        self.assertTrue(written["generated_at"])

    def scan_offline(self, worktrees: list[dict], *extra: str) -> tuple[int, str, Path]:
        """Scan these Orca worktrees with --review-out; the exit code, stdout, and the queue's path."""
        ps, terms = self.dir / "ps.json", self.dir / "terms.json"
        ps.write_text(json.dumps({"result": {"worktrees": worktrees}}))
        terms.write_text(json.dumps({"result": {"terminals": []}}))
        queue = self.dir / "triage" / "review-queue.json"
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = scan.main(["--ps-json", str(ps), "--terminals-json", str(terms),
                              "--claude-projects", str(self.dir / "claude"), "--codex-sessions", str(self.dir / "none"),
                              "--review-out", str(queue), *extra])
        return code, out.getvalue(), queue

    def test_a_scan_that_reached_no_repository_on_github_stops_without_a_review_queue(self) -> None:
        # 11/10 08:58: DNS failed after the Mac woke, every repository's PRs were missing, and the scan exited 0.
        def remote(name: str, repo: str) -> dict:
            return {**worktree(f"/remote/{name}", "done", pr={"number": 5, "url": f"https://github.com/{repo}/pull/5"}),
                    "_environment": "mbp16",
                    "agents": [{"agentType": "claude", "state": "done", "lastAssistantMessage": "PR を出しました。"}]}

        rows = [remote("a", "acme/app"), remote("b", "acme/lib")]
        offline = subprocess.CompletedProcess([], 1, "", 'Post "https://api.github.com/graphql": proxyconnect tcp: '
                                                      "dial tcp 127.0.0.1:9: connect: connection refused")
        github = FakeGitHub({"acme/app": [pr_node(5)]})

        def one_repo_reached(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return offline if 'name: "lib"' in " ".join(argv) else github(argv, **kwargs)

        with patch.object(scan.subprocess, "run", return_value=offline), patch.object(scan.time, "sleep"):
            code, out, queue = self.scan_offline(rows)
        self.assertNotEqual(code, 0)
        self.assertTrue(out.startswith("ネットワーク断"), out)
        self.assertFalse(queue.exists())

        with patch.object(scan.subprocess, "run", side_effect=one_repo_reached), patch.object(scan.time, "sleep"):
            code, _, queue = self.scan_offline(rows)
        self.assertEqual(code, 0)
        self.assertTrue(queue.exists())

    def test_a_github_answer_after_a_timed_out_try_counts_as_reached(self) -> None:
        reach = scan.Reach()
        answered = subprocess.CompletedProcess([], 1, '{"data": {"repository": null}}',
                                               "GraphQL: Could not resolve to a Repository with the name 'acme/gone'.")
        with patch.object(scan.subprocess, "run", side_effect=[subprocess.TimeoutExpired("gh", 30), answered]), \
                patch.object(scan.time, "sleep"):
            scan.graphql_repository("acme/gone", "query", reach)
        self.assertEqual(reach.down(), [])

    def test_a_scan_whose_every_jev_call_failed_to_connect_stops_without_a_review_queue(self) -> None:
        key = self.dir / "api_key"
        key.write_text("key-1\n")
        write_jsonl(scan.claude_project_dir(self.dir / "claude", "/w/idle") / "s.jsonl",
                    [user("go"), assistant(text("PR を出しました。"))])

        def offline(request: urllib.request.Request, timeout: float) -> io.BytesIO:
            raise urllib.error.URLError(OSError(8, "nodename nor servname provided, or not known"))

        with patch.object(scan.urllib.request, "urlopen", offline):
            code, out, queue = self.scan_offline([worktree("/w/idle", "done")], "--jev-key-file", str(key))
        self.assertNotEqual(code, 0)
        self.assertTrue(out.startswith("ネットワーク断"), out)
        self.assertFalse(queue.exists())

        def refused(request: urllib.request.Request, timeout: float) -> io.BytesIO:
            raise urllib.error.HTTPError("u", 401, "Unauthorized", Message(), None)

        with patch.object(scan.urllib.request, "urlopen", refused):
            code, _, queue = self.scan_offline([worktree("/w/idle", "done")], "--jev-key-file", str(key))
        self.assertEqual(code, 0)
        self.assertTrue(queue.exists())

    def test_only_scans_one_worktree_and_queues_it_even_when_working(self) -> None:
        selected, other = "/w/selected", "/w/other"
        ps, terms = self.dir / "ps.json", self.dir / "terms.json"
        ps.write_text(json.dumps({"result": {"worktrees": [worktree(selected, "working"),
                                                           worktree(other, "done")]}}))
        terms.write_text(json.dumps({"result": {"terminals": [terminal(selected, "claude", "selected_tab"),
                                                               terminal(other, "claude", "other_tab")]}}))
        projects = self.dir / "claude"
        write_jsonl(scan.claude_project_dir(projects, selected) / "s.jsonl",
                    [user("go"), {**assistant(text("working")), "timestamp": "2026-10-27T01:00:00Z"}])
        queue = self.dir / "triage" / "review-queue.json"
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(scan.main(["--ps-json", str(ps), "--terminals-json", str(terms),
                                        "--claude-projects", str(projects), "--codex-sessions", str(self.dir / "none"),
                                        "--only", "app/selected", "--review-out", str(queue)]), 0)

        rows = json.loads(out.getvalue())
        self.assertEqual([(row["path"], row["class"]) for row in rows], [(selected, "working")])
        self.assertEqual([tab["handle"] for tab in rows[0]["terminals"]], ["selected_tab"])
        self.assertEqual([(row["path"], row["class"]) for row in json.loads(queue.read_text())["rows"]],
                         [(selected, "working")])

    def test_summary_lists_review_rows_and_finished_worktree_tabs(self) -> None:
        ps = self.dir / "ps.json"
        ps.write_text(json.dumps({"result": {"worktrees": [
            worktree("/w/idle", "done"), worktree("/w/finished", "done", status="completed")]}}))
        terms = self.dir / "terms.json"
        terms.write_text(json.dumps({"result": {"terminals": [
            terminal("/w/idle", "claude", "t1"), terminal("/w/finished", "shell", "t2"),
            terminal("/w/finished", "shell", "t3")]}}))
        claude = self.dir / "claude"
        write_jsonl(scan.claude_project_dir(claude, "/w/idle") / "s.jsonl",
                    [user("go"), assistant(text("PR を出しました。"))])
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = scan.main(["--ps-json", str(ps), "--terminals-json", str(terms),
                              "--claude-projects", str(claude), "--codex-sessions", str(self.dir / "none"),
                              "--jev-key-file", str(self.dir / "no-key"), "--summary"])
        self.assertEqual(code, 0)
        self.assertIn("review queue: 1\n  app/idle [unsure]", out.getvalue())
        self.assertIn("finished: 1 worktrees, 2 tabs\n  app/finished (2 tabs)", out.getvalue())

    def test_reads_the_orchestration_mailbox_from_json_files(self) -> None:
        ps = self.dir / "ps.json"
        ps.write_text(json.dumps({"result": {"worktrees": [worktree("/w/lead", "done")]}}))
        terms = self.dir / "terms.json"
        terms.write_text(json.dumps({"result": {"terminals": [terminal("/w/lead", "claude", "term_lead")]}}))
        inbox = self.dir / "inbox.json"
        inbox.write_text(json.dumps({"result": {"messages": [
            {"run_id": "run_a", "to_handle": "run:run_a", "type": "worker_done", "read": 0,
             "created_at": "2026-10-06T07:04:31Z", "subject": "#280 GREEN"}], "count": 1}}))
        runs = self.dir / "runs.json"
        runs.write_text(json.dumps({"result": {"runs": [{"id": "run_a", "coordinator_handle": "term_lead"}]}}))
        claude = self.dir / "claude"
        write_jsonl(scan.claude_project_dir(claude, "/w/lead") / "s.jsonl",
                    [{**user("go"), "timestamp": "2026-10-06T06:56:00Z"}, assistant(text("レビューに回しました。"))])

        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            scan.main(["--ps-json", str(ps), "--terminals-json", str(terms), "--claude-projects", str(claude),
                       "--codex-sessions", str(self.dir / "none"),
                       "--inbox-json", str(inbox), "--runs-json", str(runs)])

        row = json.loads(out.getvalue())[0]
        self.assertEqual((row["class"], row["reasons"]), ("stalled", [
            "1 orchestration message unread since its last turn began (oldest 2026-10-06T07:04:31Z)"]))

    def test_follows_every_page_of_runs_and_survives_an_orca_without_a_mailbox(self) -> None:
        inbox = self.dir / "inbox.json"
        inbox.write_text(json.dumps({"result": {"messages": [
            {"run_id": run, "to_handle": "run:" + run, "type": "question", "read": 0,
             "created_at": "2026-10-07T03:09:00Z", "subject": "Question"} for run in ("run_1", "run_2")]}}))
        pages: dict[tuple[str, ...], dict] = {(): {"runs": [{"id": "run_1", "coordinator_handle": "term_1"}], "nextCursor": "c2"},
                 ("--cursor", "c2"): {"runs": [{"id": "run_2", "coordinator_handle": "term_2"}], "nextCursor": None}}

        def run_list(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            cursor = tuple(argv[argv.index("100") + 1:-1])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": pages[cursor]}), "")

        args = ["--ps-json", "ps.json", "--inbox-json", str(inbox)]
        with patch.object(scan.subprocess, "run", side_effect=run_list):
            mail = scan.read_mail(scan.parse_args(args))
        self.assertEqual(sorted(mail), ["term_1", "term_2"])

        err = io.StringIO()
        with patch.object(scan.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "orca")), \
                redirect_stderr(err):
            self.assertEqual(scan.read_mail(scan.parse_args(args)), {})
        self.assertIn("unread worker mail is not checked", err.getvalue())

    def test_shell_idle_minutes_sets_when_a_quiet_shell_becomes_closable(self) -> None:
        ps = self.dir / "ps.json"
        ps.write_text(json.dumps({"result": {"worktrees": [worktree("/w/s", None)]}}))
        shell = {**terminal("/w/s", None, "term_shell", preview=PROMPT),
                 "lastOutputAt": (time.time() - 10 * 60) * 1000}
        terms = self.dir / "terms.json"
        terms.write_text(json.dumps({"result": {"terminals": [shell]}}))

        def closable(*extra: str) -> list[str]:
            out = io.StringIO()
            with redirect_stdout(out):
                scan.main(["--ps-json", str(ps), "--terminals-json", str(terms),
                           "--claude-projects", str(self.dir / "claude"),
                           "--codex-sessions", str(self.dir / "none"), *extra])
            return json.loads(out.getvalue())[0]["closable"]

        self.assertEqual(closable(), [])
        self.assertEqual(closable("--shell-idle-minutes", "5"), ["term_shell"])

    def scan_merged_worktree(self, *extra: str) -> tuple[tuple[str, list[str], dict], str]:
        ps = self.dir / "ps.json"
        ps.write_text(json.dumps({"result": {"worktrees": [
            worktree("/w/m", "done", pr={"number": 1, "state": "merged"})]}}))
        terms = self.dir / "terms.json"
        terms.write_text(json.dumps({"result": {"terminals": [terminal("/w/m", "claude")]}}))
        claude = self.dir / "claude"
        write_jsonl(scan.claude_project_dir(claude, "/w/m") / "s.jsonl",
                    [user("go"), assistant(text("Merged. Shall I delete the branch?"))])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            scan.main(["--ps-json", str(ps), "--terminals-json", str(terms), "--claude-projects", str(claude),
                       "--codex-sessions", str(self.dir / "none"), *extra])
        row = json.loads(out.getvalue())[0]
        return (row["class"], row["reasons"], row["jev"]), err.getvalue()

    def test_without_a_jev_key_idle_final_messages_come_back_unjudged(self) -> None:
        result, err = self.scan_merged_worktree()

        self.assertEqual(result, ("unsure", ["Jev could not judge: read the final message"],
                                  {"source": "claude", "asks": None, "status": None, "confidence": None}))
        self.assertIn("TYPESAFE_API_KEY", err)

    def test_judges_with_the_key_file(self) -> None:
        key = self.dir / "api_key"
        key.write_text("key-2\n")
        sent = []

        def urlopen(request: urllib.request.Request, timeout: float) -> io.BytesIO:
            sent.append(request.get_header("Authorization"))
            return jev_answer(0.7)

        with patch.object(scan.urllib.request, "urlopen", urlopen):
            result, _ = self.scan_merged_worktree("--jev-key-file", str(key))

        self.assertEqual(result, ("waiting", ["final message asks (Jev 0.70)"],
                                  {"source": "claude", **verdict(0.7)}))
        self.assertEqual(sent, ["Bearer key-2"])

    def test_the_environment_key_wins_over_the_key_file(self) -> None:
        key = self.dir / "api_key"
        key.write_text("from-file\n")
        garbled = self.dir / "garbled"
        garbled.write_bytes(b"\xff\xfe")

        self.assertEqual(scan.jev_key({"TYPESAFE_API_KEY": "from-env"}, key), "from-env")
        self.assertEqual(scan.jev_key({}, key), "from-file")
        self.assertIsNone(scan.jev_key({}, self.dir / "missing"))
        self.assertIsNone(scan.jev_key({}, garbled))


class CloseTabsRowCommandTests(unittest.TestCase):
    def test_rows_combine_all_handles_and_keep_a_row_that_moved_after_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = root / "scan.json"
            saved.write_text(json.dumps([
                {"path": "/w/idle", "repo": "app", "scanned_at": "2026-10-27T01:00:00Z",
                 "terminals": [], "closable": ["shell", "card"], "meaningful_tabs": ["agent", "(no handle) sleeping"],
                 "done_cards": [{"handle": "card"}, {"handle": "card_only"}, {"handle": None}]},
                {"path": "/w/moved", "repo": "app", "scanned_at": "2026-10-27T01:00:00Z",
                 "terminals": [{"handle": "keep"}], "closable": ["keep"]},
                {"path": "/remote/idle", "repo": "app", "environment": "mbp16",
                 "scanned_at": "2026-10-27T01:00:00Z", "terminals": [], "meaningful_tabs": ["shell"]},
            ]))
            closed: list[tuple[str | None, str]] = []

            def orca(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                environment = argv[argv.index("--environment") + 1] if "--environment" in argv else None
                if argv[1:3] == ["worktree", "ps"]:
                    stamp = scan.parse_time("2026-10-27T01:01:00Z").timestamp() * 1000
                    result = {"worktrees": [{"path": "/w/moved", "agents": [{"stateStartedAt": stamp}]}]}
                else:
                    self.assertEqual(argv[1:3], ["terminal", "close"])
                    closed.append((environment, argv[argv.index("--terminal") + 1]))
                    result = {}
                return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result}), "")

            with patch.object(close_tabs.subprocess, "run", side_effect=orca), redirect_stdout(io.StringIO()) as out:
                code = close_tabs.main(["--from", str(saved), "--claude-projects", str(root / "projects"),
                                        "--state-file", str(root / "no-state.json"),
                                        "--inbox", str(root / "inbox"), "--row", "/w/idle",
                                        "--row", "mbp16::idle", "--row", "app/moved"])

            self.assertEqual(code, 1)
            self.assertEqual(closed, [(None, "shell"), (None, "card"), (None, "agent"), (None, "card_only"),
                                      ("mbp16", "shell")])
            self.assertIn("keep: KEPT", out.getvalue())

            with patch.object(close_tabs.subprocess, "run", side_effect=AssertionError("must not close")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(close_tabs.main(["--from", str(saved), "--row", "/w/idle", "--row", "idle"]), 1)
            self.assertIn("mbp16::idle", err.getvalue())


def git(cwd: str | Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class WorktreeNotesTests(unittest.TestCase):
    def test_orca_returns_the_comment_or_none_for_an_unknown_worktree(self) -> None:
        from worktree_notes import OrcaNotes

        comment = "parallel-tests-b6: PR #3568 のレビュー対応中（消さないでください）"
        replies = ((0, {"ok": True, "result": {"worktree": {"comment": comment}}}, comment),
                   (1, {"ok": False, "error": {"code": "selector_not_found"}}, None))
        for code, payload, expected in replies:
            with self.subTest(payload=payload), patch("worktree_notes.subprocess.run", return_value=
                    subprocess.CompletedProcess([], code, stdout=json.dumps(payload), stderr="")):
                self.assertEqual(OrcaNotes().comment(Path("/w/keep")), expected)


class FakeWorktreeNotes:
    def __init__(self, note: str | None = None) -> None:
        self.note = note

    def comment(self, worktree: Path) -> str | None:
        return self.note


class RmCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        binary = Path(self.tmp.name) / "orca-bin/orca"
        binary.parent.mkdir()
        binary.write_text('#!/bin/sh\nif [ "$1 $2" = "automations list" ]; then\n'
                          '  echo \'{"ok":true,"result":{"automations":[]}}\'\nelse\n'
                          '  echo \'{"ok":false,"error":{"code":"selector_not_found"}}\'\n  exit 1\nfi\n')
        binary.chmod(0o755)
        cli = patch.dict(os.environ, {"PATH": f"{binary.parent}:{os.environ.get('PATH', '')}"})
        cli.start()
        self.addCleanup(cli.stop)
        self.main = Path(self.tmp.name) / "main"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        git(self.main, "config", "user.email", "t@example.com")
        git(self.main, "config", "user.name", "t")
        (self.main / ".gitignore").write_text(".env\nnode_modules/\nuploads\n")
        (self.main / "app.txt").write_text("v1\n")
        git(self.main, "add", ".gitignore", "app.txt")
        git(self.main, "commit", "-q", "-m", "init")
        self.wt = Path(self.tmp.name) / "wt"
        git(self.main, "worktree", "add", "-q", "-b", "feature", str(self.wt))
        recheck = patch.object(rmcheck, "PROCESS_RECHECK_SECONDS", 0)
        recheck.start()
        self.addCleanup(recheck.stop)
        automations = patch.object(hourly_runtime, "orca_result", return_value={"automations": []})
        automations.start()
        self.addCleanup(automations.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def check(self, **kwargs: object) -> dict:
        return rmcheck.check(self.wt, self.main, base="main", compose_projects=[], **kwargs)

    def test_an_automation_workspace_blocks_archival_and_removal(self) -> None:
        (self.main / ".git/info/exclude").write_text(".tmp/\n")
        source = self.wt / ".tmp/notes.md"
        source.parent.mkdir()
        source.write_text("keep for the automation\n")
        payload = {"automations": [{"name": "Daily review", "workspaceMode": "existing", "enabled": False,
                                    "workspaceId": "repo-id::" + str(self.wt.resolve())}]}
        with patch.object(hourly_runtime, "orca_result", return_value=payload):
            code, result = self.run_main("--archive-path", ".tmp", "--archive-dir", str(self.main / "archive"))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("自動実行『Daily review』の作業場", result["reasons"])
        self.assertEqual(source.read_text(), "keep for the automation\n")
        self.assertEqual(result["archived"], [])

    def test_clean_worktree_contained_in_base_is_ok(self) -> None:
        result = self.check()

        self.assertTrue(result["ok"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["processes"], [])

    def test_keep_note_blocks_until_the_lead_answers_remove(self) -> None:
        import lead_decisions

        comment = "parallel-tests-b6: PR #3568 のレビュー対応中（消さないでください）"
        notes = FakeWorktreeNotes(comment)
        log = Path(self.tmp.name) / "decisions.jsonl"
        key = "remove:" + str(self.wt.resolve())

        code, result = self.run_main("--decisions-file", str(log), notes=notes)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn(comment, " ".join(result["blockers"]))
        self.assertIn(key, " ".join(result["blockers"]))

        lead_decisions.append(log, lead_decisions.Decision("allow", key, "消してよい", "2026-10-28T00:00:00Z"))
        code, result = self.run_main("--decisions-file", str(log), notes=notes)
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertIn("消してよい", " ".join(result["notes"]))

    def test_keep_note_prevents_nested_detach(self) -> None:
        copy = self.nested_catalog()
        comment = "parallel-devenv: ベンチマークの比較に使うので残す"

        code, result = self.run_main("--detach-nested", "--decisions-file",
                                     str(Path(self.tmp.name) / "decisions.jsonl"), notes=FakeWorktreeNotes(comment))
        self.assertEqual((code, result["detached"]), (1, []))
        self.assertIn(comment, " ".join(result["blockers"]))
        self.assertTrue(copy.exists())

    def test_running_process_using_the_worktree_blocks_removal(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "print('ready', flush=True); import time; time.sleep(30)",
             str(self.wt.resolve())],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            code, result = self.run_main()

            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual([item["pid"] for item in result["processes"]], [process.pid])
            self.assertIn("python3", result["processes"][0]["command"])
            self.assertIn("PID", " ".join(result["blockers"]))
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

        code, result = self.run_main()
        self.assertEqual((code, result["processes"]), (0, []))

    def test_process_running_from_worktree_blocks_without_path_in_command(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "print('ready', flush=True); import time; time.sleep(30)"],
            cwd=self.wt, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            code, result = self.run_main()

            self.assertEqual(code, 1)
            self.assertEqual([item["pid"] for item in result["processes"]], [process.pid])
            self.assertEqual(result["processes"][0]["cwd"], str(self.wt.resolve()))
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_the_reader_of_its_output_does_not_block(self) -> None:
        # rm_check.py | <reader in the worktree>: the reader shares the check's process group.
        reader = f"{sys.executable} -c 'import sys; sys.stdout.write(sys.stdin.read())'"
        command = (f"{sys.executable} {rmcheck.__file__} {self.wt} - --base main --no-docker "
                   f"| (cd {self.wt} && {reader})")
        completed = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)

        result = json.loads(completed.stdout)
        self.assertEqual((result["ok"], result["processes"]), (True, []), completed.stderr)

    def test_the_reader_of_a_batch_wrappers_output_does_not_block(self) -> None:
        wrapper = Path(self.tmp.name) / "batch.py"
        wrapper.write_text("import subprocess, sys\n"
                           "result = subprocess.run(sys.argv[1:], capture_output=True, text=True)\n"
                           "print(result.stdout)\n")
        args = [sys.executable, str(wrapper), sys.executable, str(rmcheck.__file__), str(self.wt), "-",
                "--base", "main", "--no-docker"]
        reader = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
        completed = subprocess.run(["/bin/sh", "-c", shlex.join(args) + " | " + shlex.join(reader)],
                                   cwd=self.wt, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        self.assertEqual((result["ok"], result["processes"]), (True, []), result)

    def test_a_worker_beside_the_reader_of_its_output_still_blocks(self) -> None:
        # The reader writes to /dev/null, which a worker in the same process group reads.
        out = Path(self.tmp.name) / "out.json"
        command = (f"(cd {self.wt} && exec sleep 30 < /dev/null) & sleep 1; "
                   f"{sys.executable} {rmcheck.__file__} {self.wt} - --base main --no-docker "
                   f"| tee {out} > /dev/null; kill $!")
        completed = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)

        result = json.loads(out.read_text())
        self.assertFalse(result["ok"], completed.stderr)
        self.assertEqual(len(result["processes"]), 1)

    def test_check_does_not_count_itself_or_its_shell(self) -> None:
        command = (f'{sys.executable} {rmcheck.__file__} --worktree {self.wt} '
                   '--base main --no-docker; :')
        completed = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["processes"], [])

    def test_process_in_a_sibling_with_the_same_name_prefix_does_not_block(self) -> None:
        sibling = self.wt.with_name(self.wt.name + "-other")
        sibling.mkdir()
        process = subprocess.Popen(
            [sys.executable, "-c", "print('ready', flush=True); import time; time.sleep(30)",
             str(sibling.resolve() / "server.js")],
            cwd=sibling, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert process.stdout is not None
            self.assertEqual(process.stdout.readline().strip(), "ready")
            code, result = self.run_main()

            self.assertEqual((code, result["processes"]), (0, []))
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_the_main_checkout_is_never_removable(self) -> None:
        result = rmcheck.check(self.main, self.main, base="main", compose_projects=[])

        self.assertEqual((result["ok"], result["blockers"]),
                         (False, ["this is the repository's main checkout: close its tabs and keep it"]))

    def test_uncommitted_changes_block_removal(self) -> None:
        (self.wt / "app.txt").write_text("v2\n")
        (self.wt / "new.txt").write_text("draft\n")

        result = self.check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["blockers"], ["2 uncommitted changes"])

    def test_reasons_name_every_path_that_stops_the_removal_even_without_blockers(self) -> None:
        (self.wt / "uploads").mkdir()
        (self.wt / "uploads" / "export.csv").write_text("hand-made\n")
        code, review_only = self.run_main()
        (self.wt / "app.txt").write_text("v2\n")
        _, both = self.run_main()

        self.assertEqual((code, review_only["blockers"], len(review_only["reasons"])), (1, [], 1))
        self.assertIn("uploads/", review_only["reasons"][0])
        self.assertEqual(both["reasons"], ["1 uncommitted changes: app.txt", review_only["reasons"][0]])

    def test_a_process_outside_tabs_that_ends_during_the_wait_does_not_block(self) -> None:
        brief = subprocess.Popen(["sleep", "30"], cwd=self.wt)
        stays = subprocess.Popen(["sleep", "30"], cwd=self.wt)

        def brief_ends(_seconds: float) -> None:
            brief.kill()
            brief.wait()

        try:
            with patch.object(rmcheck.time, "sleep", side_effect=brief_ends):
                result = self.check()
        finally:
            for process in (brief, stays):
                process.kill()
                process.wait()

        self.assertEqual([item["pid"] for item in result["processes"]], [stays.pid])
        [blocker] = result["blockers"]
        self.assertIn("PID %d（sleep 30、cwd %s）" % (stays.pid, self.wt.resolve()), blocker)
        self.assertNotIn(str(brief.pid), blocker)

    def test_a_scheduled_tasks_lock_blocks_only_while_its_holder_runs(self) -> None:
        (self.main / ".git/info/exclude").write_text("**/.claude/scheduled_tasks.lock\n")
        lock = self.wt / ".claude/scheduled_tasks.lock"
        lock.parent.mkdir()
        ended = subprocess.Popen(["true"])
        ended.wait()
        lock.write_text(json.dumps({"sessionId": "s", "pid": ended.pid, "procStart": "Thu Nov  5 01:02:28 2026"}))
        stale = self.check()
        holder = subprocess.Popen(["sleep", "30"])
        try:
            lock.write_text(json.dumps({"sessionId": "s", "pid": holder.pid, "procStart": self.started_at(holder.pid)}))
            held = self.check()
        finally:
            holder.kill()
            holder.wait()

        self.assertEqual((stale["ok"], stale["review"], stale["regenerable"]),
                         (True, [], [".claude/scheduled_tasks.lock"]))
        self.assertEqual((held["ok"], held["review"]), (False, []))
        self.assertEqual([(item["pid"], item["lock"]) for item in held["processes"]],
                         [(holder.pid, ".claude/scheduled_tasks.lock")])

    def test_a_commit_whose_patch_reached_the_base_does_not_block(self) -> None:
        # A PR rebased before its merge leaves the old commit here; its patch is in the base.
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        (self.main / "base.txt").write_text("moved on\n")
        git(self.main, "add", "base.txt")
        git(self.main, "commit", "-q", "-m", "base moves on")
        git(self.main, "cherry-pick", git(self.wt, "rev-parse", "HEAD"))
        rebased = self.check()
        self.assertEqual(rebased["blockers"], [])
        self.assertEqual(rebased["merged_by_patch"], [git(self.wt, "rev-parse", "HEAD")])

        (self.wt / "other.txt").write_text("only here\n")
        git(self.wt, "add", "other.txt")
        git(self.wt, "commit", "-q", "-m", "unmerged")
        self.assertEqual(len(self.check()["blockers"]), 1)

    def test_a_merge_commit_s_own_changes_still_block(self) -> None:
        # git cherry skips merge commits, so an edit made while merging the base must not pass.
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        git(self.main, "cherry-pick", git(self.wt, "rev-parse", "HEAD"))
        (self.main / "base.txt").write_text("moved on\n")
        git(self.main, "add", "base.txt")
        git(self.main, "commit", "-q", "-m", "base moves on")
        git(self.wt, "merge", "-q", "--no-ff", "--no-edit", "main")
        (self.wt / "base.txt").write_text("moved on\nhand edit\n")
        git(self.wt, "commit", "-q", "--amend", "-a", "--no-edit")

        self.assertEqual(len(self.check()["blockers"]), 1)

    def test_only_regenerable_output_left_is_ok(self) -> None:
        (self.main / ".gitignore").write_text(".env\nnode_modules/\nuploads\ne2e/screenshots/\n")
        git(self.main, "commit", "-q", "-am", "ignore screenshots")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.wt / "e2e" / "screenshots").mkdir(parents=True)
        (self.wt / "e2e" / "screenshots" / "受付 画面.png").write_text("x")

        result = self.check()

        self.assertTrue(result["ok"])
        self.assertEqual((result["review"], result["regenerable"]), ([], ["e2e/screenshots/"]))

    def test_generated_sources_and_test_output_do_not_hold_up_removal(self) -> None:
        (self.main / ".gitignore").write_text(".env\nnode_modules/\nuploads\n__generated__/\nsrc/paraglide/\n"
                                               "e2e/screenshots/\ne2e/.state/\ne2e/.auth/\ndocs/screenshots/\n")
        git(self.main, "commit", "-q", "-am", "ignore generated")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        for rel in ("apps/web/src/api/__generated__/graphql.ts", "src/paraglide/messages.js",
                    "e2e/screenshots/a.png", "e2e/.state/default.sqlite3", "e2e/.auth/user.json",
                    "docs/screenshots/handmade.png"):
            (self.wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.wt / rel).write_text("x")

        result = self.check()

        self.assertEqual(result["review"], ["docs/screenshots/ (not in main checkout)"])
        self.assertEqual(sorted(result["regenerable"]), ["apps/web/src/api/__generated__/", "e2e/.auth/",
                                                        "e2e/.state/", "e2e/screenshots/", "src/paraglide/"])

    def test_untracked_python_bytecode_is_regenerable_not_uncommitted(self) -> None:
        (self.wt / "tool").mkdir()
        (self.wt / "tool" / "run.py").write_text("print(1)\n")
        git(self.wt, "add", "tool/run.py")
        git(self.wt, "commit", "-q", "-m", "tool")
        git(self.main, "merge", "-q", "--ff-only", "feature")
        (self.wt / "tool" / "__pycache__").mkdir()
        (self.wt / "tool" / "__pycache__" / "run.cpython-314.pyc").write_bytes(b"\x00")
        (self.wt / "stale.pyc").write_bytes(b"\x00")
        (self.wt / "notes" / "__pycache__").mkdir(parents=True)
        (self.wt / "notes" / "__pycache__" / "keep.txt").write_text("hand-written\n")

        result = self.check()

        self.assertEqual(result["blockers"], ["1 uncommitted changes"])
        self.assertEqual(result["uncommitted"], ["notes/"])
        self.assertEqual(sorted(result["regenerable"]), ["stale.pyc", "tool/__pycache__/"])

    def test_tool_output_the_repository_ignores_does_not_hold_up_removal(self) -> None:
        # Seen in Orca worktrees: hook managers' install output, test reports and coverage, codegen, tool caches.
        (self.main / ".gitignore").write_text(".env\nnode_modules/\nuploads\n.vite-hooks/_/\nplaywright-report-real/\n"
                                               ".coverage\nsrc/graphql/generated/\n.cache/\n")
        git(self.main, "commit", "-q", "-am", "ignore tool output")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.wt / ".husky" / "_").mkdir(parents=True)
        (self.wt / ".husky" / "_" / ".gitignore").write_text("*")
        (self.wt / ".husky" / "_" / "h").write_text("#!/usr/bin/env sh\n")
        (self.main / ".gitignore").write_text((self.main / ".gitignore").read_text() + "reports/generated/\n")
        git(self.main, "commit", "-q", "-am", "ignore generated reports")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        for rel in (".vite-hooks/_/h", "playwright-report-real/index.html", "apps/api/.coverage",
                    "src/graphql/generated/types.ts", ".cache/typecheck/state.json",
                    "reports/generated/summary.html"):
            (self.wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.wt / rel).write_text("x")

        result = self.check()

        self.assertEqual(result["review"], ["reports/generated/ (not in main checkout)"])
        self.assertEqual(sorted(result["regenerable"]), [".husky/_/.gitignore", ".husky/_/h", ".vite-hooks/_/",
                                                        "src/graphql/generated/"])

    def test_the_apm_installation_identity_does_not_hold_up_removal(self) -> None:
        # APM 0.35 writes these into each checkout it installs to, ignoring them with their own .gitignore.
        (self.wt / ".apm-local").mkdir()
        (self.wt / ".apm-local" / ".gitignore").write_text("*\n")
        (self.wt / ".apm-local" / "installation").write_text("0123456789abcdef0123456789abcdef\n")

        result = self.check()

        self.assertTrue(result["ok"])
        self.assertEqual((result["review"], sorted(result["regenerable"])),
                         ([], [".apm-local/.gitignore", ".apm-local/installation"]))

    def test_unmerged_commit_blocks_unless_it_is_the_pr_head(self) -> None:
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        head = git(self.wt, "rev-parse", "HEAD")

        blocked = self.check()
        squashed = self.check(pr={"number": 7, "state": "MERGED", "headRefOid": head})
        reopened = self.check(pr={"number": 7, "state": "OPEN", "headRefOid": head})

        self.assertEqual(blocked["blockers"], ["HEAD %s is not in main" % head[:8]])
        self.assertTrue(squashed["ok"])
        self.assertTrue(reopened["ok"])

    def test_a_merged_pr_of_the_branch_at_head_counts_without_pr(self) -> None:
        git(self.main, "remote", "add", "origin", "git@github.com:acme/app.git")
        (self.wt / "app.txt").write_text("squashed\n")
        git(self.wt, "commit", "-q", "-am", "change")
        head = git(self.wt, "rev-parse", "HEAD")
        real_run = subprocess.run
        lookups = []

        def gh_lists(prs: list[dict]) -> Callable[..., subprocess.CompletedProcess[str]]:
            def fake_run(cmd: list[str], **kwargs: Unpack[TextRunOptions]) -> subprocess.CompletedProcess[str]:
                if cmd[0] == "gh":
                    lookups.append(cmd)
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(prs), stderr="")
                return real_run(cmd, **kwargs)
            return fake_run

        merged = {"number": 7, "state": "MERGED", "headRefOid": head, "baseRefName": "main"}
        with patch.object(rmcheck.subprocess, "run", side_effect=gh_lists([merged])):
            code, result = self.run_main()
        with patch.object(rmcheck.subprocess, "run", side_effect=gh_lists([{**merged, "headRefOid": "c" * 40}])):
            other_code, other = self.run_main()

        self.assertEqual((code, result["blockers"]), (0, []))
        self.assertIn("PR #7", " ".join(result["notes"]))
        self.assertEqual(lookups[0][:5], ["gh", "pr", "list", "--head", "feature"])
        self.assertEqual((other_code, other["blockers"]), (1, ["HEAD %s is not in main" % head[:8]]))

    def test_archive_scratch_moves_only_untracked_exec_plan_markdown_absent_from_a_merged_pr_base(self) -> None:
        plan = self.wt / "docs/exec-plans/active/remaining.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("Remaining work\n")
        archive = Path(self.tmp.name) / "archive"
        args = ("--pr", "7", "--base", "main", "--archive-scratch", "--archive-dir", str(archive))
        pr = {"number": 7, "state": "MERGED", "headRefOid": git(self.wt, "rev-parse", "HEAD"), "baseRefName": "main"}

        for state in (None, {**pr, "state": "OPEN"}):
            code, result = self.run_main_without_base(*args, pr=state)
            self.assertEqual((code, result["archived"]), (1, []))
            self.assertTrue(plan.exists())
        for other in (self.wt / "draft.txt", plan.with_suffix(".txt"), self.wt / "app.txt"):
            with self.subTest(other=other):
                other.write_text("keep this\n")
                code, result = self.run_main_without_base(*args, pr=pr)
                self.assertEqual((code, result["archived"]), (1, []))
                self.assertTrue(plan.exists())
                if other.name == "app.txt":
                    other.write_text("v1\n")
                else:
                    other.unlink()

        code, result = self.run_main_without_base(*args, pr=pr)
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertFalse(plan.exists())
        moved = result["archived"]
        self.assertEqual([(item["from"], item["verified"]) for item in moved], [(str(plan.resolve()), True)])
        self.assertEqual(Path(moved[0]["to"]).read_text(), "Remaining work\n")

    def test_archive_scratch_keeps_an_untracked_plan_that_is_still_in_base(self) -> None:
        rel = "docs/exec-plans/plan.md"
        base_plan = self.main / rel
        base_plan.parent.mkdir(parents=True)
        base_plan.write_text("Base plan\n")
        git(self.main, "add", rel)
        git(self.main, "commit", "-q", "-m", "plan")
        git(self.wt, "merge", "-q", "main")
        git(self.wt, "rm", "-q", rel)
        git(self.wt, "commit", "-q", "-m", "finish plan")
        plan = self.wt / rel
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("New local work\n")
        pr = {"number": 7, "state": "MERGED", "headRefOid": git(self.wt, "rev-parse", "HEAD"), "baseRefName": "main"}
        code, result = self.run_main_without_base("--pr", "7", "--base", "main", "--archive-scratch",
                                                 "--archive-dir", str(Path(self.tmp.name) / "archive"), pr=pr)
        self.assertEqual((code, result["archived"]), (1, []))
        self.assertEqual(plan.read_text(), "New local work\n")

    def test_archive_scratch_moves_records_only_when_they_alone_keep_the_worktree(self) -> None:
        (self.wt / ".yomiyasu" / "inputs").mkdir(parents=True)
        (self.wt / ".yomiyasu" / ".gitignore").write_text("*\n")
        (self.wt / ".yomiyasu" / "inputs" / "draft.md").write_text("原文\n")
        (self.wt / ".investigate").mkdir()
        (self.wt / ".investigate" / "notes.md").write_text("調べた記録\n")
        archive = Path(self.tmp.name) / "archive"

        default_code, _ = self.run_main()
        (self.wt / "app.txt").write_text("unsaved work\n")
        dirty_code, dirty = self.run_main("--archive-scratch", "--archive-dir", str(archive))
        (self.wt / "app.txt").write_text("v1\n")
        code, result = self.run_main("--archive-scratch", "--archive-dir", str(archive))

        self.assertEqual((default_code, dirty_code), (1, 1))
        self.assertEqual(dirty["archived"], [])
        self.assertEqual((code, result["ok"], result["review"], result["blockers"]), (0, True, [], []))
        self.assertFalse((self.wt / ".yomiyasu").exists() or (self.wt / ".investigate").exists())
        moved = sorted(path.name for path in archive.iterdir() if path.is_dir())
        self.assertRegex(" ".join(moved), r"^wt-investigate-\d{8} wt-yomiyasu-\d{8}$")
        self.assertEqual((archive / moved[1] / "inputs" / "draft.md").read_text(), "原文\n")
        self.assertEqual(sorted(Path(item["to"]).name for item in result["archived"]), moved)

    def test_an_ignored_output_is_regenerable_only_while_a_tracked_file_of_its_generator_names_it(self) -> None:
        # 11/10: a repository's worktree hook wrote .claude/.venv-missing in 29 worktrees, and each one stopped removal.
        (self.main / ".git/info/exclude").write_text(".claude/.venv-missing\n")
        (self.wt / ".claude").mkdir()
        (self.wt / ".claude/.venv-missing").write_text("uv sync failed at 2026-10-01\n")

        unconfirmed = self.check()
        hook = self.main / ".claude/hooks/worktree_create.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text('VENV_MARKER="$WORKTREE_PATH/.claude/.venv-missing"\n')
        git(self.main, "add", ".claude/hooks/worktree_create.sh")
        git(self.main, "commit", "-q", "-m", "hook")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        confirmed = self.check()

        self.assertEqual(([item["path"] for item in unconfirmed["held"]], unconfirmed["ok"]),
                         ([".claude/.venv-missing"], False))
        self.assertEqual((confirmed["ok"], confirmed["held"], confirmed["regenerable"]),
                         (True, [], [".claude/.venv-missing"]))

    def test_archive_scratch_moves_work_records_aside_and_lists_the_files_it_holds_for_a_decision(self) -> None:
        # 11/10: screenshots and design critiques stopped removals until each was moved by hand with --archive-path.
        (self.main / ".git/info/exclude").write_text("scripts/__screenshots__/\n.impeccable/\nlocal.sqlite3\n")
        (self.main / ".env.example").write_text("API_URL=http://localhost\n")
        git(self.main, "add", ".env.example")
        git(self.main, "commit", "-q", "-m", "example")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.wt / "scripts/__screenshots__").mkdir(parents=True)
        (self.wt / "scripts/__screenshots__/after-mobile.png").write_bytes(b"\x89PNG after")
        (self.wt / ".impeccable/critique").mkdir(parents=True)
        (self.wt / ".impeccable/critique/admin.md").write_text("余白が狭い\n")
        (self.wt / ".env").write_text("API_URL=http://localhost\nOPENAI_API_KEY=sk-only-here\n")
        (self.wt / "local.sqlite3").write_bytes(b"SQLite format 3\x00")
        archive = Path(self.tmp.name) / "archive"

        code, held = self.run_main("--archive-scratch", "--archive-dir", str(archive))

        self.assertEqual((code, held["archived"]), (1, []))
        self.assertEqual(sorted((item["path"], item["kind"]) for item in held["records"]),
                         [(".impeccable/", "reviews"), ("scripts/__screenshots__/", "images")])
        self.assertEqual([(item["path"], item["kind"]) for item in held["held"]],
                         [(".env", "setting"), ("local.sqlite3", "data")])
        self.assertIn("OPENAI_API_KEY", held["held"][0]["reason"])
        self.assertNotIn("sk-only-here", json.dumps(held, ensure_ascii=False))
        self.assertTrue(all(item["decision"] for item in held["held"]))
        self.assertTrue(any(".env" in reason and held["held"][0]["decision"] in reason for reason in held["reasons"]))

        (self.wt / ".env").unlink()
        (self.wt / "local.sqlite3").unlink()
        code, result = self.run_main("--archive-scratch", "--archive-dir", str(archive))

        self.assertEqual((code, result["ok"], result["review"]), (0, True, []))
        self.assertFalse((self.wt / "scripts/__screenshots__").exists() or (self.wt / ".impeccable").exists())
        self.assertTrue(all(item["verified"] for item in result["archived"]))
        manifest = [json.loads(line) for line in (archive / "archived.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(entry["from"] for entry in manifest),
                         sorted(str(self.wt.resolve() / rel) for rel in (".impeccable", "scripts/__screenshots__")))
        moved = next(Path(entry["to"]) for entry in manifest if entry["from"].endswith("__screenshots__"))
        self.assertEqual((moved / "after-mobile.png").read_bytes(), b"\x89PNG after")

    def test_archive_path_moves_a_named_path_only_when_named_paths_alone_keep_the_worktree(self) -> None:
        (self.main / ".git/info/exclude").write_text(".tmp/\n")
        named = self.wt / ".tmp/pr-descriptions"
        named.mkdir(parents=True)
        (named / "917.md").write_text("PR 本文の写し\n")
        (self.wt / ".tmp/scratch.txt").write_text("not named\n")
        archive = Path(self.tmp.name) / "archive"
        args = ("--archive-path", ".tmp/pr-descriptions", "--archive-dir", str(archive))

        kept_code, kept = self.run_main(*args)
        (self.wt / ".tmp/scratch.txt").unlink()
        code, result = self.run_main(*args)

        self.assertEqual((kept_code, kept["archived"]), (1, []))
        self.assertEqual((code, result["ok"], result["review"]), (0, True, []))
        [moved] = result["archived"]
        self.assertRegex(Path(moved["to"]).name, r"^wt-pr-descriptions-\d{8}$")
        self.assertEqual((Path(moved["to"]) / "917.md").read_text(), "PR 本文の写し\n")
        self.assertFalse(named.exists())

    def test_archive_paths_that_overlap_move_once_with_their_parent(self) -> None:
        (self.main / ".git/info/exclude").write_text("notes/child/\n")
        (self.wt / "notes/child").mkdir(parents=True)
        (self.wt / "notes/other.md").write_text("untracked\n")
        (self.wt / "notes/child/draft.md").write_text("ignored\n")

        code, result = self.run_main("--archive-path", "notes", "--archive-path", "notes/child",
                                     "--archive-dir", str(Path(self.tmp.name) / "archive"))

        self.assertEqual((code, [Path(item["from"]).name for item in result["archived"]]), (0, ["notes"]))

    def test_a_named_deletion_moves_nothing_and_keeps_the_worktree(self) -> None:
        (self.wt / "notes").mkdir()
        (self.wt / "notes/draft.md").write_text("untracked\n")
        (self.wt / "app.txt").unlink()

        code, result = self.run_main("--archive-path", "notes", "--archive-path", "app.txt",
                                     "--archive-dir", str(Path(self.tmp.name) / "archive"))

        self.assertEqual((code, result["archived"]), (1, []))
        self.assertTrue((self.wt / "notes/draft.md").exists())

    def test_archive_scratch_copies_records_main_also_ignores_and_lacks(self) -> None:
        self.ignore_review_records()
        (self.wt / ".claude/settings.local.json").write_text('{"hooks": {}}\n')
        (self.main / ".claude/reviews/topic").write_text("a file in main\n")
        (self.wt / ".claude/reviews/topic").mkdir()
        (self.wt / ".claude/reviews/topic/more.md").write_text("needs a directory main has as a file\n")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.main / ".claude/reviews/linked").symlink_to(outside)
        (self.wt / ".claude/reviews/linked").mkdir()
        (self.wt / ".claude/reviews/linked/away.md").write_text("main links this directory elsewhere\n")

        code, result = self.run_main("--archive-scratch")

        self.assertEqual((self.main / ".claude/reviews/new.md").read_text(), "the worktree's review\n")
        self.assertEqual([Path(item["to"]).name for item in result["copied_to_main"]], ["new.md"])
        self.assertEqual(code, 1)
        self.assertFalse((self.main / ".claude/settings.local.json").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_archive_scratch_copies_nothing_while_other_blockers_keep_the_worktree(self) -> None:
        self.ignore_review_records()
        (self.wt / "app.txt").write_text("unsaved work\n")

        _, result = self.run_main("--archive-scratch")

        self.assertEqual(result["copied_to_main"], [])
        self.assertFalse((self.main / ".claude/reviews/new.md").exists())

    def ignore_review_records(self) -> None:
        """Main ignores .claude/ and keeps an older review; the worktree has a new one main lacks."""
        (self.main / ".gitignore").write_text(".env\nnode_modules/\nuploads\n.claude/\n")
        git(self.main, "commit", "-q", "-am", "ignore review records")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.main / ".claude/reviews").mkdir(parents=True)
        (self.main / ".claude/reviews/old.md").write_text("main's record\n")
        (self.wt / ".claude/reviews").mkdir(parents=True)
        (self.wt / ".claude/reviews/new.md").write_text("the worktree's review\n")

    def test_detach_nested_and_archive_scratch_clear_a_worktree_only_they_keep(self) -> None:
        copy = self.nested_catalog()
        (self.wt / ".investigate").mkdir()
        (self.wt / ".investigate" / "notes.md").write_text("調べた記録\n")
        archive = Path(self.tmp.name) / "archive"

        _, alone = self.run_main("--detach-nested")
        code, result = self.run_main("--detach-nested", "--archive-scratch", "--archive-dir", str(archive))

        self.assertEqual(alone["detached"], [])
        self.assertEqual((code, result["ok"], len(result["detached"]), len(result["archived"])), (0, True, 1, 1))
        self.assertFalse(copy.exists() or (self.wt / ".investigate").exists())

    def test_work_after_the_scan_blocks_removal(self) -> None:
        scan_file = Path(self.tmp.name) / "scan.json"
        scanned_at = datetime.now().astimezone().isoformat()
        scan_file.write_text(json.dumps([{"path": str(self.wt), "scanned_at": scanned_at}]))
        (self.wt / "node_modules").mkdir()
        (self.wt / "node_modules" / "pkg.js").write_text("installed\n")

        before_code, before = self.run_main("--from", str(scan_file))
        time.sleep(1.1)  # the reflog dates a move of HEAD to the second
        (self.wt / "draft.md").write_text("new notes\n")
        git(self.wt, "add", "draft.md")
        git(self.wt, "commit", "-q", "-m", "draft")
        git(self.main, "merge", "-q", "feature")
        code, result = self.run_main("--from", str(scan_file))
        later_code, _ = self.run_main("--since", datetime.now().astimezone().isoformat())

        self.assertEqual((before_code, before["blockers"]), (0, []))
        self.assertEqual(code, 1)
        self.assertEqual(len(result["blockers"]), 2)
        self.assertIn("draft.md", result["blockers"][0])
        self.assertIn("HEAD", result["blockers"][1])
        self.assertEqual(later_code, 0)

    def test_an_ignored_directory_of_logs_or_cbm_test_storage_is_regenerable(self) -> None:
        (self.main / ".gitignore").write_text(".env\nnode_modules/\nuploads\nlogs/\napps/api/media/\n")
        git(self.main, "commit", "-q", "-am", "ignore logs")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.wt / "logs").mkdir()
        (self.wt / "logs" / "app.log").write_text("DEBUG ready\n")
        storage = self.wt / "apps/api/media/files/datafile/storage/2026/11"
        storage.mkdir(parents=True)
        (storage / "upload.csv").write_text("a,b\n")

        code, result = self.run_main()

        self.assertEqual((code, result["review"]), (0, []))
        self.assertEqual(sorted(result["regenerable"]), ["apps/api/media/", "logs/"])
        self.assertRegex(" ".join(result["notes"]), r"logs/ holds only logs: 12 bytes, last written \d{4}-")

    def test_ignored_cargo_tauri_and_terraform_outputs_are_regenerable_beside_their_sources(self) -> None:
        # 2026-11-10/11: keito's src-tauri/target/ and gen/schemas/, and taihei-aws's .terraform/ (s3 backend) and
        # module zip/ held removals as unknown until the hourly tab read their .gitignore lines by hand.
        sources = {"src-tauri/.gitignore": "/target/\n/gen/schemas\n", "src-tauri/Cargo.toml": "[package]\n",
                   "src-tauri/tauri.conf.json": "{}\n", "terraform/devel/main.tf": 'terraform { backend "s3" {} }\n',
                   "terraform/modules/dms/monitor.tf": 'data "archive_file" "restart" {\n'
                                                       '  output_path = "${path.module}/zip/restart.zip"\n}\n',
                   "terraform/modules/lambda/main.tf": 'data "archive_file" "fn" {\n'
                                                       '  output_path = "${path.module}/zip/${each.key}.zip"\n}\n',
                   "terraform/modules/backup/main.tf": 'data "archive_file" "build" {\n'
                                                       '  output_path = "${path.module}/zip/build.zip"\n'
                                                       '  # output_path = "${path.module}/zip/unique-backup.zip"\n}\n',
                   "terraform/modules/other/main.tf": 'module "archive_file" {\n  source = "./empty"\n'
                                                       '  output_path = "${path.module}/zip/kept.zip"\n}\n'}
        for name, content in sources.items():
            (self.main / name).parent.mkdir(parents=True, exist_ok=True)
            (self.main / name).write_text(content)
        with (self.main / ".gitignore").open("a") as ignore:
            ignore.write(".terraform\nzip\n")
        git(self.main, "add", ".gitignore", *sources)
        git(self.main, "commit", "-q", "-m", "tauri and terraform")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        outputs = {"src-tauri/target/debug/app": "binary", "src-tauri/gen/schemas/desktop-schema.json": "{}",
                   "terraform/devel/.terraform/terraform.tfstate": json.dumps({"backend": {"type": "s3"}}),
                   "terraform/devel/.terraform/providers/aws": "provider",
                   "terraform/local/.terraform/terraform.tfstate": json.dumps({"backend": {"type": "local"}}),
                   "terraform/modules/dms/zip/restart.zip": "zip", "terraform/modules/lambda/zip/notify.zip": "zip",
                   "terraform/modules/backup/zip/unique-backup.zip": "zip", "terraform/modules/other/zip/kept.zip": "zip"}
        for name, content in outputs.items():
            (self.wt / name).parent.mkdir(parents=True, exist_ok=True)
            (self.wt / name).write_text(content)

        code, result = self.run_main()

        # Being ignored is no sign on its own: a local backend, and zips no tracked archive_file declares, are held.
        self.assertEqual((code, [item["path"] for item in result["held"]]),
                         (1, ["terraform/local/.terraform/", "terraform/modules/backup/zip/",
                              "terraform/modules/other/zip/"]))
        self.assertEqual(sorted(result["regenerable"]), ["src-tauri/gen/schemas/", "src-tauri/target/",
                                                         "terraform/devel/.terraform/", "terraform/modules/dms/zip/",
                                                         "terraform/modules/lambda/zip/"])
        self.assertIn("src-tauri/target/ is Cargo's build output (src-tauri/Cargo.toml), ignored by "
                      "src-tauri/.gitignore:1:/target/", result["notes"])
        self.assertIn("terraform/modules/dms/zip/ is archives Terraform's archive_file writes "
                      "(terraform/modules/dms/monitor.tf), ignored by .gitignore:5:zip", result["notes"])

    def test_open_pr_at_clean_checkout_head_is_removable(self) -> None:
        (self.wt / "app.txt").write_text("pushed change\n")
        git(self.wt, "commit", "-q", "-am", "change")
        head = git(self.wt, "rev-parse", "HEAD")
        pr = {"number": 7, "state": "OPEN", "headRefOid": head}

        code, result = self.run_main_without_base("--pr", "7", pr=pr)
        (self.wt / "app.txt").write_text("unsaved work\n")
        dirty = self.check(pr=pr)

        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(dirty["blockers"], ["1 uncommitted changes"])

    def test_an_ignored_path_that_holds_nothing_needs_no_review(self) -> None:
        (self.main / "uploads").write_text("rows from the main checkout\n")
        (self.wt / "uploads").write_text("")
        (self.wt / "node_modules").mkdir()
        (self.wt / ".env").mkdir()
        (self.wt / ".env" / "empty").write_text("")

        self.assertEqual(self.check()["review"], [])

    def test_ignored_files_need_review_unless_cache_or_same_as_main(self) -> None:
        (self.main / ".env").write_text("A=1\n")
        (self.wt / ".env").write_text("A=1\n")
        (self.wt / "node_modules").mkdir()
        (self.wt / "node_modules" / "x.js").write_text("cache\n")
        same = self.check()
        (self.wt / ".env").write_text("A=2\n")
        (self.wt / ".gitignore").write_text(".env\nnode_modules/\nout/\n")
        git(self.wt, "commit", "-q", "-am", "ignore out")
        git(self.main, "merge", "-q", "feature")
        (self.wt / "out").mkdir()
        (self.wt / "out" / "report.json").write_text("{}\n")

        changed = self.check()

        self.assertTrue(same["ok"])
        self.assertEqual(same["review"], [])
        self.assertFalse(changed["ok"])
        self.assertEqual(changed["review"], [".env (differs from main checkout)",
                                             "out/ (not in main checkout)"])

    def test_ignored_tool_output_is_regenerable_only_while_it_holds_no_other_work(self) -> None:
        (self.main / ".git" / "info" / "exclude").write_text("site/\nweb/var/\nweb/test_*.sqlite3\n")
        (self.wt / "mkdocs.yaml").write_text("site_name: docs\n")
        git(self.wt, "add", "mkdocs.yaml")
        git(self.wt, "commit", "-q", "-m", "docs config")
        git(self.main, "merge", "-q", "feature")
        for rel in ("site/index.html", "web/var/test-storage/upload.png", "web/test_db_masters.sqlite3"):
            path = self.wt / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("generated\n")
        (self.wt / "web/var/empty").write_text("")

        generated = self.check()
        (self.wt / "web/var/notes.txt").write_text("keep these notes\n")
        mixed = self.check()
        git(self.wt, "mv", "mkdocs.yaml", "other.yaml")
        no_config = self.check()

        self.assertEqual(generated["review"], [])
        self.assertEqual(set(generated["regenerable"]), {"site/", "web/var/", "web/test_db_masters.sqlite3"})
        self.assertEqual(mixed["review"], ["web/var/ (not in main checkout)"])
        self.assertIn("site/ (not in main checkout)", no_config["review"])

    def test_ignored_directory_is_copied_when_main_holds_all_its_files_and_more(self) -> None:
        (self.main / ".git" / "info" / "exclude").write_text(".claude/reviews/\n")
        for checkout in (self.main, self.wt):
            (checkout / ".claude/reviews").mkdir(parents=True)
            (checkout / ".claude/reviews/review.md").write_text("saved review\n")
        (self.main / ".claude/reviews/other.md").write_text("another review\n")

        copied = self.check()
        # A main-side link back into the worktree is no copy of what it points at.
        (self.wt / ".claude/reviews/run").mkdir()
        (self.wt / ".claude/reviews/run/notes.md").write_text("only here\n")
        (self.main / ".claude/reviews/run").symlink_to(self.wt / ".claude/reviews/run")
        linked = self.check()
        (self.main / ".claude/reviews/run").unlink()
        (self.wt / ".claude/reviews/run/notes.md").unlink()
        (self.wt / ".claude/reviews/run").rmdir()
        (self.wt / ".claude/reviews/review.md").write_text("new review\n")
        changed = self.check()

        self.assertEqual(copied["review"], [])
        self.assertEqual(linked["review"], [".claude/reviews/ (differs from main checkout)"])
        self.assertEqual(changed["review"], [".claude/reviews/ (differs from main checkout)"])

    def test_an_env_is_regenerable_when_it_moves_only_ports_from_its_example(self) -> None:
        (self.main / ".env.example").write_text("# local\nDB_HOST_PORT=5432\nAPI_KEY=\nDEBUG=1\n")
        git(self.main, "add", ".env.example")
        git(self.main, "commit", "-q", "-m", "env example")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.wt / ".env").write_text("DB_HOST_PORT=15432\nAPI_KEY=\nDEBUG=1\nCOMPOSE_PROJECT_NAME=wt\n")
        ports_only = self.check()
        (self.wt / ".env").write_text("DB_HOST_PORT=15432\nAPI_KEY=from-the-vendor\nDEBUG=1\nEXTRA=on\n")

        with_values = self.check()

        self.assertEqual((ports_only["review"], ports_only["regenerable"]), ([], [".env"]))
        self.assertEqual(with_values["review"],
                         [".env (not in main checkout; values .env.example does not have: API_KEY, EXTRA)"])
        self.assertNotIn("from-the-vendor", json.dumps(with_values))

    def test_an_env_with_a_multiline_value_stays_in_review_without_key_names(self) -> None:
        (self.main / ".env.example").write_text('PORT=8000\nSIGNING_KEY="-----BEGIN KEY-----\nexample\n-----END KEY-----"\n')
        git(self.main, "add", ".env.example")
        git(self.main, "commit", "-q", "-m", "env example")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.wt / ".env").write_text('PORT=8001\nSIGNING_KEY="-----BEGIN KEY-----\nc2VjcmV0PQ==\n-----END KEY-----"\n')

        result = self.check()

        self.assertEqual((result["review"], result["regenerable"]), ([".env (not in main checkout)"], []))
        self.assertNotIn("c2VjcmV0", json.dumps(result))

    def test_reports_compose_projects_defined_in_the_worktree(self) -> None:
        projects = [
            {"Name": "app-wt", "Status": "running(2)", "ConfigFiles": str(self.wt / "compose.yaml")},
            {"Name": "app-main", "Status": "running(1)", "ConfigFiles": str(self.main / "compose.yaml")},
        ]

        result = rmcheck.check(self.wt, self.main, base="main", compose_projects=projects)

        self.assertTrue(result["ok"])
        self.assertEqual(result["containers"], [{"project": "app-wt", "status": "running(2)"}])

    def test_docker_timeout_blocks_only_a_worktree_with_a_compose_definition(self) -> None:
        real_run = subprocess.run

        def fake_run(cmd: list[str], **kwargs: Unpack[TextRunOptions]) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "docker":
                raise subprocess.TimeoutExpired(cmd, rmcheck.DOCKER_TIMEOUT)
            return real_run(cmd, **kwargs)

        (self.wt / "node_modules/package").mkdir(parents=True)
        (self.wt / "node_modules/package/compose.yaml").write_text("services: {}\n")
        with patch.object(rmcheck.subprocess, "run", side_effect=fake_run):
            without = self.run_main(docker=True)
            (self.wt / "ops").mkdir()
            (self.wt / "ops/compose.dev.yml").write_text("services: {}\n")
            git(self.wt, "add", "ops")
            git(self.wt, "commit", "-q", "-m", "compose")
            git(self.main, "merge", "-q", "feature")
            with_compose = self.run_main(docker=True)

        self.assertEqual((without[0], without[1]["blockers"]), (0, []))
        self.assertEqual(with_compose[0], 1)
        self.assertIn("docker が 20 秒応答しません", " ".join(with_compose[1]["blockers"]))

    def test_a_dangling_symlink_is_reported_instead_of_crashing(self) -> None:
        (self.main / "uploads").mkdir()
        (self.main / "uploads" / "a.png").write_text("png")
        (self.wt / "uploads").symlink_to(Path(self.tmp.name) / "gone")

        result = self.check()

        self.assertEqual(result["review"], ["uploads (differs from main checkout)"])

    def test_same_tree_compares_raw_symlink_targets(self) -> None:
        left = self.wt / "link"
        right = self.main / "link"
        left.symlink_to("file/")
        right.symlink_to("file")

        self.assertFalse(rmcheck.same_tree(left, right))

    def test_compose_placeholder_entries_never_match_the_worktree(self) -> None:
        projects = [{"Name": "elsewhere", "Status": "running(1)", "ConfigFiles": "/other/compose.yaml,-"}]
        cwd = Path.cwd()
        os.chdir(self.wt)
        try:
            result = rmcheck.check(self.wt, self.main, base="main", compose_projects=projects)
        finally:
            os.chdir(cwd)

        self.assertEqual(result["containers"], [])

    def test_processes_in_orca_tabs_name_their_tab_and_idle_shells_do_not_block(self) -> None:
        wt = str(self.wt.resolve())
        host = "/Applications/Orca Terminal Host.app/Contents/MacOS/orca-terminal-host --socket s"
        older_host = "/Applications/Orca.app/Contents/Frameworks/Orca Helper.app/Contents/MacOS/Orca Helper " \
                     "/Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/daemon-entry.js --socket s"
        login = "/usr/bin/login -flpq me /bin/bash -c exec -l orca-tcc-login /bin/zsh /bin/zsh -l"
        table = {1: (0, "launchd"), 10: (1, host), 11: (1, older_host),
                 20: (11, login), 21: (20, "-/bin/zsh -l"),                       # a shell at its prompt
                 30: (10, login), 31: (30, "-/bin/zsh -l"), 32: (31, "codex --yolo"),  # an agent at work
                 40: (1, "node server.js")}                                       # started outside Orca
        cwds = {21: wt, 32: wt, 40: wt}
        with patch.object(rmcheck, "process_table", return_value=table), \
                patch.object(rmcheck, "process_cwds", return_value=cwds), \
                patch.object(rmcheck, "process_handles", return_value={32: "term_busy"}), \
                patch.object(rmcheck, "running", return_value=True):
            result = self.check()
        with patch.object(rmcheck, "process_table", return_value={pid: table[pid] for pid in (1, 11, 20, 21)}), \
                patch.object(rmcheck, "process_cwds", return_value={21: wt}), \
                patch.object(rmcheck, "process_handles", return_value={}):
            idle_only = self.check()

        tabs = {item["pid"]: (item["in_orca_tab"], item["idle_tab"], item["tab"]) for item in result["processes"]}
        self.assertEqual(tabs, {21: (True, True, None), 32: (True, False, "term_busy"), 40: (False, False, None)})
        self.assertEqual(len(result["blockers"]), 2)
        self.assertIn(f"{SCRIPTS / 'close_tabs.py'} --from SCAN_JSON term_busy", result["blockers"][1])
        self.assertIn("PID 40（node server.js、cwd %s）" % wt, result["blockers"][0])
        self.assertEqual((idle_only["ok"], idle_only["blockers"]), (True, []))

    def test_a_failed_pr_lookup_names_the_command_for_a_mac_where_gh_works(self) -> None:
        git(self.main, "remote", "add", "origin", "git@github.com:acme/app.git")
        head = git(self.wt, "rev-parse", "HEAD")
        real_run = subprocess.run

        def fake_run(cmd: list[str], **kwargs: Unpack[TextRunOptions]) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "gh":
                raise subprocess.CalledProcessError(1, cmd, stderr="HTTP 401: Bad credentials")
            return real_run(cmd, **kwargs)

        with patch.object(rmcheck.subprocess, "run", side_effect=fake_run):
            code, result = self.run_main("--pr", "7")

        self.assertEqual(code, 1)
        self.assertIn("merged_pr_head.py --repo acme/app --pr 7 --head " + head, " ".join(result["notes"]))

    def test_merged_pr_head_prints_rm_check_arguments_only_for_a_merged_pr(self) -> None:
        pr_head, head = "a" * 40, "b" * 40

        def gh(state: str) -> Callable[..., subprocess.CompletedProcess[str]]:
            def run(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                if cmd[:3] == ["gh", "pr", "view"]:
                    out = {"state": state, "headRefOid": pr_head}
                else:
                    self.assertEqual(cmd[:3], ["gh", "api", f"repos/acme/app/compare/{pr_head}...{head}"])
                    out = {"ahead_by": 0, "status": "behind"}
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
            return run

        argv = ["--repo", "acme/app", "--pr", "7", "--head", head]
        with patch.object(merged_pr_head.subprocess, "run", side_effect=gh("MERGED")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(merged_pr_head.main(argv), 0)
        self.assertEqual(out.getvalue(), f"--pr-head {pr_head} --head-ahead-of-pr 0\n")
        with patch.object(merged_pr_head.subprocess, "run", side_effect=gh("OPEN")), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(merged_pr_head.main(argv), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("OPEN", err.getvalue())

    def test_local_dependency_clones_are_disposable_only_when_clean_stash_free_and_on_a_remote(self) -> None:
        remote = Path(self.tmp.name) / "deps.git"
        git(self.main, "clone", "-q", "--bare", str(self.main), str(remote))
        copies = [self.wt / "local-deps" / name for name in ("first", "second")]
        for copy in copies:
            git(self.main, "clone", "-q", str(remote), str(copy))
            git(copy, "config", "user.email", "t@example.com")
            git(copy, "config", "user.name", "t")

        for ignored in (False, True):
            with self.subTest(ignored=ignored):
                (self.main / ".git/info/exclude").write_text("local-deps/\n" if ignored else "")
                code, result = self.run_main()
                self.assertEqual((code, result["ok"]), (0, True), result)
                self.assertTrue(all(str(copy.resolve()) in " ".join(result["notes"]) for copy in copies))
                self.assertTrue(all(copy.exists() for copy in copies))

        changed = copies[1]
        (changed / ".env").write_text("LOCAL=keep\n")
        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn(str(changed.resolve()), " ".join(result["reasons"]))
        (changed / ".env").unlink()
        (changed / "app.txt").write_text("local work\n")
        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn(str(changed.resolve()), " ".join(result["reasons"]))
        git(changed, "stash", "push", "-q")
        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("stash", " ".join(result["reasons"]))

        git(changed, "stash", "pop", "-q")
        git(changed, "commit", "-q", "-am", "unpushed")
        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn(str(changed.resolve()), " ".join(result["reasons"]))

        git(changed, "tag", "-a", "-m", "saved", "saved")
        git(changed, "push", "-q", "origin", "refs/tags/saved")
        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (0, True), result)
        git(changed, "push", "-q", "origin", ":refs/tags/saved")
        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn(str(changed.resolve()), " ".join(result["reasons"]))

    def test_local_dependency_clones_keep_commits_on_other_local_branches_and_tags(self) -> None:
        remote = Path(self.tmp.name) / "deps.git"
        git(self.main, "clone", "-q", "--bare", str(self.main), str(remote))
        copy = self.wt / "local-deps/lib"
        git(self.main, "clone", "-q", str(remote), str(copy))
        git(copy, "config", "user.email", "t@example.com")
        git(copy, "config", "user.name", "t")
        git(copy, "checkout", "-q", "-b", "draft")
        (copy / "app.txt").write_text("unpublished work\n")
        git(copy, "commit", "-q", "-am", "draft")
        git(copy, "checkout", "-q", "main")

        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False), result)
        self.assertIn("refs/heads/draft", " ".join(result["reasons"]))
        git(copy, "push", "-q", "origin", "draft")
        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (0, True), result)

        git(copy, "checkout", "-q", "--detach", "draft")
        (copy / "app.txt").write_text("tagged local work\n")
        git(copy, "commit", "-q", "-am", "local tag")
        git(copy, "tag", "local-only")
        git(copy, "checkout", "-q", "main")
        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False), result)
        self.assertIn("refs/tags/local-only", " ".join(result["reasons"]))

    def test_local_dependency_clones_keep_the_repository_of_an_external_linked_worktree(self) -> None:
        remote = Path(self.tmp.name) / "deps.git"
        git(self.main, "clone", "-q", "--bare", str(self.main), str(remote))
        copy = self.wt / "local-deps/lib"
        git(self.main, "clone", "-q", str(remote), str(copy))
        linked = Path(self.tmp.name) / "external-worktree"
        git(copy, "worktree", "add", "-q", "--detach", str(linked), "HEAD")

        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False), result)
        self.assertIn(str(linked.resolve()), " ".join(result["reasons"]))
        self.assertTrue((linked / ".git").is_file())

    def test_local_dependency_clones_keep_initialized_submodule_history(self) -> None:
        remote = Path(self.tmp.name) / "deps.git"
        git(self.main, "clone", "-q", "--bare", str(self.main), str(remote))
        copy = self.wt / "local-deps/lib"
        git(self.main, "clone", "-q", str(remote), str(copy))
        git(copy, "config", "user.email", "t@example.com")
        git(copy, "config", "user.name", "t")
        git(copy, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(remote), "child")
        git(copy, "commit", "-q", "-am", "submodule")
        git(copy, "push", "-q", "origin", "main")
        child = copy / "child"
        original = git(child, "rev-parse", "HEAD")
        git(child, "config", "user.email", "t@example.com")
        git(child, "config", "user.name", "t")
        git(child, "checkout", "-q", "-b", "unpublished")
        (child / "app.txt").write_text("unpublished child history\n")
        git(child, "commit", "-q", "-am", "child work")
        git(child, "checkout", "-q", "--detach", original)
        self.assertEqual(git(copy, "status", "--porcelain"), "")

        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False), result)
        self.assertIn("submodule", " ".join(result["reasons"]))

    def test_a_tracked_dependency_gitlink_change_still_blocks_removal(self) -> None:
        remote = Path(self.tmp.name) / "deps.git"
        git(self.main, "clone", "-q", "--bare", str(self.main), str(remote))
        copy = self.wt / "local-deps/lib"
        git(self.main, "clone", "-q", str(remote), str(copy))
        git(copy, "config", "user.email", "t@example.com")
        git(copy, "config", "user.name", "t")
        git(self.wt, "add", "local-deps/lib")
        git(self.wt, "commit", "-q", "-m", "pin dependency")
        git(self.main, "merge", "-q", "feature")
        (copy / "app.txt").write_text("updated dependency\n")
        git(copy, "commit", "-q", "-am", "dependency update")
        git(copy, "push", "-q", "origin", "main")

        code, result = self.run_main()
        self.assertEqual((code, result["ok"]), (1, False), result)
        self.assertIn("local-deps/lib", result["uncommitted"])

    def test_nested_worktrees_on_a_remote_are_detached_and_others_block(self) -> None:
        remote = Path(self.tmp.name) / "deps.git"
        git(Path(self.tmp.name), "init", "--bare", "-q", str(remote))
        deps = Path(self.tmp.name) / "deps"
        deps.mkdir()
        git(deps, "init", "-q", "-b", "main")
        git(deps, "config", "user.email", "t@example.com")
        git(deps, "config", "user.name", "t")
        (deps / "lib.txt").write_text("v1\n")
        git(deps, "add", "lib.txt")
        git(deps, "commit", "-q", "-m", "lib")
        git(deps, "remote", "add", "origin", str(remote))
        git(deps, "push", "-q", "-u", "origin", "main")
        (self.main / ".git" / "info" / "exclude").write_text("local-deps/\n")
        pushed, local = self.wt / "local-deps" / "pushed", self.wt / "local-deps" / "local"
        git(deps, "worktree", "add", "-q", "--detach", str(pushed), "main")
        git(deps, "worktree", "add", "-q", "-b", "draft", str(local), "main")
        (deps / ".git" / "info" / "exclude").write_text("*.egg-info/\n")
        (pushed / "src" / "deps.egg-info").mkdir(parents=True)
        (pushed / "src" / "deps.egg-info" / "PKG-INFO").write_text("Name: deps\n")
        (local / "lib.txt").write_text("v2\n")
        git(local, "commit", "-q", "-am", "unpushed")

        code, result = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual({Path(item["path"]).name: item["blockers"] == [] for item in result["nested_worktrees"]},
                         {"pushed": True, "local": False})
        self.assertTrue(pushed.exists())

        # While one copy cannot go, the worktree stays, so no copy is taken from it.
        code, result = self.run_main("--detach-nested")
        self.assertEqual((code, result["detached"]), (1, []))
        self.assertTrue(pushed.exists())
        self.assertIn("is on no remote branch", " ".join(result["blockers"]))

        git(local, "push", "-q", "origin", "draft")
        code, result = self.run_main("--detach-nested")
        self.assertEqual((code, result["ok"], len(result["detached"])), (0, True, 2))
        self.assertFalse(pushed.exists())
        self.assertNotIn(str(pushed.resolve()), git(deps, "worktree", "list"))
        self.assertEqual(result["nested_worktrees"], [])

        (self.wt / "draft.txt").write_text("kept work\n")
        git(deps, "worktree", "add", "-q", "--detach", str(pushed), "main")
        code, result = self.run_main("--detach-nested")
        self.assertEqual((code, result["detached"]), (1, []))
        self.assertTrue(pushed.exists())

    def nested_catalog(self) -> Path:
        remote = Path(self.tmp.name) / "deps.git"
        git(Path(self.tmp.name), "init", "--bare", "-q", str(remote))
        deps = Path(self.tmp.name) / "deps"
        deps.mkdir()
        git(deps, "init", "-q", "-b", "main")
        git(deps, "config", "user.email", "t@example.com")
        git(deps, "config", "user.name", "t")
        locale = deps / "locale/de/LC_MESSAGES"
        locale.mkdir(parents=True)
        (locale / "django.po").write_text('msgid "hello"\nmsgstr "Hallo"\n')
        (locale / "django.mo").write_bytes(b"compiled catalog")
        git(deps, "add", "locale")
        git(deps, "commit", "-q", "-m", "catalog")
        git(deps, "remote", "add", "origin", str(remote))
        git(deps, "push", "-q", "-u", "origin", "main")
        (self.main / ".git/info/exclude").write_text("local-deps/\n")
        copy = self.wt / "local-deps/pushed"
        git(deps, "worktree", "add", "-q", "--detach", str(copy), "main")
        (copy / "locale/de/LC_MESSAGES/django.mo").write_bytes(b"recompiled catalog")
        return copy

    def test_a_live_lock_in_a_nested_worktree_keeps_it_attached(self) -> None:
        copy = self.nested_catalog()
        (Path(self.tmp.name) / "deps/.git/info/exclude").write_text("**/.claude/scheduled_tasks.lock\n")
        (copy / ".claude").mkdir()
        holder = subprocess.Popen(["sleep", "30"])
        try:
            (copy / ".claude/scheduled_tasks.lock").write_text(
                json.dumps({"pid": holder.pid, "procStart": self.started_at(holder.pid)}))
            code, result = self.run_main("--detach-nested")
        finally:
            holder.kill()
            holder.wait()

        self.assertEqual((code, result["detached"]), (1, []))
        self.assertTrue(copy.exists())
        self.assertIn("PID %d" % holder.pid, " ".join(result["blockers"]))

    def started_at(self, pid: int) -> str:
        """ps's lstart in UTC, as a scheduled tasks lock records its holder's start."""
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                              env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout.strip()

    def test_nested_recompiled_catalog_is_restored_and_detached(self) -> None:
        copy = self.nested_catalog()
        rel = "locale/de/LC_MESSAGES/django.mo"

        _, before = self.run_main()
        self.assertEqual(before["nested_worktrees"][0]["regenerable"], [rel])

        code, result = self.run_main("--detach-nested")
        self.assertEqual((code, result["ok"], result["detached"]), (0, True, [str(copy.resolve())]))
        self.assertFalse(copy.exists())

    def test_nested_catalog_with_changed_source_blocks_detach(self) -> None:
        copy = self.nested_catalog()
        (copy / "locale/de/LC_MESSAGES/django.po").write_text('msgid "hello"\nmsgstr "Guten Tag"\n')

        code, result = self.run_main("--detach-nested")
        self.assertEqual((code, result["ok"], result["detached"]), (1, False, []))
        self.assertEqual(result["nested_worktrees"][0]["blockers"], ["2 uncommitted changes"])
        self.assertTrue(copy.exists())

    def test_a_failed_pr_lookup_blocks_with_a_reason(self) -> None:
        real_run = subprocess.run

        def fake_run(cmd: list[str], **kwargs: Unpack[TextRunOptions]) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "gh":
                raise FileNotFoundError("gh")
            return real_run(cmd, **kwargs)

        with patch.object(rmcheck.subprocess, "run", side_effect=fake_run):
            code, result = self.run_main("--pr", "7")

        self.assertEqual(code, 1)
        self.assertEqual(result["blockers"], ["PR #7 lookup failed: gh"])
        self.assertIn("--pr-head", " ".join(result["notes"]))

    def test_command_accepts_a_merged_pr_head_without_fetched_base(self) -> None:
        (self.wt / "app.txt").write_text("merged change\n")
        git(self.wt, "commit", "-q", "-am", "merged change")
        head = git(self.wt, "rev-parse", "HEAD")

        code, result = self.run_main("--pr-head", head, "--fetch-failed")

        self.assertEqual((code, result["ok"], result["blockers"]), (0, True, []))
        self.assertIn("git fetch failed", " ".join(result["notes"]))

    def test_command_accepts_an_ancestor_of_the_merged_pr_head(self) -> None:
        (self.wt / "app.txt").write_text("earlier PR commit\n")
        git(self.wt, "commit", "-q", "-am", "earlier PR commit")
        earlier = git(self.wt, "rev-parse", "HEAD")
        (self.wt / "later.txt").write_text("later PR commit\n")
        git(self.wt, "add", "later.txt")
        git(self.wt, "commit", "-q", "-m", "later PR commit")
        pr_head = git(self.wt, "rev-parse", "HEAD")
        git(self.wt, "reset", "--hard", earlier)

        code, result = self.run_main("--pr-head", pr_head)

        self.assertEqual((code, result["ok"], result["blockers"]), (0, True, []))

    def test_command_blocks_unpushed_commit_after_the_merged_pr_head(self) -> None:
        (self.wt / "app.txt").write_text("merged change\n")
        git(self.wt, "commit", "-q", "-am", "merged change")
        pr_head = git(self.wt, "rev-parse", "HEAD")
        (self.wt / "draft.txt").write_text("unpublished work\n")
        git(self.wt, "add", "draft.txt")
        git(self.wt, "commit", "-q", "-m", "local draft")

        code, result = self.run_main("--pr-head", pr_head)

        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("ahead of merged PR head", " ".join(result["blockers"]))

    def test_a_commit_after_pr_head_blocks_even_if_its_patch_is_in_base(self) -> None:
        (self.wt / "app.txt").write_text("merged change\n")
        git(self.wt, "commit", "-q", "-am", "merged change")
        pr_head = git(self.wt, "rev-parse", "HEAD")
        (self.wt / "draft.txt").write_text("later patch\n")
        git(self.wt, "add", "draft.txt")
        git(self.wt, "commit", "-q", "-m", "later patch")
        (self.main / "base.txt").write_text("base moved separately\n")
        git(self.main, "add", "base.txt")
        git(self.main, "commit", "-q", "-m", "base moved separately")
        git(self.main, "cherry-pick", pr_head)
        git(self.main, "cherry-pick", git(self.wt, "rev-parse", "HEAD"))

        code, result = self.run_main("--pr-head", pr_head)

        self.assertEqual(code, 1)
        self.assertIn("ahead of merged PR head", " ".join(result["blockers"]))

    def test_command_uses_compare_when_the_pr_head_object_is_missing(self) -> None:
        (self.wt / "app.txt").write_text("older PR commit\n")
        git(self.wt, "commit", "-q", "-am", "older PR commit")
        missing_pr_head = "a" * 40

        unknown_code, unknown = self.run_main("--pr-head", missing_pr_head)
        cleared_code, cleared = self.run_main(
            "--pr-head", missing_pr_head, "--head-ahead-of-pr", "0")
        blocked_code, blocked = self.run_main(
            "--pr-head", missing_pr_head, "--head-ahead-of-pr", "1")

        self.assertEqual(unknown_code, 1)
        self.assertIn("--head-ahead-of-pr", " ".join(unknown["notes"]))
        self.assertEqual((cleared_code, cleared["ok"], cleared["blockers"]), (0, True, []))
        self.assertEqual(blocked_code, 1)
        self.assertIn("ahead of merged PR head", " ".join(blocked["blockers"]))

    def test_stale_origin_fetch_is_reported_from_shared_git_state(self) -> None:
        bare = Path(self.tmp.name) / "remote.git"
        bare.mkdir()
        git(bare, "init", "--bare", "-q")
        git(self.main, "remote", "add", "origin", str(bare))
        git(self.main, "push", "-q", "origin", "main")
        git(self.main, "fetch", "-q", "origin", "main")
        common = Path(git(self.wt, "rev-parse", "--git-common-dir"))
        fetched = common / "FETCH_HEAD"
        fetched.write_text(fetched.read_text().replace(
            str(bare).removesuffix(".git"), "https://git.example.invalid/team/remote"))
        git(self.main, "remote", "set-url", "origin", "git@git.example.invalid:team/remote.git")
        old = datetime.fromisoformat("2020-01-02T00:00:00+00:00").timestamp()
        os.utime(fetched, (old, old))

        _, stale = self.run_main_without_base()
        pr = {"number": 7, "state": "MERGED", "headRefOid": git(self.wt, "rev-parse", "HEAD")}
        _, with_pr = self.run_main_without_base("--pr", "7", pr=pr)
        git(self.main, "remote", "set-url", "origin", str(bare))
        git(self.wt, "fetch", "-q", "origin", "main")
        _, fresh = self.run_main_without_base()

        self.assertEqual(stale["base"], "origin/main")
        self.assertIn("origin の参照は 2020-01-02T00:00:00Z の fetch のまま", " ".join(stale["notes"]))
        self.assertNotIn("--pr-head", " ".join(stale["notes"]))
        self.assertIn("--pr-head", " ".join(with_pr["notes"]))
        self.assertNotIn("origin の参照は", " ".join(fresh.get("notes", [])))

    def run_main(self, *argv: str, notes: FakeWorktreeNotes | None = None,
                 docker: bool = False) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(self.wt), "--base", "main",
                                 *([] if docker else ["--no-docker"]), *argv],
                                worktree_notes=notes or FakeWorktreeNotes())
        return code, json.loads(out.getvalue())

    def run_main_without_base(self, *argv: str, pr: dict | None = None) -> tuple[int, dict]:
        real_run = subprocess.run

        def fake_run(cmd: list[str], **kwargs: Unpack[TextRunOptions]) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(pr), stderr="")
            return real_run(cmd, **kwargs)

        out = io.StringIO()
        with patch.object(rmcheck.subprocess, "run", side_effect=fake_run), redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(self.wt), "--no-docker", *argv], worktree_notes=FakeWorktreeNotes())
        return code, json.loads(out.getvalue())

    def remote_default_and_working_base(self) -> str:
        """origin/HEAD -> origin/main at the first commit; origin/develop holds the worktree's commit."""
        first = git(self.main, "rev-parse", "main")
        git(self.main, "update-ref", "refs/remotes/origin/main", first)
        git(self.main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        head = git(self.wt, "rev-parse", "HEAD")
        git(self.main, "update-ref", "refs/remotes/origin/develop", head)
        return head

    def test_command_checks_against_the_base_branch_of_the_pr(self) -> None:
        # A repository whose default branch is not where the work merges (cxc merges into feat/nature).
        head = self.remote_default_and_working_base()
        pr = {"number": 7, "state": "OPEN", "headRefOid": head, "baseRefName": "develop"}

        default_code, default = self.run_main_without_base()
        pr_code, by_pr = self.run_main_without_base("--pr", "7", pr=pr)

        self.assertEqual((default_code, default["base"]), (1, "origin/HEAD"))
        self.assertEqual((pr_code, by_pr["base"], by_pr["blockers"]), (0, "origin/develop", []))

    def test_a_pr_base_branch_missing_locally_is_named_in_the_notes(self) -> None:
        head = self.remote_default_and_working_base()
        pr = {"number": 7, "state": "OPEN", "headRefOid": head, "baseRefName": "release"}

        code, result = self.run_main_without_base("--pr", "7", pr=pr)

        self.assertEqual((code, result["base"]), (0, "origin/HEAD"))
        self.assertEqual(result["notes"], ["PR #7 base origin/release is not fetched: fetch it or pass --base"])

    def test_command_checks_against_the_remote_branch_the_worktree_started_from(self) -> None:
        first = git(self.main, "rev-parse", "main")
        for ref in ("refs/remotes/origin/main", "refs/remotes/origin/develop"):
            git(self.main, "update-ref", ref, first)
        git(self.main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.wt = Path(self.tmp.name) / "topic"
        git(self.main, "worktree", "add", "-q", "-b", "topic", str(self.wt), "refs/remotes/origin/develop")
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        git(self.main, "update-ref", "refs/remotes/origin/develop", git(self.wt, "rev-parse", "HEAD"))

        code, result = self.run_main_without_base()

        self.assertEqual((code, result["base"], result["blockers"]), (0, "origin/develop", []))

    def test_a_branch_started_from_its_own_remote_branch_uses_its_upstream_without_a_pr(self) -> None:
        git(self.main, "remote", "add", "origin", "https://git.example.invalid/team/app.git")
        first = git(self.main, "rev-parse", "main")
        for ref in ("refs/remotes/origin/main", "refs/remotes/origin/topic"):
            git(self.main, "update-ref", ref, first)
        git(self.main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.wt = Path(self.tmp.name) / "topic"
        git(self.main, "worktree", "add", "-q", "-b", "topic", str(self.wt), "refs/remotes/origin/topic")
        git(self.wt, "branch", "--set-upstream-to", "origin/topic")
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "work in progress")
        head = git(self.wt, "rev-parse", "HEAD")
        git(self.main, "update-ref", "refs/remotes/origin/topic", head)

        code, result = self.run_main_without_base()

        self.assertEqual((code, result["base"], result["blockers"]), (0, "origin/topic", []))
        self.assertIn("origin/topic", " ".join(result["notes"]))
        self.assertNotIn("--pr-head", " ".join(result["notes"]))

    def test_an_unpushed_head_keeps_the_default_base_without_pr_head_advice(self) -> None:
        head = self.remote_default_and_working_base()
        git(self.main, "remote", "add", "origin", "https://git.example.invalid/team/app.git")
        git(self.main, "update-ref", "refs/remotes/origin/release/masters-0026", head)
        git(self.wt, "branch", "--set-upstream-to", "origin/release/masters-0026")
        (self.wt / "app.txt").write_text("unpushed\n")
        git(self.wt, "commit", "-q", "-am", "unpushed work")
        code, result = self.run_main_without_base()
        git(self.main, "branch", "local-copy", git(self.wt, "rev-parse", "HEAD"))
        git(self.wt, "branch", "--set-upstream-to", "local-copy")
        local_code, local = self.run_main_without_base()

        self.assertEqual((code, result["base"]), (1, "origin/HEAD"))
        self.assertNotIn("--pr-head", " ".join(result.get("notes", [])))
        self.assertEqual((local_code, local["base"]), (1, "origin/HEAD"))

    def test_command_finds_the_main_checkout_and_fails_when_blocked(self) -> None:
        (self.main / ".env").write_text("A=1\n")
        (self.wt / ".env").write_text("A=1\n")
        clean_code, clean = self.run_main()
        (self.wt / "app.txt").write_text("v2\n")
        dirty_code, dirty = self.run_main()

        self.assertEqual((clean_code, clean["ok"], clean["main"]), (0, True, str(self.main.resolve())))
        self.assertEqual((dirty_code, dirty["blockers"]), (1, ["1 uncommitted changes"]))

    def test_command_without_a_base_ref_says_so(self) -> None:
        git(self.main, "branch", "-m", "main", "trunk")
        (self.wt / "app.txt").write_text("v2\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(self.wt), "--no-docker"], worktree_notes=FakeWorktreeNotes())

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["blockers"],
                         ["1 uncommitted changes",
                          "no base ref found (origin/HEAD, origin/main, origin/master, main, master); pass --base"])

    def test_a_repository_with_no_remote_is_checked_against_its_local_main(self) -> None:
        # 2026-11-10: an orca worktree whose repository has only an Initial commit and no remote stopped at
        # "no base ref found" until the hourly tab passed --base main.
        code, result = self.run_main_without_base()
        self.assertEqual((code, result["base"], result["blockers"]), (0, "main", []))
        self.assertIn("Using main as base: the repository has no origin/HEAD, origin/main, origin/master, so the "
                      "local branch the main checkout keeps stands in", result["notes"])

        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "unmerged")
        code, result = self.run_main_without_base()
        self.assertEqual(code, 1)
        self.assertRegex(" ".join(result["blockers"]), r"HEAD \w{8} is not in main")

    def test_stale_remote_refs_are_fetched_before_judging(self) -> None:
        bare = Path(self.tmp.name) / "remote.git"
        git(Path(self.tmp.name), "init", "--bare", "-q", "-b", "main", str(bare))
        git(self.main, "remote", "add", "origin", str(bare))
        git(self.main, "push", "-q", "origin", "main")
        git(self.main, "fetch", "-q", "origin")
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        other = Path(self.tmp.name) / "other"
        git(Path(self.tmp.name), "clone", "-q", str(bare), str(other))
        git(other, "fetch", "-q", str(self.wt), "feature")
        git(other, "merge", "-q", "--ff-only", "FETCH_HEAD")
        git(other, "push", "-q", "origin", "main")
        fetched = Path(git(self.wt, "rev-parse", "--git-common-dir")) / "FETCH_HEAD"
        old = (datetime.now() - timedelta(hours=2)).timestamp()
        os.utime(fetched, (old, old))

        code, result = self.run_main_without_base()

        self.assertEqual((code, result["blockers"]), (0, []))

    def test_the_remote_the_branch_started_from_is_fetched_before_judging(self) -> None:
        bare = Path(self.tmp.name) / "upstream.git"
        git(Path(self.tmp.name), "init", "--bare", "-q", "-b", "main", str(bare))
        git(self.main, "remote", "add", "upstream", str(bare))
        git(self.main, "push", "-q", "upstream", "main")
        git(self.main, "fetch", "-q", "upstream")
        self.wt = Path(self.tmp.name) / "from-upstream"
        git(self.main, "worktree", "add", "-q", "-b", "fork-work", str(self.wt), "upstream/main")
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        git(self.wt, "push", "-q", str(bare), "fork-work:main")
        fetched = Path(git(self.wt, "rev-parse", "--git-common-dir")) / "FETCH_HEAD"
        old = (datetime.now() - timedelta(hours=2)).timestamp()
        os.utime(fetched, (old, old))

        code, result = self.run_main_without_base()

        self.assertEqual((code, result["blockers"]), (0, []))

    def test_command_accepts_the_worktree_and_pr_as_positional_arguments(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = rmcheck.main([str(self.wt), "-", "--base", "main", "--no-docker"],
                                worktree_notes=FakeWorktreeNotes())

        self.assertEqual((code, json.loads(out.getvalue())["worktree"]), (0, str(self.wt.resolve())))

    def test_command_rejects_a_path_that_is_not_a_git_worktree(self) -> None:
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        out = io.StringIO()
        with redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(plain), "--no-docker"])

        self.assertEqual(code, 2)
        [blocker] = json.loads(out.getvalue())["blockers"]
        self.assertRegex(blocker, r"^not a git worktree: git rev-parse exited 128: fatal: not a git repository")


class RmCheckConfirmGoneTests(unittest.TestCase):
    """--confirm-gone after `orca worktree rm`: gone only when no Orca row's path names it and the disk lacks it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.removed = self.dir / "workspaces" / "fix-login"

    def run_confirm(self, *rows: dict) -> tuple[int, dict]:
        orca = self.dir / "orca"
        payload = {"ok": True, "result": {"worktrees": list(rows), "truncated": False}}
        orca.write_text(f"#!{sys.executable}\nprint({json.dumps(json.dumps(payload))})\n")
        orca.chmod(0o755)
        with redirect_stdout(io.StringIO()) as out:
            code = rmcheck.main(["--worktree", str(self.removed), "--confirm-gone", "0", "--orca", str(orca)])
        return code, json.loads(out.getvalue())

    def test_a_removed_worktree_named_only_in_another_rows_tab_output_is_gone(self) -> None:
        # 2026-10-29: matching the name anywhere in `orca worktree ps --json` called a removed worktree present.
        other = {"path": str(self.dir / "workspaces" / "triage"), "preview": f"removed {self.removed}"}

        code, result = self.run_confirm(other)

        self.assertEqual((code, result["gone"], result["in_orca"], result["on_disk"]), (0, True, False, False))

    def test_a_worktree_orca_still_lists_is_reported_stuck(self) -> None:
        self.removed.mkdir(parents=True)

        code, result = self.run_confirm({"path": str(self.removed)})

        self.assertEqual((code, result["gone"], result["in_orca"], result["on_disk"]), (1, False, True, True))


NOTHING_OWED = {"needs_owner": [], "closed_issues": []}


def gh_issue(url: str, number: int, closing_prs: tuple[int, ...] = ()) -> dict:
    return {"number": number, "title": f"issue {number}", "url": url + str(number), "assignees": [],
            "closedByPullRequestsReferences": [{"number": pr} for pr in closing_prs]}


class LeadDecisionProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lead = handoff.lead_decisions
        self.log = self.root / "decisions.jsonl"
        self.handed = self.root / "handed.jsonl"
        self.inbox = self.root / "inbox.jsonl"
        self.answers = self.root / "answers.jsonl"
        environment = patch.dict(os.environ, {"TRIAGE_INBOX": str(self.inbox),
                                              "TRIAGE_ASK_ANSWERS": str(self.answers)})
        environment.start()
        self.addCleanup(environment.stop)

    def call(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.lead.main(["--decisions-file", str(self.log), *args])
        return code, out.getvalue()

    def test_show_match_searches_keys_and_answers_with_any_word_ignoring_case(self) -> None:
        self.lead.append(self.log,
                         self.lead.Decision("1", "merge:Acme/app#1", "Hold\nfor review", "2026-10-01T01:00:00Z"),
                         self.lead.Decision("2", "remove:/w/a", "READY to remove", "2026-10-01T02:00:00Z"),
                         self.lead.Decision("3", "keep:/w/b", "wait", "2026-10-01T03:00:00Z"))

        code, shown = self.call("show", "--match", "ACME", "--match", "ready")

        self.assertEqual(code, 0)
        self.assertEqual(shown.splitlines(), ["2026-10-01T11:00:00+09:00 remove:/w/a  READY to remove",
                                              "2026-10-01T10:00:00+09:00 merge:Acme/app#1  Hold for review"])

    def test_show_json_gives_the_whole_answer_a_line_cuts_and_the_line_names_the_cut(self) -> None:
        # A conditional answer cut at 160 characters reads as unconditional.
        answer = "merge する。" + "経過" * 90 + "ただし CI が green のときだけ"
        self.lead.append(self.log, self.lead.Decision("1", "merge:Acme/app#1", answer, "2026-10-01T01:00:00Z"))

        line = self.call("show", "--match", "acme")[1]
        code, shown = self.call("show", "--match", "acme", "--json")

        self.assertTrue(line.endswith("…（続きは --json）\n"), line)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(shown), [{"id": "1", "decision": "merge:Acme/app#1", "answer": answer,
                                              "decided_at": "2026-10-01T01:00:00Z", "value": None, "marks": []}])

    def test_show_since_newest_handoff_sorts_in_jst_and_marks_existing_remove_paths(self) -> None:
        kept = self.root / "kept"
        kept.mkdir()
        gone = self.root / "gone"
        directory = self.root / "handoffs"
        for stamp in ("2026-10-01T00:00:00+00:00", "2026-10-01T02:00:00+00:00"):
            handoff.SavedHandoff("body", "info", [], stamp, 0).save(directory, datetime.fromisoformat(stamp))
        self.lead.append(self.log,
                         self.lead.Decision("1", f"remove:{kept}", "remove it", "2026-10-01T11:00:00+09:00"),
                         self.lead.Decision("2", f"remove:mac::{gone}", "done", "2026-10-01T03:00:00Z"),
                         self.lead.Decision("3", "old", "old answer", "2026-10-01T01:00:00Z"))

        code, shown = self.call("show", "--since-last-handoff", "--handoffs-dir", str(directory))

        self.assertEqual(code, 0)
        self.assertEqual(shown.splitlines(), [f"2026-10-01T12:00:00+09:00 remove:mac::{gone}  done",
                                              f"2026-10-01T11:00:00+09:00 remove:{kept} 未実行  remove it"])

    def test_unanswered_records_first_handoff_and_total_count_per_key(self) -> None:
        write_jsonl(self.handed, [{"decision": key, "handed_off_at": stamp} for key, stamp in [
            ("keep:/w/a", "2026-10-01T01:00:00Z"), ("keep:/w/b", "2026-10-01T02:00:00Z"),
            ("keep:/w/a", "2026-10-01T03:00:00Z")]])

        code, shown = self.call("unanswered", "--handed-off-file", str(self.handed))

        self.assertEqual(code, 0)
        result = json.loads(shown)
        self.assertEqual([(item["decision"], item["first_handed_off_at"], item["handed_off_at"], item["handoff_count"])
                          for item in result["unanswered"]],
                         [("keep:/w/a", "2026-10-01T01:00:00Z", "2026-10-01T03:00:00Z", 2),
                          ("keep:/w/b", "2026-10-01T02:00:00Z", "2026-10-01T02:00:00Z", 1)])
        self.assertEqual(result["settled"], [])

    def test_unanswered_settles_a_structured_ask_answered_question_by_question(self) -> None:
        write_jsonl(self.inbox, [{"id": "ask2", "kind": "ask", "worktree": "lane", "ts": "2026-10-01T00:00:00Z",
                                  "text": "two", "questions": [{"question": "A?"}, {"question": "B?"}]}])
        write_jsonl(self.handed, [{"decision": "whole", "handed_off_at": "2026-10-01T01:00:00Z", "ask_id": "ask2"},
                                  {"decision": "one", "handed_off_at": "2026-10-01T01:00:00Z", "ask_id": "ask3:1"}])
        write_jsonl(self.answers, [{"id": "ask2:1", "at": "2026-10-01T02:00:00Z", "how": "send", "note": "A"},
                                   {"id": "ask2:2", "at": "2026-10-01T02:01:00Z", "how": "send", "note": "B"},
                                   {"id": "ask3", "at": "2026-10-01T02:02:00Z", "how": "send", "note": "all"}])

        code, shown = self.call("unanswered", "--handed-off-file", str(self.handed))

        self.assertEqual(code, 0)
        result = json.loads(shown)
        self.assertEqual(result["unanswered"], [])
        self.assertEqual([item["decision"] for item in result["settled"]], ["whole", "one"])

    def test_unanswered_settles_bound_asks_with_the_answer_record_and_reads_old_handoffs(self) -> None:
        write_jsonl(self.handed, [{"decision": "bound", "handed_off_at": "2026-10-01T01:00:00Z", "ask_id": "ask1"},
                                  {"decision": "bound", "handed_off_at": "2026-10-01T02:00:00Z"},
                                  {"decision": "old", "handed_off_at": "2026-10-01T01:00:00Z"},
                                  {"decision": "direct", "handed_off_at": "2026-10-01T01:00:00Z"}])
        answer = {"id": "ask1", "at": "2026-10-01T02:00:00Z", "how": "send", "note": "go ahead"}
        self.answers.write_text("not JSON\n" + json.dumps(answer))
        decision = self.lead.Decision("d", "direct", "wait", "2026-10-01T02:00:00Z")
        self.lead.append(self.log, decision)

        code, shown = self.call("unanswered", "--handed-off-file", str(self.handed))

        self.assertEqual(code, 0)
        result = json.loads(shown)
        self.assertEqual([item["decision"] for item in result["unanswered"]], ["old"])
        self.assertEqual(result["settled"], [{"decision": "bound", "answer": answer},
                                              {"decision": "direct", "answer": self.lead.asdict(decision)}])
        code, selected = self.call("unanswered", "--handed-off-file", str(self.handed),
                                   "--decision", "bound", "--decision", "new")
        self.assertEqual(code, 0)
        self.assertEqual([item["decision"] for item in json.loads(selected)["unanswered"]], ["new"])

    def test_pr_keys_attach_named_reports_around_the_last_handoff_and_optional_asana(self) -> None:
        key = "merge:acme/app#12+#14"
        stamp = "2026-10-01T02:00:00Z"
        write_jsonl(self.handed, [{"decision": key, "handed_off_at": "2026-10-01T00:00:00Z"},
                                  {"decision": key, "handed_off_at": stamp}])
        reports = [{"id": "key", "ts": "2026-10-01T01:00:00Z", "kind": "done", "worktree": "a", "text": key},
                   {"id": "number", "ts": "2026-10-01T01:30:00Z", "kind": "info", "worktree": "b",
                    "text": "Ready: acme/app#14 " + "x" * 200},
                   {"id": "url", "ts": "2026-10-01T03:00:00Z", "kind": "info", "worktree": "c",
                    "text": "https://github.com/acme/app/pull/12を確認"},
                   {"id": "other", "ts": "2026-10-01T03:00:00Z", "text": "acme/app#120"}]
        write_jsonl(self.inbox, reports)
        status = self.root / "asana.json"
        status.write_text(json.dumps([{"url": f"https://github.com/acme/app/pull/{number}",
                                      "asana": {"completed": completed}} for number, completed in [(12, True), (14, False)]]))

        def gh(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(args[:2], ["gh", "api"])
            return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"repository": {
                f"item_{number}": {"__typename": "PullRequest", "state": "OPEN", "updatedAt": stamp}
                for number in (12, 14)}}}), "")

        with patch("subprocess.run", side_effect=gh) as run:
            code, shown = self.call("unanswered", "--handed-off-file", str(self.handed), "--asana-status", str(status))
            _, without = self.call("unanswered", "--handed-off-file", str(self.handed))

        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        row = json.loads(shown)["unanswered"][0]
        self.assertEqual(row["reports"], {"before": [{**reports[0]}, {**reports[1], "text": reports[1]["text"][:160]}],
                                           "after": [reports[2]]})
        self.assertEqual([pr["asana"] for pr in row["prs"]], [{"completed": True}, {"completed": False}])
        self.assertTrue(all("asana" not in pr for pr in json.loads(without)["unanswered"][0]["prs"]))

    def test_an_issue_key_reads_the_issue_and_names_the_worktrees_working_on_it(self) -> None:
        # 2026-11-10: spawn:Eotel/skills#159 named an issue, was read as a PR, and printed nulls.
        write_jsonl(self.handed, [{"decision": "spawn:acme/app#159", "handed_off_at": "2026-10-01T02:00:00Z"}])
        nodes = {"item_159": {"__typename": "Issue", "state": "OPEN", "updatedAt": "2026-10-01T03:00:00Z",
                              "closedByPullRequestsReferences": {"nodes": [{"number": 160}]}}}
        worktrees = [{"path": f"/w/{name}", "repo": repo, "_environment": "mbp16", "linkedIssue": issue,
                      "linkedItems": [{"type": "pr", "number": pr}] if pr else [], "linkedPR": None}
                     for name, repo, issue, pr in [("app-159", "app", 159, None), ("app-pr160", "app", None, 160),
                                                   ("other-159", "other", 159, None), ("app-1", "app", 1, None)]]

        with patch.object(scan, "graphql_repository", return_value=nodes), \
                patch.object(hourly_state, "orca_worktrees", return_value=worktrees):
            code, shown = self.call("unanswered", "--handed-off-file", str(self.handed))

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(shown)["unanswered"][0]["prs"], [{
            "repo": "acme/app", "number": 159, "type": "issue", "state": "OPEN", "updatedAt": "2026-10-01T03:00:00Z",
            "updated_after_handoff": True, "worktrees": ["mbp16::/w/app-159", "mbp16::/w/app-pr160"]}])


class HandoffProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.inbox = self.root / "inbox.jsonl"
        self.inbox.write_text("")
        self.state = self.root / "hourly.json"
        self.body = self.root / "body.txt"
        self.body.write_text("handoff body")
        self.handed = self.root / "handed.jsonl"
        environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                                              "TRIAGE_INBOX": str(self.inbox),
                                              "TRIAGE_ASK_ANSWERS": str(self.root / "answers.jsonl")})
        environment.start()
        self.addCleanup(environment.stop)
        processes = patch.object(handoff.scan_sessions, "process_table", return_value={})
        processes.start()
        self.addCleanup(processes.stop)
        handoffs = patch.object(handoff, "HANDOFFS", self.root / "handoffs")
        handoffs.start()
        self.addCleanup(handoffs.stop)

    def call(self, action: str, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = handoff.main([action, "--state-file", str(self.state), "--claude-projects", str(self.projects), *args])
        return code, out.getvalue(), err.getvalue()

    def send(self, *args: str) -> tuple[int, str, str]:
        return self.call("send", "--inbox", str(self.inbox), "--body-file", str(self.body), "--since", "0",
                         "--decisions-file", str(self.root / "decisions.jsonl"), "--handed-off-file", str(self.handed),
                         "--handoffs-dir", str(self.root / "handoffs"), "--notify", "fake-notify", *args)

    def test_send_binds_an_ask_to_each_decision_log_entry(self) -> None:
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            code, _, err = self.send("--decision", "keep:/w/a", "--decision-ask", "keep:/w/a=ask1",
                                     "--decision", "keep:/w/b")

        self.assertEqual(code, 0, err)
        self.assertEqual(run.call_args.args[0][:3], ["fake-notify", "--kind", "ask"])
        self.assertEqual([(item.decision, item.ask_id) for item in handoff.lead_decisions.handed_off(self.handed)],
                         [("keep:/w/a", "ask1"), ("keep:/w/b", None)])

    def test_send_exempts_only_answers_delivered_by_this_worktree_and_conversation(self) -> None:
        ask_id = "f439c015-aaaa-bbbb-cccc-000000000001"
        write_jsonl(self.inbox, [{"id": ask_id, "ts": "2026-11-14T00:00:00Z", "kind": "ask",
                                  "worktree": "lane", "text": "Publish?"}])
        self.body.write_text("ask f439c015 は答えを届けた")
        with patch.dict(os.environ, {"ORCA_WORKTREE_ID": "repo::/w/triage", "CLAUDE_CODE_SESSION_ID": "run-1",
                                      "CODEX_THREAD_ID": "", "CLAUDE_SESSION_ID": ""}):
            inbox_read.record_answers(self.root / "answers.jsonl", [ask_id], "send --decision", "Publish")
        for worktree_path, session, allowed in [("/w/triage", "run-1", True), ("/w/triage", "run-2", False),
                                                  ("/w/other", "run-1", False), ("/w/triage", "", False)]:
            with self.subTest(worktree=worktree_path, session=session), \
                    patch.dict(os.environ, {"ORCA_WORKTREE_ID": f"repo::{worktree_path}",
                                             "CLAUDE_CODE_SESSION_ID": session, "CODEX_THREAD_ID": "",
                                             "CLAUDE_SESSION_ID": "", "CLAUDE_PID": ""}), \
                    patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
                code, output, error = self.send()
                self.assertEqual(code, 0 if allowed else 1, error)
                self.assertEqual("この回の送信" in error, allowed)
                self.assertEqual("NEXT_OFFSET=" in output, allowed)

    def test_lead_state_reads_agents_questions_and_screen_from_the_appointed_worktree(self) -> None:
        self.state.write_text(json.dumps({"version": 1, "left": {}, "questions": {}, "handoffs": {},
                                          "lead": {"worktree": "/w/lead"}}))
        question = {"questions": [{"question": "Merge?", "options": [{"label": "Yes"}, {"label": "Wait"}]}]}
        write_jsonl(self.projects / "-w-lead" / "old.jsonl", [assistant(text("old"))])
        write_jsonl(self.projects / "-w-lead" / "new.jsonl", [
            # A question the user moved on from is not pending any more.
            assistant({"type": "tool_use", "id": "abandoned", "name": "AskUserQuestion", "input": question}),
            user("go"), {**assistant(text("latest")), "timestamp": "2026-10-01T01:00:00Z"},
            assistant({"type": "tool_use", "id": "answered", "name": "AskUserQuestion", "input": question}),
            user([{"type": "tool_result", "tool_use_id": "answered", "content": "yes"}]),
            {**assistant({"type": "tool_use", "id": "pending", "name": "AskUserQuestion", "input": question}),
             "timestamp": "2026-10-01T02:00:00Z"},
            {"type": "queue-operation", "operation": "enqueue", "content": "undelivered",
             "timestamp": "2026-10-01T02:01:00Z"}])
        os.utime(self.projects / "-w-lead" / "old.jsonl", (1, 1))
        agent = done_agent("claude", "tab:leaf")
        row = {**worktree("/w/lead"), "agents": [agent]}
        replies: dict[tuple[str, ...], dict] = {("worktree", "ps"): {"worktrees": [worktree("/w/other"), row]},
                   ("terminal", "list"): {"terminals": [terminal("/w/other", "claude", "other"),
                                                           terminal("/w/lead", "claude", "lead-tab")]},
                   ("terminal", "read", "--terminal", "lead-tab", "--screen"):
                       {"terminal": {"source": "screen", "tail": ["latest", "❯"]}}}

        def orca(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(args[0], "fake-orca")
            self.assertEqual(args[-1], "--json")
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": replies[tuple(args[1:-1])]}))

        with patch("subprocess.run", side_effect=orca) as run:
            code, shown, err = self.call("lead-state", "--orca", "fake-orca")

        self.assertEqual(code, 0, err)
        result = json.loads(shown)
        self.assertEqual(result["worktree"], "/w/lead")
        self.assertEqual(result["agents"][0]["state"], "done")
        self.assertEqual(result["agents"][0]["stopped_at"], scan.iso_time(agent["stateStartedAt"]))
        self.assertEqual(result["final"], {"at": "2026-10-01T01:00:00Z", "text": "latest"})
        self.assertEqual(result["pending_questions"], [{"id": "pending", "asked_at": "2026-10-01T02:00:00Z", **question}])
        self.assertEqual(result["terminals"], [{"handle": "lead-tab", "screen_tail": ["latest", "❯"]}])
        self.assertEqual([(item["text"], item["enqueued_at"]) for item in result["queued_input"]],
                         [("undelivered", "2026-10-01T02:01:00Z")])
        self.assertIsInstance(result["queued_input"][0]["minutes"], int)
        self.assertEqual(run.call_count, 3)

    def test_lead_state_shows_the_last_ordinary_message_and_answers_since_the_last_handoff_past_a_limit(self) -> None:
        self.state.write_text(json.dumps({"version": 1, "left": {}, "questions": {}, "handoffs": {},
                                          "lead": {"worktree": "/w/lead"}}))
        question = {"questions": [{"question": "Merge?", "options": [{"label": "Yes"}, {"label": "Wait"}]}]}

        def asked(tool_id: str, at: str) -> dict:
            return {**assistant({"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": question}),
                    "timestamp": at}

        def answered(tool_id: str, at: str, choice: str, **notes: str) -> dict:
            return {"type": "user", "timestamp": at, "toolUseResult": {
                        **question, "answers": {"Merge?": choice},
                        "annotations": {"Merge?": {"notes": note} for note in notes.values()}},
                    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                             "content": choice}]}}

        write_jsonl(self.projects / "-w-lead" / "lead.jsonl", [
            asked("before", "2026-11-09T00:00:00Z"), answered("before", "2026-11-09T00:01:00Z", "Yes"),
            {**assistant(text("まとめました")), "timestamp": "2026-11-09T01:10:00Z"},
            asked("after", "2026-11-09T01:20:00Z"), answered("after", "2026-11-09T01:21:00Z", "Wait", note="CI の後で"),
            {**user("続けて"), "timestamp": "2026-11-09T01:30:00Z"},
            {**assistant(text("You've hit your weekly limit · resets 8pm")), "timestamp": "2026-11-09T01:31:00Z",
             "isApiErrorMessage": True}])
        handoff.SavedHandoff(text="前回", kind="info", decisions=[], sent_at="2026-11-09T01:00:00+00:00",
                             next_offset=0).save(self.root / "handoffs", datetime(2026, 11, 9, 1, tzinfo=timezone.utc))
        replies: dict[tuple[str, ...], dict] = {("worktree", "ps"): {"worktrees": [{**worktree("/w/lead"), "agents": []}]},
                                                ("terminal", "list"): {"terminals": []}}

        with patch("subprocess.run", side_effect=lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, json.dumps({"result": replies[tuple(args[1:-1])]}))):
            code, shown, err = self.call("lead-state", "--orca", "fake-orca")

        self.assertEqual(code, 0, err)
        result = json.loads(shown)
        self.assertEqual(result["final"], {"at": "2026-11-09T01:10:00Z", "text": "まとめました"})
        self.assertEqual(result["answered"], [{"id": "after", "asked_at": "2026-11-09T01:20:00Z",
                                               "answered_at": "2026-11-09T01:21:00Z", "answers": {"Merge?": "Wait"},
                                               "notes": {"Merge?": "CI の後で"}}])
        self.assertEqual(result["transcript"], str(self.projects / "-w-lead" / "lead.jsonl"))
        self.assertEqual(result["user_messages"], [{"at": "2026-11-09T01:30:00Z", "text": "続けて"}])

    def test_lead_state_clears_only_a_connection_error_followed_by_a_new_reply_and_running_line(self) -> None:
        self.state.write_text(json.dumps({"version": 1, "left": {}, "questions": {}, "handoffs": {},
                                          "lead": {"worktree": "/w/lead"}}))
        error = "API Error: Your computer went to sleep mid-response. The response above may be incomplete."
        progress = "✽ Transmuting… (14m 25s · ↓ 2.7k tokens · thinking with xhigh effort)"
        write_jsonl(self.projects / "-w-lead" / "lead.jsonl", [
            assistant(text("前の発言")), {**assistant(text(error)), "isApiErrorMessage": True, "error": "server_error"}])
        replies = {("worktree", "ps"): {"worktrees": [worktree("/w/lead")]},
                   ("terminal", "list"): {"terminals": [terminal("/w/lead", "claude", "term_lead")]}}
        for lines, recovered in [([error, "⏺ 検査を続けています。", progress], True),
                                  ([error, "⏺ 検査を続けています。", progress,
                                    "⎿  API Error: Can't reach the API server", "❯"], False),
                                  (["⏺ 前の発言", progress, error, "❯"], False)]:
            with self.subTest(recovered=recovered), patch("subprocess.run", side_effect=lambda args, **kwargs:
                    subprocess.CompletedProcess(args, 0, json.dumps({"result": replies[tuple(args[1:-1])]}))):
                replies[("terminal", "read", "--terminal", "term_lead", "--screen")] = {
                    "terminal": {"source": "screen", "tail": lines}}
                code, shown, err = self.call("lead-state")
            self.assertEqual(code, 0, err)
            result = json.loads(shown)
            self.assertEqual(result["api_error"], None if recovered else error)
            self.assertEqual(result["final"]["text"], "検査を続けています。" if recovered else "前の発言")

    def test_lead_state_searches_speech_questions_and_text_tool_results_with_times(self) -> None:
        self.state.write_text(json.dumps({"version": 1, "left": {}, "questions": {}, "handoffs": {},
                                          "lead": {"worktree": "/w/lead"}}))
        question = {"questions": [{"header": "release", "question": "MUSE を出しますか？"}]}
        write_jsonl(self.projects / "-w-lead" / "lead.jsonl", [
            {**user("muse: earlier permission"), "timestamp": "2026-11-14T00:58:00Z"},
            {**assistant(text("muse: before")), "timestamp": "2026-11-14T00:59:00Z"},
            {**user("MUSE は出してよい"), "timestamp": "2026-11-14T01:00:00Z"},
            {**user("<task-notification>MUSE: done</task-notification>"), "timestamp": "2026-11-14T01:01:00Z"},
            {**assistant(text("muse: ready")), "timestamp": "2026-11-14T01:02:00Z"},
            {**assistant({"type": "tool_use", "id": "ask", "name": "AskUserQuestion", "input": question}),
             "timestamp": "2026-11-14T01:03:00Z"},
            {**user([{"type": "tool_result", "tool_use_id": "tool", "content": [
                {"type": "text", "text": "muse: CI passed"}, {"type": "image", "data": "not-text"}]}]),
             "timestamp": "2026-11-14T01:04:00Z"},
            {**assistant(text("MUSE error")), "timestamp": "2026-11-14T01:05:00Z", "isApiErrorMessage": True}])
        handoff.SavedHandoff(text="handoff", kind="info", decisions=[], sent_at="2026-11-14T01:00:00Z",
                             next_offset=0).save(self.root / "handoffs", datetime(2026, 11, 14, 1, tzinfo=timezone.utc))
        replies = {("worktree", "ps"): {"worktrees": [worktree("/w/lead")]},
                   ("terminal", "list"): {"terminals": []}}
        with patch("subprocess.run", side_effect=lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, json.dumps({"result": replies[tuple(args[1:-1])]}))):
            code, shown, err = self.call("lead-state", "--grep", "MuSe", "--since", "2026-11-14T10:00:00+09:00")

        self.assertEqual(code, 0, err)
        result = json.loads(shown)
        self.assertEqual(result["user_messages"], [{"at": "2026-11-14T01:00:00Z", "text": "MUSE は出してよい"}])
        self.assertEqual([(item["at"], item["kind"]) for item in result["matches"]], [
            ("2026-11-14T01:00:00Z", "user"), ("2026-11-14T01:02:00Z", "assistant"),
            ("2026-11-14T01:03:00Z", "AskUserQuestion"), ("2026-11-14T01:04:00Z", "tool_result")])
        self.assertIn("release", result["matches"][2]["text"])
        self.assertEqual(result["matches"][3]["text"], "muse: CI passed")

    def test_lead_state_marks_elsewhere_answers_and_only_offers_to_close_the_whole_answered_selector(self) -> None:
        questions = [{"header": "release's approval", "question": "muse を出しますか？"},
                     {"header": "merge", "question": "muse を merge しますか？"}]
        self.state.write_text(json.dumps({"version": 1, "left": {}, "handoffs": {},
            "lead": {"worktree": "/w/lead"}, "questions": {
                "release": {"question": questions[0]["question"] + "選択肢: 出す／待つ", "answer": "出す",
                            "answered_at": "2026-11-14T01:02:00Z"}}}))
        write_jsonl(self.inbox, [{"kind": "ask", "id": "ask-one", "ts": "2026-11-14T00:00:00Z",
                                  "questions": questions}])
        write_jsonl(self.projects / "-w-lead" / "lead.jsonl", [
            {**assistant({"type": "tool_use", "id": "pending", "name": "AskUserQuestion",
                          "input": {"questions": questions}}), "timestamp": "2026-11-14T01:01:00Z"}])
        replies = {("worktree", "ps"): {"worktrees": [worktree("/w/lead")]},
                   ("terminal", "list"): {"terminals": [terminal("/w/lead", "claude", "term_lead")]},
                   ("terminal", "read", "--terminal", "term_lead", "--screen"):
                       {"terminal": {"tail": ["release's approval | merge", "Enter to select"]}}}
        with patch("subprocess.run", side_effect=lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, json.dumps({"result": replies[tuple(args[1:-1])]}))):
            _, shown, _ = self.call("lead-state")
            partial = json.loads(shown)["pending_questions"][0]
            self.assertEqual(partial["questions"][0]["answered_elsewhere"], "2026-11-14T01:02:00Z")
            self.assertNotIn("answered_elsewhere", partial["questions"][1])
            self.assertNotIn("close_selector", partial)
            write_jsonl(self.root / "answers.jsonl", [
                {"id": "ask-one:2", "ts": "2026-11-14T01:02:30Z", "how": "inbox_read.py answered"},
                {"id": "ask-one:2", "ts": "2026-11-14T01:03:00Z", "how": "send"}])
            code, shown, err = self.call("lead-state")

        self.assertEqual(code, 0, err)
        call = json.loads(shown)["pending_questions"][0]
        self.assertEqual(call["answered_elsewhere"], "2026-11-14T01:03:00+00:00")
        self.assertEqual(call["questions"][1]["answer_source"], "ask-answers ask-one:2")
        self.assertEqual([shlex.split(command) for command in call["close_selector"]], [[
            str(Path(handoff.NOTIFY).with_name("close-selector").expanduser()), "term_lead", "release's approval", "merge"]])

    def test_lead_state_does_not_close_from_old_answers_withdrawals_or_ambiguous_asks(self) -> None:
        question = {"header": "merge", "question": "Merge?"}
        self.state.write_text(json.dumps({"version": 1, "left": {}, "handoffs": {},
            "lead": {"worktree": "/w/lead"}, "questions": {
                "old": {"question": "Merge?", "answer": "Yes", "answered_at": "2026-11-14T00:00:00Z"}}}))
        write_jsonl(self.projects / "-w-lead" / "lead.jsonl", [
            {**assistant({"type": "tool_use", "id": "pending", "name": "AskUserQuestion",
                          "input": {"questions": [question]}}), "timestamp": "2026-11-14T01:00:00Z"}])
        replies = {("worktree", "ps"): {"worktrees": [worktree("/w/lead")]},
                   ("terminal", "list"): {"terminals": []}}
        with patch("subprocess.run", side_effect=lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, json.dumps({"result": replies[tuple(args[1:-1])]}))):
            for how, ids in [("send", ["a", "b"]), ("triage-notify --withdraws", ["a"]),
                             ("inbox_read.py answered", ["a"])]:
                with self.subTest(how=how, ids=ids):
                    write_jsonl(self.inbox, [{"kind": "ask", "id": key, "questions": [question]} for key in ids])
                    write_jsonl(self.root / "answers.jsonl", [{"id": "a", "ts": "2026-11-14T02:00:00Z", "how": how}])
                    code, shown, err = self.call("lead-state")
                    self.assertEqual(code, 0, err)
                    self.assertNotIn("answered_elsewhere", json.loads(shown)["pending_questions"][0])

    def test_lead_state_leaves_identical_hourly_questions_from_different_decisions_open(self) -> None:
        question = {"header": "merge", "question": "Merge?"}
        self.state.write_text(json.dumps({"version": 1, "left": {}, "handoffs": {},
            "lead": {"worktree": "/w/lead"}, "questions": {
                "first": {"row": "/w/first", "decision": "merge:first", "question": "Merge?", "answer": None},
                "other": {"row": "/w/other", "decision": "merge:other", "question": "Merge?", "answer": "Yes",
                          "answered_at": "2026-11-14T02:00:00Z"}}}))
        write_jsonl(self.projects / "-w-lead" / "lead.jsonl", [
            {**assistant({"type": "tool_use", "id": "pending", "name": "AskUserQuestion",
                          "input": {"questions": [question]}}), "timestamp": "2026-11-14T01:00:00Z"}])
        replies = {("worktree", "ps"): {"worktrees": [worktree("/w/lead")]},
                   ("terminal", "list"): {"terminals": []}}
        with patch("subprocess.run", side_effect=lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, json.dumps({"result": replies[tuple(args[1:-1])]}))):
            code, shown, err = self.call("lead-state")
            self.assertEqual(code, 0, err)
            self.assertNotIn("answered_elsewhere", json.loads(shown)["pending_questions"][0])
            state = json.loads(self.state.read_text())
            state["questions"]["first"].update(header="merge", answer="No", answered_at="2026-11-14T02:01:00Z")
            state["questions"]["other"]["header"] = "another merge"
            self.state.write_text(json.dumps(state))
            code, shown, err = self.call("lead-state")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(shown)["pending_questions"][0]["questions"][0]["answer_source"],
                             "hourly_state first")

    def test_send_defaults_to_the_appointed_lead_and_includes_undelivered_input(self) -> None:
        self.state.write_text(json.dumps({"version": 1, "left": {}, "questions": {}, "handoffs": {},
                                          "lead": {"worktree": "/w/lead"}}))
        write_jsonl(self.projects / "-w-lead" / "s.jsonl", [
            user("go"), assistant(text("last reply")),
            {"type": "queue-operation", "operation": "enqueue", "content": "pending instruction",
             "timestamp": "2026-10-01T01:00:00Z"}])
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            code, _, err = self.send()

        self.assertEqual(code, 0, err)
        body = run.call_args.args[0][-1]
        self.assertIn("指揮の今（lead）", body)
        self.assertIn("last reply", body)
        self.assertIn("pending instruction", body)
        self.assertRegex(body, r"2026-10-01T01:00:00Z、\d+ 分経過")


class HourlyStateTests(unittest.TestCase):
    def test_hourly_reads_publish_complete_json_files_without_stdout(self) -> None:
        saved = self.root / "scan.json"
        saved.write_text("[]")
        (self.root / "inbox.jsonl").write_text("")
        orca_replies: dict[tuple[str, ...], dict] = {
                       ("worktree", "ps"): {"worktrees": [{"path": "/w/lead", "agents": []}]},
                       ("terminal", "list"): {"terminals": []}}
        def orca(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "ps":
                return subprocess.CompletedProcess(args, 0, "")
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": orca_replies[tuple(args[1:-1])]}))
        commands = [(hourly_state.main, ["--state-file", str(self.state), "stale-asks", "--from", str(saved)]),
                    (inbox_read.main, ["open-asks"]),
                    (handoff.main, ["lead-state", "--lead-worktree", "/w/lead", "--orca", "fake-orca",
                                    "--claude-projects", str(self.root / "projects"),
                                    "--handoffs-dir", str(self.root / "handoffs")])]
        for main, args in commands:
            destination = self.root / "output" / "read.json"
            destination.parent.mkdir(exist_ok=True)
            destination.write_text('{"previous": true}')
            published = []
            replace = os.replace
            def publish(source: str, target: str) -> None:
                self.assertEqual(json.loads(destination.read_text()), {"previous": True})
                published.append(json.loads(Path(source).read_text()))
                replace(source, target)
            with self.subTest(command=args), patch("subprocess.run", side_effect=orca), \
                    patch("os.replace", side_effect=publish), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([*args, "--out", str(destination)]), 0)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(published, [json.loads(destination.read_text())])
            self.assertEqual(list(destination.parent.iterdir()), [destination])
            destination.write_text('{"previous": true}')
            with self.subTest(write_failure=args), patch("subprocess.run", side_effect=orca), \
                    patch("os.replace", side_effect=OSError("fixture disk error")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main([*args, "--out", str(destination)]), 1)
            self.assertEqual(json.loads(destination.read_text()), {"previous": True})
            self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_answers_to_withdrawn_questions_name_replacement_and_do_not_discard_other_answers(self) -> None:
        message = self.source("question.txt", "公開しますか？")
        reason = self.source("reason.txt", "選択肢が変わった")
        answer = self.source("answer.txt", "公開する")
        def ask(decision: str) -> dict:
            return self.call("asked", "--row", "/w/lane", "--decision", decision,
                             "--final-message-file", message, "--question-file", message)[1]
        old, new, other = ask("old"), ask("new"), ask("other")
        self.call("withdraw", "--id", old["id"], "--reason-file", reason, "--replaced-by", new["id"])
        code, shown, err = self.run_main("answered", "--id", old["id"], "--answer-file", answer)
        self.assertEqual(code, 2)
        self.assertIn(new["id"], err)
        self.assertEqual(json.loads(shown)["replaced_by"], new["id"])
        lines = self.source("answers.jsonl", "".join(json.dumps({"id": item["id"], "answer": "公開する"}) + "\n"
                                                    for item in (old, other)))
        code, shown, err = self.run_main("answered-many", "--file", lines)
        self.assertEqual(code, 2)
        self.assertIn(new["id"], err)
        self.assertEqual(json.loads(shown)[1]["answer"], "公開する")
        questions = self.call("show")[1]["questions"]
        self.assertIsNone(questions[old["id"]]["answer"])
        self.assertIsNone(questions[new["id"]]["answer"])
        self.assertEqual(questions[other["id"]]["answer"], "公開する")

    def test_asked_and_asked_many_create_a_fresh_question_after_withdrawal(self) -> None:
        message = self.source("question.txt", "公開しますか？")
        reason = self.source("reason.txt", "問いを出し直す")
        args = ["--row", "/w/lane", "--decision", "publish", "--final-message-file", message,
                "--question-file", message]
        _, first = self.call("asked", *args)
        self.call("withdraw", "--id", first["id"], "--reason-file", reason)
        _, second = self.call("asked", *args)
        self.assertNotEqual(second["id"], first["id"])
        self.assertNotIn("withdrawn_at", second)
        self.assertEqual(self.call("asked", *args)[1]["id"], second["id"])
        self.call("withdraw", "--id", second["id"], "--reason-file", reason)
        lines = self.source("asked.jsonl", json.dumps({"row": "/w/lane", "decision": "publish",
                            "final_message": "公開しますか？", "question": "公開しますか？"}) + "\n")
        code, (third,) = self.call("asked-many", "--file", lines)
        self.assertEqual(code, 0)
        self.assertNotIn(third["id"], (first["id"], second["id"]))
        self.assertEqual(len(self.call("show")[1]["questions"]), 3)

    def test_stale_asks_runs_a_user_wait_command_again_and_omits_completed_waits(self) -> None:
        message = self.source("wait.txt", "登録を済ませる")
        ready = self.root / "ready file"
        saved = self.root / "scan.json"
        saved.write_text("[]")
        (self.root / "inbox.jsonl").write_text("")
        command = f"test -f {shlex.quote(str(ready))}"
        with patch.dict(os.environ, {"ORCA_WORKTREE_ID": "repo::" + str(self.root)}):
            code, wait = self.call("user-wait", "--text-file", message, "--check-command", command)
        self.assertEqual(code, 0)
        code, stale = self.call("stale-asks", "--from", str(saved))
        self.assertEqual((code, [item["id"] for item in stale]), (0, [wait["id"]]))
        self.assertEqual(stale[0]["checks"][0]["exit_code"], 1)
        ready.touch()
        self.assertEqual(self.call("stale-asks", "--from", str(saved)), (0, []))

    def test_stale_asks_uses_later_lane_reports_as_progress_after_an_answer(self) -> None:
        saved = self.root / "scan.json"
        saved.write_text(json.dumps([{"path": "/w/lane", "repo": "app", "class": "waiting", "claude": {
            "last_user_at": "2026-11-11T01:00:00Z", "final_at": "2026-11-11T01:05:00Z", "final_text": "待ちます"}}]))
        ask = {"id": "ab12cd34-0000-ask", "ts": "2026-11-11T01:02:00Z",
               "kind": "ask", "worktree": "lane", "repo": "app", "text": "公開しますか？"}
        write_jsonl(self.root / "answers.jsonl", [{"id": ask["id"], "ts": "2026-11-11T01:30:00Z",
                                                   "how": "send", "note": "公開する"}])
        for kind, repo, at, expected in (("done", "app", "01:40", []), ("info", "app", "01:40", []),
                                        ("info", "other", "01:40", ["stopped_after_answer"]),
                                        ("info", "app", "01:20", ["stopped_after_answer"])):
            with self.subTest(kind=kind, repo=repo, at=at):
                write_jsonl(self.root / "inbox.jsonl", [ask, {"id": "progress", "kind": kind,
                    "repo": repo, "worktree": "lane", "ts": f"2026-11-11T{at}:00Z", "text": "適用済み"}])
                code, stale = self.call("stale-asks", "--from", str(saved))
                self.assertEqual((code, [item["state"] for item in stale]), (0, expected))

    def test_stale_asks_does_not_reopen_a_shared_question_already_covered_by_answers(self) -> None:
        saved = self.root / "scan.json"
        (self.root / "inbox.jsonl").write_text("")
        message = self.source("q.txt", "公開しますか？")
        self.call("asked", "--row", "/w/reasked", "--decision", "again", "--asked-at", "2026-11-08T01:00:00Z",
                  "--final-message-file", message, "--question-file", message, "--covers", "ask:reasked::publish")
        saved.write_text(json.dumps([{"path": "/w/reasked", "repo": "app", "class": "waiting", "claude": {
            "final_at": "2026-11-08T01:00:00Z", "final_text": "公開しますか？"}}]))
        write_jsonl(self.root / "decisions.jsonl", [{"id": "a", "decision": "ask:reasked::publish",
            "answer": "公開する", "decided_at": "2026-11-09T01:00:00Z"}])
        code, result = self.call("stale-asks", "--from", str(saved))
        self.assertEqual(code, 0)
        self.assertEqual(result[0]["state"], "settled")

    def test_stale_asks_keeps_reasked_and_in_progress_questions_and_uses_prompt_time(self) -> None:
        saved = self.root / "scan.json"
        (self.root / "inbox.jsonl").write_text("")
        message = self.source("q.txt", "公開しますか？")
        self.call("asked", "--row", "/w/reasked", "--decision", "again", "--asked-at", "2026-11-09T01:00:00Z",
                  "--final-message-file", message, "--question-file", message)
        rows: list[dict] = [{"path": f"/w/{name}", "repo": "app", "class": "waiting", "claude": {
                    "final_at": "2026-11-02T01:00:00Z", "final_text": "公開しますか？"}}
                for name in ("reasked", "progress", "prompt")]
        rows[1]["verdict"] = {"class": "in_progress"}
        rows[1]["claude"]["pending_questions"] = [{"questions": [{"question": "公開しますか？"}]}]
        rows[2]["prompt_waiting"] = {"tab": "t", "question": "公開しますか？", "choices": ["1. はい"],
                                      "asked_at": "2026-11-09T02:00:00Z"}
        saved.write_text(json.dumps(rows))
        write_jsonl(self.root / "decisions.jsonl", [{"id": name, "decision": f"ask:{name}::old",
                    "answer": "旧問への回答", "decided_at": "2026-11-03T01:00:00Z"}
                    for name in ("reasked", "progress")])
        code, result = self.call("stale-asks", "--from", str(saved))
        self.assertEqual(code, 0)
        by_name = {item["name"]: item for item in result}
        self.assertEqual(by_name["reasked"]["state"], "unanswered")
        self.assertEqual(by_name["progress"]["state"], "unanswered")
        self.assertEqual(by_name["prompt"]["since"], "2026-11-09T02:00:00Z")

    def test_in_progress_without_a_question_is_not_an_unanswered_user_question(self) -> None:
        saved = self.root / "scan.json"
        (self.root / "inbox.jsonl").write_text("")
        saved.write_text(json.dumps([{"path": "/w/continuing", "repo": "app", "class": "stalled",
            "verdict": {"class": "in_progress", "next_step": "Continue implementation"},
            "claude": {"final_at": "2026-11-09T01:00:00Z", "final_text": "I will continue implementation."}}]))
        code, result = self.call("stale-asks", "--from", str(saved))
        self.assertEqual(code, 0)
        self.assertEqual(result, [])

    def test_stale_asks_reports_pr_state_head_and_update_for_an_unasked_row(self) -> None:
        saved = self.root / "scan.json"
        (self.root / "inbox.jsonl").write_text("")
        saved.write_text(json.dumps([{"path": "/w/review", "repo": "app", "class": "waiting",
                                      "git": {"github": "acme/app"}, "pr": {"number": 10},
                                      "claude": {"final_at": "2026-11-09T01:00:00Z", "final_text": "Merge #10?"}}]))
        nodes = {"pr_10": {"state": "OPEN", "updatedAt": "2026-11-09T02:00:00Z", "headRefOid": "new-head"}}
        with patch.object(scan, "graphql_repository", return_value=nodes) as api:
            code, result = self.call("stale-asks", "--from", str(saved))
        self.assertEqual(code, 0)
        self.assertEqual(result[0]["pr"], {"state": "OPEN", "head": "new-head", "updatedAt": "2026-11-09T02:00:00Z"})
        self.assertEqual(len(api.call_args_list), 1)

    def test_pending_batches_question_pr_facts_and_reports_current_heads(self) -> None:
        lane = self.root / "lane"
        subprocess.run(["git", "init", "-q", str(lane)], check=True)
        subprocess.run(["git", "-C", str(lane), "remote", "add", "origin", "git@github.com:acme/app.git"], check=True)
        message = self.source("q.txt", "#10 と #11 を merge しますか？")
        with patch.object(scan, "graphql_repository", return_value={}):
            _, first = self.call("asked", "--row", str(lane), "--decision", "merge",
                                 "--final-message-file", message, "--question-file", message)
        message2 = self.source("q2.txt", "#12 を merge しますか？")
        with patch.object(scan, "graphql_repository", return_value={}):
            self.call("asked", "--row", str(lane), "--decision", "merge2",
                      "--final-message-file", message2, "--question-file", message2)
        nodes = {f"item_{number}": {"state": "OPEN", "updatedAt": "2026-11-09T01:00:00Z", "headRefOid": f"head{number}"}
                 for number in (10, 11, 12)}
        with patch.object(scan, "graphql_repository", return_value=nodes) as api:
            code, result = self.call("pending")
        self.assertEqual(code, 0)
        self.assertEqual(len(api.call_args_list), 1)
        item = next(item for item in result["questions"] if item["id"] == first["id"])
        self.assertEqual([(pr["number"], pr["state"], pr["head"]) for pr in item["prs"]],
                         [(10, "OPEN", "head10"), (11, "OPEN", "head11")])

    def test_stale_asks_includes_a_lost_one_shot_schedule(self) -> None:
        saved = self.root / "scan.json"
        (self.root / "inbox.jsonl").write_text("")
        saved.write_text(json.dumps([{"path": "/w/scheduled", "class": "stalled", "repo": "app",
                                      "claude": {"final_at": "2026-11-09T01:00:00Z", "final_text": "Scheduled."},
                                      "lost_schedules": [{"id": "abc", "cron": "30 18 9 11 *", "prompt": "Release status"}]}]))
        code, result = self.call("stale-asks", "--from", str(saved))
        self.assertEqual(code, 0)
        self.assertEqual(result[0]["state"], "lost_schedule")
        self.assertIn("Release status", result[0]["question"])

    def test_stale_asks_marks_an_ask_whose_related_issues_are_all_closed(self) -> None:
        saved = self.root / "scan.json"
        saved.write_text(json.dumps([{"path": f"/w/{name}", "repo": "app", "class": "waiting", "claude": {
            "final_at": "2026-11-09T01:00:00Z", "final_text": "どうしますか？"}} for name in ("closed", "open", "backlog")]))
        issues = {"closed": ["acme/app#5", "https://github.com/acme/app/pull/6"], "open": ["acme/app#7"],
                  "backlog": ["HOSYU_TEST-1"]}
        write_jsonl(self.root / "inbox.jsonl", [
            {"id": f"{name}-0000-ask", "kind": "ask", "worktree": name, "repo": "app", "ts": "2026-11-09T01:00:00Z",
             "text": "どうしますか？", "questions": [{"question": f"{name} を進めますか？", "issue": issue} for issue in refs]}
            for name, refs in issues.items()])
        nodes = {"item_5": {"state": "CLOSED", "updatedAt": "2026-11-09T02:00:00Z"},
                 "item_6": {"state": "MERGED", "updatedAt": "2026-11-09T03:00:00Z", "headRefOid": "h6"},
                 "item_7": {"state": "OPEN", "updatedAt": "2026-11-09T02:00:00Z"}}
        with patch.object(scan, "graphql_repository", return_value=nodes) as api:
            code, result = self.call("stale-asks", "--from", str(saved))
            with redirect_stdout(io.StringIO()) as out:
                hourly_state.main(["--state-file", str(self.state), "stale-asks", "--from", str(saved), "--summary"])
        self.assertEqual(code, 0)
        self.assertEqual({item["name"]: item["related_issues_closed"] for item in result},
                         {"closed": True, "open": False, "backlog": False})
        closed = next(item for item in result if item["name"] == "closed")
        self.assertEqual([(ref["ref"], ref["state"]) for ref in closed["related_issues"]],
                         [("acme/app#5", "CLOSED"), ("https://github.com/acme/app/pull/6", "MERGED")])
        self.assertEqual(len(api.call_args_list), 2)  # one read of acme/app per run
        lines = {line.split(" | ")[0]: line for line in out.getvalue().splitlines()}
        self.assertIn("関わる issue が閉じた", lines["closed"])
        self.assertNotIn("関わる issue が閉じた", lines["open"] + lines["backlog"])

    def test_answered_by_decision_records_the_one_unanswered_question_asked_under_it(self) -> None:
        message = self.source("message.txt", "Merge?")
        answer = self.source("answer.txt", "merge する")
        asked = {row: self.call("asked", "--row", row, "--decision", decision, "--final-message-file", message,
                                "--question-file", message)[1]["id"]
                 for row, decision in (("/w/a", "merge:PR#10"), ("/w/b", "merge:PR#10"), ("/w/c", "deploy"))}

        code, answered = self.call("answered", "--decision", "deploy", "--answer-file", answer)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            ambiguous = hourly_state.main(["--state-file", str(self.state), "answered", "--decision", "merge:PR#10",
                                           "--answer-file", answer])

        self.assertEqual((code, answered["id"], answered["answer"]), (0, asked["/w/c"], "merge する"))
        self.assertEqual(ambiguous, 1)
        self.assertIn(asked["/w/a"], err.getvalue())
        self.assertIn(asked["/w/b"], err.getvalue())

    def test_asked_many_takes_the_row_and_final_message_from_each_ask_and_checks_every_line_first(self) -> None:
        scan_json = self.root / "scan.json"
        # The same path on two Macs: the ask's tab names the row.
        scan_json.write_text(json.dumps([
            {"path": "/w/lane", "repo": "app", "class": "waiting", "terminals": [{"handle": "term_here"}]},
            {"path": "/w/lane", "environment": "mbp16", "repo": "app", "class": "waiting",
             "terminals": [{"handle": "term_there"}]}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": "abcd1234-0000-ask", "kind": "ask", "worktree": "lane",
                                                 "repo": "app", "handle": "term_there", "ts": "2026-11-09T01:00:00Z",
                                                 "text": "A と B のどちらで出しますか？"}])
        lines = self.root / "asked.jsonl"
        from_ask = {"ask": "abcd1234", "decision": "release", "question": "A と B のどちらで出しますか？"}
        by_row = {"row": "/w/other", "decision": "merge:PR#3", "question": "merge しますか？"}
        lines.write_text(json.dumps(from_ask) + "\n" + json.dumps(by_row) + "\n")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            refused = hourly_state.main(["--state-file", str(self.state), "asked-many", "--file", str(lines),
                                         "--from", str(scan_json)])
        self.assertEqual(refused, 1)
        self.assertIn("line 2", err.getvalue())
        self.assertEqual(self.call("show")[1]["questions"], {})

        lines.write_text(json.dumps(from_ask) + "\n" + json.dumps({**by_row, "final_message": "merge しますか？"}) + "\n")
        code, recorded = self.call("asked-many", "--file", str(lines), "--from", str(scan_json))

        self.assertEqual(code, 0)
        self.assertEqual([(item["row"], item["decision"], item["final_message"]) for item in recorded],
                         [("mbp16::/w/lane", "release", "A と B のどちらで出しますか？"),
                          ("/w/other", "merge:PR#3", "merge しますか？")])

    def test_asked_many_binds_numbered_ask_parts_and_rejects_an_out_of_range_part(self) -> None:
        ask_id = "abcd1234-0000-ask"
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "repo": "app", "class": "waiting"}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": ask_id, "kind": "ask", "worktree": "lane", "repo": "app",
            "ts": "2026-11-13T09:00:00Z", "text": "Two questions", "questions": [
                {"header": "First", "question": "First?"}, {"header": "Second", "question": "Second?"}]}])
        lines = self.root / "asked.jsonl"
        items = [{"ask": f"{ask_id[:8]}:1", "decision": "first", "question": "First?"},
                 {"ask": f"{ask_id}:2", "decision": f"answer:lane::{ask_id}", "question": "Second?"}]
        write_jsonl(lines, [{**item, "asked_at": "2026-11-13T09:01:00Z"} for item in items])
        code, output, error = self.run_main("asked-many", "--file", str(lines), "--from", str(scan_json))
        self.assertEqual(code, 0, error)
        recorded = json.loads(output)
        self.assertEqual([item["ask_id"] for item in recorded], [f"{ask_id}:1", f"{ask_id}:2"])

        sync = importlib.import_module("answer_sync")
        with patch.dict(os.environ, {"TRIAGE_INBOX": str(self.root / "inbox.jsonl"),
                                      "TRIAGE_ASK_ANSWERS": str(self.root / "answers.jsonl")}):
            sync.record([f"{ask_id}:2"], "Second answer", "send", at="2026-11-13T09:02:00Z",
                        state_file=self.state, decisions_file=self.root / "lead.jsonl",
                        handed_off_file=self.root / "handed.jsonl")
        questions = self.call("show")[1]["questions"]
        self.assertIsNone(questions[recorded[0]["id"]]["answer"])
        self.assertEqual(questions[recorded[1]["id"]]["answer"], "Second answer")

        write_jsonl(lines, [{"ask": f"{ask_id}:1", "decision": "new-first", "question": "First?"},
                            {"ask": f"{ask_id}:3", "decision": "invalid", "question": "Third?"}])
        code, _, error = self.run_main("asked-many", "--file", str(lines), "--from", str(scan_json))
        self.assertEqual(code, 1)
        self.assertIn(":3", error)
        self.assertEqual(self.call("show")[1]["questions"], questions)

    def test_answered_many_records_by_id_or_decision_only_when_every_line_holds(self) -> None:
        message = self.source("message.txt", "Merge?")
        first, second = (self.call("asked", "--row", row, "--decision", decision, "--final-message-file", message,
                                   "--question-file", message)[1]["id"]
                         for row, decision in (("/w/a", "merge:PR#10"), ("/w/b", "merge:PR#11")))
        lines = self.root / "answered.jsonl"
        lines.write_text(json.dumps({"id": first, "answer": "merge する"}) + "\n"
                         + json.dumps({"decision": "merge:PR#99", "answer": "待つ"}) + "\n")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            refused = hourly_state.main(["--state-file", str(self.state), "answered-many", "--file", str(lines)])
        self.assertEqual(refused, 1)
        self.assertIn("line 2", err.getvalue())
        self.assertEqual([item["answer"] for item in self.call("show")[1]["questions"].values()], [None, None])

        lines.write_text(json.dumps({"id": first, "answer": "merge する"}) + "\n"
                         + json.dumps({"decision": "merge:PR#11", "answer": "待つ"}) + "\n")
        code, recorded = self.call("answered-many", "--file", str(lines))

        self.assertEqual(code, 0)
        self.assertEqual([(item["id"], item["answer"]) for item in recorded], [(first, "merge する"), (second, "待つ")])

    def test_answer_text_takes_its_recorded_time_and_a_time_ten_minutes_off_writes_nothing(self) -> None:
        # 2026-11-10: an answer that arrived at 13:03 was recorded with a hand-written 「12:1x」.
        message = self.source("message.txt", "Merge?")
        first, second, third = (self.call("asked", "--row", row, "--decision", "merge", "--final-message-file",
                                          message, "--question-file", message)[1]["id"] for row in ("/w/a", "/w/b", "/w/c"))
        at = "2026-11-10T13:03:19+09:00"
        stamped = {"id": first, "answered_at": at, "answer": "答え（11/10 {answered_at}、12:00 の定時のタブ）: merge する"}
        lines = self.root / "answered.jsonl"
        lines.write_text(json.dumps(stamped) + "\n"
                         + json.dumps({"id": second, "answered_at": at, "answer": "答え（11/10 12:1x）: 待つ"}) + "\n")

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            refused = hourly_state.main(["--state-file", str(self.state), "answered-many", "--file", str(lines)])
        self.assertEqual(refused, 1)
        self.assertIn("line 2: 12:1x is 44 minutes from answered_at 13:03 JST", err.getvalue())
        self.assertNotIn("line 1", err.getvalue())
        self.assertEqual([item["answer"] for item in self.call("show")[1]["questions"].values()], [None] * 3)

        lines.write_text(json.dumps(stamped) + "\n"
                         + json.dumps({"id": second, "answered_at": at, "answer": "答え（11/10 13:0x）: 待つ"}) + "\n")
        code, recorded = self.call("answered-many", "--file", str(lines))
        answer = self.source("answer.txt", "答え（{answered_at}）: merge する")
        _, now = self.call("answered", "--id", third, "--answer-file", answer)
        again = self.call("answered", "--id", third, "--answer-file", answer, "--answered-at", "2026-11-10T00:00:00Z")

        self.assertEqual(code, 0)
        self.assertEqual([item["answer"] for item in recorded],
                         ["答え（11/10 13:03、12:00 の定時のタブ）: merge する", "答え（11/10 13:0x）: 待つ"])
        recorded_at = datetime.fromisoformat(now["answered_at"]).astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(now["answer"], f"答え（{recorded_at:%H:%M}）: merge する")
        self.assertEqual(again, (0, now))

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "hourly.json"
        # A host's real verdict store must never reach a test run.
        hermetic = patch.object(hourly_state.scan_sessions, "VERDICTS_FILE", str(self.root / "no-verdicts.json"))
        hermetic.start()
        self.addCleanup(hermetic.stop)
        handoffs = patch.object(handoff, "HANDOFFS", self.root / "handoffs")
        handoffs.start()
        self.addCleanup(handoffs.stop)
        # Other script tests load another copy of lead_decisions into sys.modules.
        for module in dict.fromkeys((hourly_state.lead_decisions, hourly_state.answer_evidence.lead_decisions)):
            for name, value in (("DEFAULT_LOG", self.root / "decisions.jsonl"),
                                ("HANDED_OFF_LOG", self.root / "handed-off.jsonl")):
                isolated = patch.object(module, name, value)
                isolated.start()
                self.addCleanup(isolated.stop)
        environment = patch.dict(os.environ, {"TRIAGE_INBOX": str(self.root / "inbox.jsonl"),
                                              "TRIAGE_ASK_ANSWERS": str(self.root / "answers.jsonl"),
                                              "TRIAGE_ASK_CLAIMS": str(self.root / "claims.jsonl"),
                                              "ORCA_CLI_COMMAND": "missing-orca-for-offline-tests",
                                              "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                                              "ORCA_WORKTREE_ID": "repo::/w/triage",
                                              "ORCA_WORKTREE_PATH": "/w/triage",
                                              "ORCA_TERMINAL_HANDLE": "term_triage",
                                              # Not the Orca tab that runs the tests.
                                              "ORCA_PANE_KEY": "",
                                              "CLAUDE_CODE_SESSION_ID": "triage-run"})
        environment.start()
        self.addCleanup(environment.stop)

    def source(self, name: str, content: str) -> str:
        path = self.root / name
        path.write_text(content)
        return str(path)

    def call(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = hourly_state.main(["--state-file", str(self.state), *args])
        return code, json.loads(out.getvalue())

    def comment_orca(self, hourly_workspace: str) -> Path:
        """An Orca CLI whose hourly triage automation runs in hourly_workspace; it logs every other call it gets."""
        script, log = self.root / "orca", self.root / "orca-calls.jsonl"
        script.write_text(
            "#!/usr/bin/env python3\nimport json, sys\nargs = sys.argv[1:]\n"
            "if args[:2] == ['automations', 'list']:\n"
            f"    print(json.dumps({{'result': {{'automations': [{triage_automation('a1', hourly_workspace)!r}]}}}}))\n"
            "else:\n"
            f"    open({str(log)!r}, 'a').write(json.dumps(args) + '\\n')\n"
            "    print(json.dumps({'result': {}}))\n")
        script.chmod(0o755)
        return log

    def test_hourly_workspace_comment_shows_its_open_questions_and_the_last_run(self) -> None:
        log = self.comment_orca("/w/triage")
        started = datetime(2026, 11, 12, 14, 0, 30)
        (self.root / "claude" / "sessions").mkdir(parents=True)
        (self.root / "claude" / "sessions" / "77.json").write_text(json.dumps(
            {"name": "triage-run", "startedAt": int(started.timestamp() * 1000)}))
        message = self.source("message.txt", "Ready.")
        merge = self.source("merge.txt", "PR を merge しますか？\n根拠: レビュー済み")
        publish = self.source("publish.txt", "公開は明日でよいですか？")
        answer = self.source("answer.txt", "merge する")
        reason = self.source("reason.txt", "担当が取り下げた")
        with patch.dict(os.environ, {"ORCA_CLI_COMMAND": str(self.root / "orca"), "ORCA_PANE_KEY": "tab:leaf",
                                     "CLAUDE_PID": "77"}):
            _, first = self.call("asked", "--row", "/w/a", "--decision", "merge", "--final-message-file", message,
                                 "--question-file", merge)
            _, second = self.call("asked", "--row", "/w/b", "--decision", "publish", "--final-message-file", message,
                                  "--question-file", publish)
            self.assertEqual(self.call("comment", "--result", "2 問を伺った")[0], 0)
            # The lead records the answer from its own workspace.
            with patch.dict(os.environ, {"ORCA_WORKTREE_ID": "repo::/w/lead", "ORCA_WORKTREE_PATH": "/w/lead"}):
                self.call("answered", "--id", first["id"], "--answer-file", answer)
                self.call("withdraw", "--id", second["id"], "--reason-file", reason)

        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual({tuple(call[:4]) for call in calls}, {("worktree", "set", "--worktree", "path:/w/triage")})
        run = "14:00 の定時: 2 問を伺った"
        self.assertEqual([call[call.index("--comment") + 1] for call in calls], [
            "要判断: PR を merge しますか？", "要判断: PR を merge しますか？／公開は明日でよいですか？",
            f"要判断: PR を merge しますか？／公開は明日でよいですか？\n{run}", f"要判断: 公開は明日でよいですか？\n{run}", run])

    def test_asked_leaves_the_comment_of_a_workspace_without_the_hourly_run_unless_told(self) -> None:
        log = self.comment_orca("/w/hourly")
        message = self.source("message.txt", "Ready.")
        question = self.source("question.txt", "進めますか？")
        with patch.dict(os.environ, {"ORCA_CLI_COMMAND": str(self.root / "orca"), "ORCA_PANE_KEY": "tab:leaf"}):
            self.call("asked", "--row", "/w/a", "--decision", "one", "--final-message-file", message,
                      "--question-file", question)
            self.assertFalse(log.exists())
            self.call("asked", "--row", "/w/a", "--decision", "two", "--final-message-file", message,
                      "--question-file", question, "--comment")
        self.assertEqual([json.loads(line)[-2] for line in log.read_text().splitlines()],
                         ["要判断: 進めますか？／進めますか？"])

    def test_pending_lists_only_open_items_each_with_its_id(self) -> None:
        message = self.source("message.txt", "Should B start?\n")
        question = self.source("question.txt", "Send B the task?\n")
        answer = self.source("answer.txt", "Yes.\n")
        _, open_question = self.call("asked", "--row", "/w/a", "--decision", "merge:PR#10",
                                     "--final-message-file", message, "--question-file", question)
        _, answered = self.call("asked", "--row", "/w/a", "--decision", "merge:PR#11",
                                "--final-message-file", message, "--question-file", question)
        self.call("answered", "--id", answered["id"], "--answer-file", answer)
        _, handoff = self.call("promise", "--from-row", "/w/a", "--to-row", "/w/b",
                               "--condition", "A merged", "--task", "start B")
        self.call("leave", "--row", "/w/c", "--final-message-file", message)

        code, pending = self.call("pending")

        self.assertEqual(code, 0)
        self.assertEqual([item["id"] for item in pending["questions"]], [open_question["id"]])
        self.assertEqual([item["id"] for item in pending["handoffs"]], [handoff["id"]])
        self.assertEqual(pending["requests"], [])
        self.assertEqual([item["row"] for item in pending["left"]], ["/w/c"])

    def test_left_row_only_stays_left_for_the_saved_final_message(self) -> None:
        old = self.source("old.txt", "Work complete; leave it.\n")
        new = self.source("new.txt", "New work needed.\n")
        self.assertEqual(self.call("leave", "--row", "/w/a", "--final-message-file", old)[0], 0)
        self.assertEqual(self.call("left-now", "--row", "/w/a", "--final-message-file", old),
                         (0, {"left": True}))
        self.assertEqual(self.call("left-now", "--row", "/w/a", "--final-message-file", new),
                         (1, {"left": False}))

    def test_question_and_handoff_survive_a_new_session(self) -> None:
        message = self.source("message.txt", "Should B start?\n")
        question = self.source("question.txt", "Send B the task?\n")
        answer = self.source("answer.txt", "Yes, when A finishes.\n")
        code, asked = self.call("asked", "--row", "/w/a", "--decision", "merge:PR#10",
                                "--final-message-file", message,
                                "--question-file", question, "--asked-at", "2026-10-25T12:30:00+09:00")
        self.assertEqual(code, 0)
        self.assertEqual(asked["answer"], None)
        self.assertEqual(asked["asked_at"], "2026-10-25T12:30:00+09:00")
        self.assertEqual(self.call("asked", "--row", "/w/a", "--decision", "merge:PR#10",
                                   "--final-message-file", message,
                                   "--question-file", question)[1], asked)
        changed_message = self.source("changed-message.txt", "Still waiting.\n")
        self.assertEqual(self.call("asked", "--row", "/w/a", "--decision", "merge:PR#10",
                                   "--final-message-file", changed_message,
                                   "--question-file", question)[1], asked)
        self.call("answered", "--id", asked["id"], "--answer-file", answer,
                  "--answered-at", "2026-10-25T13:00:00+09:00")
        code, promise = self.call("promise", "--from-row", "/w/a", "--to-row", "/w/b",
                                  "--condition", "A finishes", "--task", "Start B")
        self.assertEqual(code, 0)
        self.assertIsNone(promise["delivered_at"])
        self.call("delivered", "--id", promise["id"], "--delivery", "turn_started")
        _, state = self.call("show")
        self.assertEqual(state["questions"][asked["id"]]["answer"], "Yes, when A finishes.")
        self.assertEqual(state["questions"][asked["id"]]["answered_at"], "2026-10-25T13:00:00+09:00")
        self.assertEqual(state["handoffs"][promise["id"]]["delivery"], "turn_started")

    def test_question_records_its_tab_for_later_cleanup(self) -> None:
        message = self.source("message.txt", "Ready.\n")
        question = self.source("question.txt", "Proceed?\n")
        with patch.dict(os.environ, {"ORCA_PANE_KEY": "tab:leaf"}):
            _, asked = self.call("asked", "--row", "/w/a", "--decision", "merge:PR#10",
                                 "--final-message-file", message, "--question-file", question)
        self.assertEqual(asked["owner_pane"], "tab:leaf")

    def test_get_and_conflicting_answer_show_the_recorded_answer_time_and_tab(self) -> None:
        message = self.source("message.txt", "Ready.")
        question = self.source("question.txt", "Proceed?")
        answer = self.source("answer.txt", "Yes.")
        other = self.source("other.txt", "No.")
        _, asked = self.call("asked", "--row", "/w/lane", "--decision", "proceed",
                             "--final-message-file", message, "--question-file", question)
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_answer"}):
            _, recorded = self.call("answered", "--id", asked["id"], "--answer-file", answer,
                                    "--answered-at", "2026-10-28T01:00:00Z")
        self.assertEqual(self.call("get", "--id", asked["id"]), (0, recorded))
        with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            self.assertEqual(hourly_state.main(["--state-file", str(self.state), "answered", "--id", asked["id"],
                                                "--answer-file", other]), 1)
        evidence = json.loads(err.getvalue().split("answer already recorded: ", 1)[1])
        self.assertEqual(evidence, {"answer": "Yes.", "answered_at": "2026-10-28T01:00:00+00:00",
                                    "answered_in": "term_answer"})
        saved = json.loads(self.state.read_text())
        del saved["questions"][asked["id"]]["answered_in"]
        self.state.write_text(json.dumps(saved))
        with redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            self.assertEqual(hourly_state.main(["--state-file", str(self.state), "answered", "--id", asked["id"],
                                                "--answer-file", other]), 1)
        self.assertIsNone(json.loads(err.getvalue().split("answer already recorded: ", 1)[1])["answered_in"])

    def test_checked_answer_replaces_the_recorded_one_and_keeps_it_with_the_reason(self) -> None:
        message = self.source("message.txt", "Ready.")
        question = self.source("question.txt", "下書きのとおり投稿しますか？")
        noted = self.source("noted.txt", "「下書きのとおり投稿」。メモ「不要.」")
        checked = self.source("checked.txt", "返事の投稿は不要")
        reason = self.source("reason.txt", "メモの読みを 1 問で確かめた")
        _, asked = self.call("asked", "--row", "/w/lane", "--decision", "post:TOUGOU_KAIKEI-799",
                             "--final-message-file", message, "--question-file", question)
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term_hourly"}):
            _, first = self.call("answered", "--id", asked["id"], "--answer-file", noted,
                                 "--answered-at", "2026-11-04T07:14:00Z")
            _, replaced = self.call("answered", "--id", asked["id"], "--answer-file", checked,
                                    "--replace-reason-file", reason)
        self.assertEqual(replaced["answer"], "返事の投稿は不要")
        self.assertEqual([{key: earlier[key] for key in ("answer", "answered_at", "answered_in", "reason")}
                          for earlier in replaced["replaced"]],
                         [{"answer": first["answer"], "answered_at": first["answered_at"],
                           "answered_in": "term_hourly", "reason": "メモの読みを 1 問で確かめた"}])
        self.assertIn("replaced_at", replaced["replaced"][0])
        self.assertEqual(self.call("get", "--id", asked["id"]), (0, replaced))

    def test_answer_without_a_terminal_handle_records_its_worktree(self) -> None:
        message = self.source("message.txt", "Ready.")
        _, asked = self.call("asked", "--row", "/w/lane", "--decision", "proceed",
                             "--final-message-file", message, "--question-file", message)
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": ""}):
            _, recorded = self.call("answered", "--id", asked["id"], "--answer-file", message)
        self.assertEqual(recorded["answered_in"], "/w/triage")

    def test_summary_of_pending_and_stale_asks_has_one_line_with_each_items_identity_and_age(self) -> None:
        message = self.source("message.txt", "Choose a release\n" + "detail " * 50)
        _, asked = self.call("asked", "--row", "/w/lane", "--decision", "release",
                             "--asked-at", "2026-10-28T01:00:00Z",
                             "--final-message-file", message, "--question-file", message)
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "class": "waiting", "repo": "app",
                                         "claude": {"final_text": "Choose a release", "final_at": "2026-10-28T01:00:00Z"}}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": "abcd1234-0000", "kind": "ask", "worktree": "lane",
                                               "ts": "2026-10-28T01:00:00Z", "text": "Choose a release"}])
        for argv, identity in ((["pending", "--summary"], f"id={asked['id']}"),
                               (["stale-asks", "--from", str(scan_json), "--summary"], "ask=abcd1234-0000")):
            with self.subTest(command=argv[0]), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(hourly_state.main(["--state-file", str(self.state), *argv]), 0)
                lines = out.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertIn("lane | unanswered | since=2026-10-28T01:00:00", lines[0])
                self.assertRegex(lines[0], r"age_minutes=\d+")
                self.assertIn(identity, lines[0])
                self.assertIn("Choose a release", lines[0])

    def test_stale_asks_name_an_ask_another_tab_is_asking(self) -> None:
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "class": "waiting", "repo": "app",
                                         "claude": {"final_text": "Choose a release", "final_at": "2026-10-28T01:00:00Z"}}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": "abcd1234-0000", "kind": "ask", "worktree": "lane",
                                               "ts": "2026-10-28T01:00:00Z", "text": "Choose a release",
                                               "questions": [{"question": "Which?"}, {"question": "When?"}]}])
        claims = self.root / "claims.jsonl"
        outputs = []
        # One question claimed leaves the other to ask; both claimed, the ask is being asked.
        for claimed in ("abcd1234-0000:1", "abcd1234-0000:2"):
            with claims.open("a") as stream:
                stream.write(json.dumps({"id": claimed, "by": "triage-lead", "ts": "2026-10-28T10:05:00+09:00"}) + "\n")
            with patch.dict(os.environ, {"TRIAGE_ASK_CLAIMS": str(claims)}):
                for summary in ([], ["--summary"]):
                    with redirect_stdout(io.StringIO()) as out:
                        self.assertEqual(hourly_state.main(["--state-file", str(self.state), "stale-asks",
                                                            "--from", str(scan_json), *summary]), 0)
                    outputs.append(out.getvalue())
        self.assertIsNone(json.loads(outputs[0])[0]["ask"]["claimed"])
        self.assertNotIn("伺い中", outputs[1])
        self.assertEqual(json.loads(outputs[2])[0]["ask"]["claimed"],
                         {"by": "triage-lead", "at": "2026-10-28T10:05:00+09:00"})
        self.assertIn("伺い中 triage-lead", outputs[3])

    def test_asked_skips_all_closed_references_in_the_rows_origin_and_pending_explains_closure(self) -> None:
        lane = self.root / "lane"
        subprocess.run(["git", "init", "-q", str(lane)], check=True)
        subprocess.run(["git", "-C", str(lane), "remote", "add", "origin", "git@github.com:acme/app.git"], check=True)
        question = self.source("question.txt", "#10 と #11 を閉じますか？")
        args = ("asked", "--row", str(lane), "--decision", "close",
                "--final-message-file", question, "--question-file", question)
        closed = {f"item_{number}": {"state": "CLOSED", "updatedAt": "2026-10-28T01:00:00Z"}
                  for number in (10, 11)}
        with patch.object(hourly_state.scan_sessions, "graphql_repository", return_value=closed) as read:
            code, skipped = self.call(*args)
        self.assertEqual(code, 0)
        self.assertFalse(skipped["recorded"])
        self.assertEqual(self.call("show")[1]["questions"], {})
        self.assertEqual(read.call_args.args[0], "acme/app")
        self.assertIn("issueOrPullRequest(number: 11)", read.call_args.args[1])
        with patch.object(hourly_state.scan_sessions, "graphql_repository",
                          return_value={**closed, "item_11": {"state": "OPEN"}}):
            _, asked = self.call(*args)
        with patch.object(hourly_state.scan_sessions, "graphql_repository", return_value=closed):
            _, pending = self.call("pending")
        self.assertEqual(pending["questions"], [])
        self.assertEqual(pending["settled"][0]["id"], asked["id"])
        self.assertEqual([item["number"] for item in pending["settled"][0]["evidence"]], [10, 11])
        # The same path on another Mac may hold another repository: no evidence, so it is recorded.
        with patch.object(hourly_state.scan_sessions, "graphql_repository", return_value=closed):
            _, remote = self.call("asked", *args[1:2], f"mbp16::{lane}", *args[3:])
        self.assertIsNone(remote["answer"])

    def test_pending_settles_covers_only_after_every_keys_answer_is_later_than_the_question(self) -> None:
        message = self.source("message.txt", "Choose both releases?")
        _, asked = self.call("asked", "--row", "/w/lane", "--decision", "both",
                             "--final-message-file", message, "--question-file", message,
                             "--asked-at", "2026-10-28T01:00:00Z", "--covers", "first", "--covers", "second")
        log = self.root / "decisions.jsonl"
        records = [{"id": key, "decision": key, "answer": f"answer {key}", "decided_at": at}
                   for key, at in (("first", "2026-10-28T02:00:00Z"), ("second", "2026-10-28T01:00:00Z"))]
        write_jsonl(log, records)
        self.assertEqual(self.call("pending")[1]["questions"][0]["id"], asked["id"])
        records.append({"id": "later", "decision": "second", "answer": "second settled",
                        "decided_at": "2026-10-28T03:00:00Z"})
        write_jsonl(log, records)
        _, pending = self.call("pending")
        self.assertEqual(pending["questions"], [])
        self.assertEqual([(item["decision"], item["decided_at"]) for item in pending["settled"][0]["evidence"]],
                         [("first", "2026-10-28T02:00:00Z"), ("second", "2026-10-28T03:00:00Z")])
        self.assertIsNone(self.call("get", "--id", asked["id"])[1]["answer"])

    def test_withdraw_keeps_a_reason_and_time_without_recording_a_user_answer(self) -> None:
        message = self.source("message.txt", "Proceed?")
        reason = self.source("reason.txt", "Superseded by the new request.")
        _, asked = self.call("asked", "--row", "/w/lane", "--decision", "proceed",
                             "--final-message-file", message, "--question-file", message)
        self.assertFalse(cleanup_hourly.no_pending_questions(self.state, asked["owner_pane"]))
        _, withdrawn = self.call("withdraw", "--id", asked["id"], "--reason-file", reason)
        self.assertIsNone(withdrawn["answer"])
        self.assertTrue(cleanup_hourly.no_pending_questions(self.state, asked["owner_pane"]))
        self.assertEqual(withdrawn["withdrawal_reason"], "Superseded by the new request.")
        self.assertIsNotNone(scan.parse_time(withdrawn["withdrawn_at"]))
        self.assertEqual(self.call("withdraw", "--id", asked["id"], "--reason-file", reason)[1], withdrawn)
        pending = self.call("pending")[1]
        self.assertEqual(pending["questions"], [])
        self.assertEqual(pending["settled"][0]["state"], "withdrawn")

    def test_pending_finds_an_unrecorded_answer_in_an_earlier_runs_transcript(self) -> None:
        message = self.source("message.txt", "Which release?")
        _, asked = self.call("asked", "--row", "/w/lane", "--decision", "release",
                             "--final-message-file", message, "--question-file", message,
                             "--asked-at", "2026-10-28T01:00:00Z")
        transcript = self.root / "claude/projects/-w-triage/earlier-run.jsonl"
        write_jsonl(transcript, [{**tool_result("answered", {"questions": [{"question": "Which release?"}],
                                                            "answers": {"Which release?": "Release B"}}),
                                 "timestamp": "2026-10-28T02:00:00Z"}])
        _, pending = self.call("pending")
        found = pending["questions"][0]
        self.assertEqual(found["id"], asked["id"])
        self.assertEqual(found["unrecorded_answers"][0]["at"], "2026-10-28T02:00:00+00:00")
        self.assertIn("Release B", found["unrecorded_answers"][0]["text"])
        self.assertIsNone(self.call("get", "--id", asked["id"])[1]["answer"])

    def test_same_question_for_a_later_pr_is_a_new_decision(self) -> None:
        message = self.source("message.txt", "Ready to merge.\n")
        question = self.source("question.txt", "Merge this PR?\n")
        no = self.source("no.txt", "No\n")
        yes = self.source("yes.txt", "Yes\n")
        _, earlier = self.call("asked", "--row", "/w/a", "--decision", "merge:PR#10",
                               "--final-message-file", message, "--question-file", question)
        self.call("answered", "--id", earlier["id"], "--answer-file", no)
        _, later = self.call("asked", "--row", "/w/a", "--decision", "merge:PR#11",
                             "--final-message-file", message, "--question-file", question)
        self.assertNotEqual(later["id"], earlier["id"])
        self.assertIsNone(later["answer"])
        self.call("answered", "--id", later["id"], "--answer-file", yes)
        _, state = self.call("show")
        self.assertEqual(state["questions"][earlier["id"]]["answer"], "No")
        self.assertEqual(state["questions"][later["id"]]["answer"], "Yes")

    def test_malformed_state_is_not_overwritten(self) -> None:
        self.state.write_text("broken")
        message = self.source("message.txt", "hello")
        with redirect_stderr(io.StringIO()):
            code = hourly_state.main(["--state-file", str(self.state), "leave", "--row", "/w/a",
                                      "--final-message-file", message])
        self.assertEqual(code, 1)
        self.assertEqual(self.state.read_text(), "broken")

    def test_request_stays_open_after_worker_done_until_symptom_is_verified(self) -> None:
        original = self.source("original.txt", "direnv が固まって開発に支障がある\n")
        symptom = self.source("symptom.txt", "各 session の direnv が固まる\n")
        evidence = self.source("evidence.txt", "実 session で direnv が止まらないことを確認\n")
        code, request = self.call("request-open", "--original-file", original,
                                  "--symptom-file", symptom, "--owner", "/w/slow-lane")
        self.assertEqual(code, 0)
        self.assertEqual(request["original"], "direnv が固まって開発に支障がある")
        self.assertEqual(request["owner"], "/w/slow-lane")
        self.assertIsNone(request["closed_at"])
        self.assertIsNone(request["closure_evidence"])
        self.assertEqual(self.call("requests-needing-owner")[1]["needs_owner"][0]["id"], request["id"])
        self.assertEqual(self.call("requests-needing-owner", "--live-owner", "/w/slow-lane"),
                         (0, NOTHING_OWED))
        self.call("request-assign", "--id", request["id"], "--owner", "/w/next-lane")
        self.assertEqual(self.call("requests-needing-owner", "--live-owner", "/w/slow-lane")[1]["needs_owner"][0]["id"],
                         request["id"])
        self.assertEqual(self.call("request-close", "--id", request["id"],
                                   "--evidence-file", evidence)[1]["closure_evidence"],
                         "実 session で direnv が止まらないことを確認")
        self.assertEqual(self.call("requests-needing-owner"), (0, NOTHING_OWED))
        self.assertEqual(self.call("show")[1]["requests"][request["id"]]["symptom"],
                         "各 session の direnv が固まる")

    def test_open_issues_of_listed_repos_without_a_checkout_become_requests_needing_owner(self) -> None:
        (self.root / "issue-repos.json").write_text(json.dumps({"repos": ["acme/app"]}))
        local = self.root / "app-1"
        subprocess.run(["git", "init", "-q", str(local)], check=True)
        subprocess.run(["git", "-C", str(local), "remote", "add", "origin", "git@github.com:acme/app.git"],
                       check=True)
        url = "https://github.com/acme/app/issues/"
        issues = [{"number": n, "title": f"issue {n}", "url": url + str(n), "assignees": [],
                   "closedByPullRequestsReferences": [{"number": 7}] if n == 2 else []} for n in (1, 2, 3)]
        worktrees = [
            {"path": str(local), "repo": "app", "linkedIssue": 1, "linkedItems": [], "linkedPR": None},
            {"path": "/remote/app-pr7", "repo": "app", "_environment": "mbp16", "linkedIssue": None,
             "linkedItems": [{"provider": "github", "type": "pr", "number": 7}],
             "linkedPR": {"number": 7, "url": "https://github.com/acme/app/pull/7"}},
            # Issue 3 of another repository is not acme/app#3.
            {"path": "/w/other", "repo": "other", "linkedIssue": 3, "linkedItems": [], "linkedPR": None},
        ]
        live = ["--live-owner", str(local), "--live-owner", "mbp16::/remote/app-pr7"]
        with patch.object(hourly_state, "gh_open_issues", return_value=issues) as listed, \
                patch.object(hourly_state, "orca_worktrees", return_value=worktrees):
            code, needing = self.call("requests-needing-owner", *live)
            again = self.call("requests-needing-owner", *live)

        listed.assert_called_with("acme/app")
        self.assertEqual(code, 0)
        self.assertEqual([request["source"]["url"] for request in needing["needs_owner"]], [url + "3"])
        self.assertEqual(again[1], needing)
        owners = {request["source"]["url"]: request["owner"]
                  for request in self.call("show")[1]["requests"].values()}
        self.assertEqual(owners, {url + "1": str(local), url + "2": "mbp16::/remote/app-pr7", url + "3": None})

    def test_saved_scan_names_live_owners_and_links_issues_without_reading_orca(self) -> None:
        (self.root / "issue-repos.json").write_text(json.dumps({"repos": ["acme/app"]}))
        url = "https://github.com/acme/app/issues/"
        # A lane at work: its agent tab makes it a live owner, and Orca links it to issue 1.
        lane = {**worktree("/w/lane", "working", pr={"number": 9, "url": "https://github.com/acme/app/pull/9"}),
                "linkedIssue": 1}
        rows = [
            *scan.build_inventory([lane], [terminal("/w/lane", "claude")], str(self.root / "claude"), {}, "要判断",
                                  now=NOW),
            # A careful read finished it, though its agent tab is still open.
            {"path": "/w/done", "environment": None, "repo": "app", "class": "unsure",
             "verdict": {"class": "finished"}, "linked_pr": None, "terminals": [{"handle": "t2", "agent": "codex"}]},
            # Only a shell is left in it on the other Mac.
            {"path": "/w/shell", "environment": "mbp16", "repo": "app", "class": "working", "linked_pr": None,
             "terminals": [{"handle": "t3", "agent": None}]},
        ]
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps(rows))
        original = self.source("original.txt", "直してほしい")
        symptom = self.source("symptom.txt", "止まる")
        for owner in ("/w/lane", "/w/done", "mbp16::/w/shell", "/w/manual"):
            self.call("request-open", "--original-file", original, "--symptom-file", symptom, "--owner", owner)

        # An Orca that cannot run: reading it would leave the issues unimported.
        with patch.object(hourly_state, "gh_open_issues", return_value=[gh_issue(url, 1), gh_issue(url, 2)]):
            code, report = self.call("requests-needing-owner", "--from", str(scan_json),
                                     "--live-owner", "/w/manual", "--orca", str(self.root / "no-orca"))

        self.assertEqual(code, 0)
        self.assertEqual([request["source"]["url"] if request.get("source") else request["owner"]
                          for request in report["needs_owner"]], ["/w/done", "mbp16::/w/shell", url + "2"])

    def test_stale_asks_names_waiting_rows_whose_question_is_unasked_unanswered_or_answered_and_left(self) -> None:
        # 2026-10-29: a choice sent as info waited 14 hours, and an ask marked answered without an answer 25 hours.
        def waiting(name: str, asked_at: str, said_at: str) -> dict:
            return {"path": f"/w/{name}", "environment": None, "repo": "app", "class": "waiting", "codex": [],
                    "claude": {"last_user_at": asked_at, "final_at": said_at, "final_text": "返事を待ちます"},
                    "terminals": [{"handle": f"term_{name}", "agent": "claude"}]}
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([
            waiting("orca-deeplink-pr", "2026-10-28T10:55:00Z", "2026-10-28T11:02:30Z"),
            waiting("report-decisions", "2026-10-28T00:10:00Z", "2026-10-28T00:19:00Z"),
            waiting("fix-login", "2026-10-28T00:55:00Z", "2026-10-28T01:01:00Z"),
            waiting("rpt-ui-bugs", "2026-10-28T05:00:00Z", "2026-10-28T05:10:00Z"),
            {**waiting("unrelated", "2026-10-28T00:55:00Z", "2026-10-28T01:01:00Z"), "class": "working"},
        ]))
        inbox, answers, log = (self.root / name for name in ("inbox.jsonl", "answers.jsonl", "decisions.jsonl"))
        write_jsonl(inbox, [
            {"id": "897047e0-0000", "ts": "2026-10-28T20:02:05+0900", "kind": "info", "worktree": "orca-deeplink-pr",
             "text": "写真を上げられなかった。進め方はどちらか。\n1. ログインする\n2. 貼る"},
            {"id": "4bd9cae3-0000", "ts": "2026-10-28T09:18:41+0900", "kind": "ask", "worktree": "report-decisions",
             "text": "c1 の全文です"},
            {"id": "aa110000-0000", "ts": "2026-10-28T09:58:00+0900", "kind": "ask", "worktree": "fix-login",
             "text": "deploy してよいか"},
            # An open ask of the previous turn, and one of the last turn answered before the lane's last message.
            {"id": "qq110000-0000", "ts": "2026-10-28T08:00:00+0900", "kind": "ask", "worktree": "rpt-ui-bugs",
             "text": "Q1"},
            {"id": "qq220000-0000", "ts": "2026-10-28T14:05:00+0900", "kind": "ask", "worktree": "rpt-ui-bugs",
             "text": "Q2"},
        ])
        write_jsonl(answers, [{"id": "4bd9cae3-0000", "ts": "2026-10-29T06:23:48+09:00",
                               "how": "inbox_read.py answered", "note": "まとめて印"},
                              {"id": "qq220000-0000", "ts": "2026-10-28T14:08:00+09:00",
                               "how": "inbox_read.py answered", "note": "答えた"}])
        write_jsonl(log, [
            {"id": "1", "decision": "answer:report-decisions::figma-3", "answer": "Figma の件は写しで確かめる",
             "decided_at": "2026-10-28T05:36:53+00:00"},
            {"id": "2", "decision": "answer:fix-login::wait", "answer": "ask aa110000 はまず待つ",
             "decided_at": "2026-10-28T00:59:00+00:00"},
            {"id": "3", "decision": "answer:fix-login::deploy", "answer": "ask aa110000 は deploy しない",
             "decided_at": "2026-10-28T02:00:00+00:00"},
            {"id": "4", "decision": "answer:fix-login::deploy", "answer": "ask aa110000 は deploy してよい",
             "decided_at": "2026-10-28T03:00:00+00:00"},
            {"id": "5", "decision": "answer:rpt-ui-bugs::q2", "answer": "ask qq220000 は B",
             "decided_at": "2026-10-28T05:07:00+00:00"},
            {"id": "6", "decision": "answer:orca-deeplink-pr::photos", "answer": "写真なしで進める",
             "decided_at": "2026-10-29T01:21:00+00:00"},
        ])

        with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_ASK_ANSWERS": str(answers)}):
            code, stale = self.call("stale-asks", "--from", str(scan_json), "--decisions-file", str(log),
                                    "--claude-projects", str(self.root / "projects"))

        self.assertEqual(code, 0)
        self.assertEqual([(item["row"], item["state"], item["since"]) for item in stale], [
            ("/w/rpt-ui-bugs", "unanswered", "2026-10-28T08:00:00+0900"),
            ("/w/report-decisions", "unanswered", "2026-10-28T09:18:41+0900"),
            ("/w/fix-login", "stopped_after_answer", "2026-10-28T03:00:00+00:00"),
            ("/w/orca-deeplink-pr", "unasked", "2026-10-28T11:02:30Z")])
        rpt_ui, report, fix_login, deeplink = stale
        self.assertEqual((report["ask"]["marked"], [answer["source"] for answer in report["candidates"]]),
                         (True, ["lead_decisions answer:report-decisions::figma-3"]))
        self.assertIn("deploy してよい", fix_login["answer"]["text"])
        self.assertEqual([info["id"] for info in deeplink["choices_in_info"]], ["897047e0-0000"])
        self.assertEqual([answer["source"] for answer in deeplink["candidates"]],
                         ["lead_decisions answer:orca-deeplink-pr::photos"])
        self.assertTrue(all(isinstance(item["age_minutes"], int) for item in stale))

    def test_stale_asks_settles_a_row_its_key_answered_after_its_last_message_past_a_delivery_note(self) -> None:
        # 2026-11-06: montly-report was unasked although ask:montly-report::spare-24man held the answer under 「中継済み」.
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([
            {"path": f"/w/{name}", "repo": "app", "class": "waiting",
             "claude": {"final_at": "2026-11-02T01:46:39Z", "final_text": "総額は 1,000 万円か 976 万円か"},
             # A question on its screen or pending in its transcript came after the answer, though its
             # transcript's last message did not.
             **({"prompt_waiting": {"tab": "t", "question": "10月の報告書をどう送りますか？", "choices": ["1. 任せる"]}}
                if name == "asking-on-screen" else {})}
            for name in ("montly-report", "earlier-answer", "asking-on-screen")]
            + [{"path": "/w/asking-in-transcript", "repo": "app", "class": "waiting",
                "claude": {"final_at": None, "pending_questions": [{"question": "10月の報告書をどう送りますか？"}]}}]))
        write_jsonl(self.root / "inbox.jsonl", [])
        write_jsonl(self.root / "decisions.jsonl", [
            {"id": "1", "decision": "ask:montly-report::spare-24man", "answer": "1,000 万円のまま",
             "decided_at": "2026-11-05T12:21:00+09:00"},
            {"id": "2", "decision": "ask:montly-report::spare-24man", "answer": "中継済み（12:2x send montly-report）",
             "decided_at": "2026-11-05T04:16:40+00:00"},
            {"id": "3", "decision": "ask:earlier-answer::total", "answer": "976 万円",
             "decided_at": "2026-11-01T09:00:00+09:00"},
            {"id": "4", "decision": "ask:asking-on-screen::total", "answer": "976 万円",
             "decided_at": "2026-11-05T12:21:00+09:00"},
            {"id": "5", "decision": "ask:asking-in-transcript::total", "answer": "976 万円",
             "decided_at": "2026-11-05T12:21:00+09:00"}])

        code, stale = self.call("stale-asks", "--from", str(scan_json))

        self.assertEqual(code, 0)
        self.assertEqual({item["name"]: item["state"] for item in stale},
                         {"montly-report": "settled", "earlier-answer": "unasked", "asking-on-screen": "unasked",
                          "asking-in-transcript": "unasked"})
        settled = next(item for item in stale if item["state"] == "settled")
        self.assertEqual(settled["evidence"], [{"decision": "ask:montly-report::spare-24man",
                                                "decided_at": "2026-11-05T12:21:00+09:00", "answer": "1,000 万円のまま"}])

    def test_stale_asks_and_lead_state_name_questions_closed_unanswered_since_the_last_handoff(self) -> None:
        # 2026-11-06: the lead's two questions closed unanswered while lead-state showed no pending question.
        question = {"questions": [{"question": "ECR の tag を付けますか？", "options": [{"label": "付ける"}]}]}

        def asked(tool_id: str, at: str) -> dict:
            return {**assistant({"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": question}),
                    "timestamp": at}

        def rejected(tool_id: str, at: str) -> dict:
            return {"type": "user", "timestamp": at, "toolUseResult": "User rejected tool use", "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "is_error": True,
                                             "content": "The user doesn't want to proceed with this tool use."}]}}

        lead = self.root.resolve() / "lead"
        lead_dir = scan.claude_project_dir(self.root / "claude/projects", str(lead))
        # The lead closed the question, then started the session whose transcript is now the newest.
        write_jsonl(lead_dir / "closed.jsonl", [
            asked("before", "2026-11-05T09:00:00Z"), rejected("before", "2026-11-05T09:10:00Z"),
            asked("after", "2026-11-05T10:39:58Z"), user("別の話"), rejected("after", "2026-11-06T00:04:38Z")])
        os.utime(lead_dir / "closed.jsonl", (1, 1))
        write_jsonl(lead_dir / "lead.jsonl", [
            asked("answered", "2026-11-06T00:18:21Z"),
            user([{"type": "tool_result", "tool_use_id": "answered", "content": "答えた"}])])
        handoff.SavedHandoff(text="前回", kind="info", decisions=[], sent_at="2026-11-05T10:00:00+00:00",
                             next_offset=0).save(self.root / "handoffs", datetime(2026, 11, 5, 10, tzinfo=timezone.utc))
        self.call("appoint-lead", "--worktree", str(lead))
        scan_json = self.root / "scan.json"
        scan_json.write_text("[]")
        write_jsonl(self.root / "inbox.jsonl", [])
        replies: dict[tuple[str, ...], dict] = {("worktree", "ps"): {"worktrees": [{**worktree(str(lead)), "agents": []}]},
                   ("terminal", "list"): {"terminals": []}}

        _, stale = self.call("stale-asks", "--from", str(scan_json))
        with patch.object(handoff.scan_sessions, "process_table", return_value={}), \
                patch("subprocess.run", side_effect=lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, json.dumps({"result": replies[tuple(args[1:-1])]}))), redirect_stdout(io.StringIO()) as out:
            code = handoff.main(["lead-state", "--state-file", str(self.state), "--orca", "fake-orca",
                                 "--claude-projects", str(self.root / "claude/projects")])

        self.assertEqual([(item["state"], item["name"], item["since"], item["closed_at"], item["questions"])
                          for item in stale],
                         [("rejected", "lead", "2026-11-05T10:39:58Z", "2026-11-06T00:04:38Z", question["questions"])])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["rejected"], [
            {"id": "after", "asked_at": "2026-11-05T10:39:58Z", "closed_at": "2026-11-06T00:04:38Z", **question}])

    def test_stale_asks_shows_the_pr_review_beside_its_merge_question(self) -> None:
        review_dir = self.root / "review"
        review_dir.mkdir()
        result = review_dir / "acme-app-12.md"
        result.write_text("**判定：直してから（Request changes）**\n", encoding="utf-8")
        (review_dir / "acme-app-12.reviewed.json").write_text(json.dumps(
            {"head": "abc123def", "sha256": hashlib.sha256(result.read_bytes()).hexdigest()}))
        (self.root / "inbox.jsonl").write_text("")
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{
            "path": "/w/app-pr12", "environment": None, "repo": "app", "class": "waiting", "codex": [],
            "github": "acme/app", "pr": {"number": 12, "url": "https://github.com/acme/app/pull/12",
                                          "head_oid": "abc123def4567"},
            "claude": {"last_user_at": "2026-10-28T00:10:00Z", "final_at": "2026-10-28T00:19:00Z",
                       "final_text": "直した内容です。" * 30 + "PR #12 を merge してよいですか"},
            "terminals": [{"handle": "term_app", "agent": "claude"}]}]))

        with patch.dict(os.environ, {"TRIAGE_REVIEW_DIR": str(review_dir)}):
            code, stale = self.call("stale-asks", "--from", str(scan_json), "--claude-projects",
                                    str(self.root / "projects"))
            out = io.StringIO()
            with redirect_stdout(out):
                hourly_state.main(["--state-file", str(self.state), "stale-asks", "--from", str(scan_json),
                                   "--claude-projects", str(self.root / "projects"), "--summary"])

        self.assertEqual(code, 0)
        self.assertEqual((stale[0]["merge_question"], stale[0]["review"]["changes_requested"],
                          stale[0]["review"]["path"]), (True, True, str(result)))
        self.assertIn("merge の問い", out.getvalue())
        self.assertIn("直してから", out.getvalue())

    def test_stale_asks_excludes_its_own_row_left_messages_and_handoffs_and_fills_questions(self) -> None:
        message = self.source("message.txt", "Leave this message.")
        self.call("leave", "--row", "/w/left", "--final-message-file", message)
        self.call("leave", "--row", "/w/changed", "--final-message-file", message)
        rows: list[dict] = [{"path": f"/w/{name}", "class": "waiting", "repo": "app",
                 "claude": {"final_text": final, "final_at": "2026-10-28T01:00:00Z"}}
                for name, final in (("triage", "Own question"), ("left", "Leave this message."),
                                    ("changed", "A new question"), ("comment", ""), ("screen", ""))]
        rows[3]["comment"] = "要判断: 公開は明日でよいですか？"
        # A verdict's question comes after the marked comment, as --row shows it.
        rows[3].update(verdict={"class": "waiting", "question": "古い問い"}, marker_question="公開は明日でよいですか？")
        rows[4]["done_cards"] = [{"screen_tail": ["⏺ 画像を使いますか？", "✻ Worked for 1m", "❯"]}]
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps(rows))
        body = "Earlier triage handoff\n—— 送る直前に道具が足した事実 ——\nNo new reports."
        write_jsonl(self.root / "inbox.jsonl", [{"id": "abcd1234-0000", "kind": "ask", "worktree": "comment",
                                               "ts": "2026-10-28T01:00:00Z", "text": body}])
        _, stale = self.call("stale-asks", "--from", str(scan_json))
        self.assertEqual([item["name"] for item in stale], ["changed", "comment", "screen"])
        self.assertEqual(stale[1]["question"], "公開は明日でよいですか？")
        self.assertEqual(stale[2]["question"], "画像を使いますか？")
        self.assertTrue(all(item["state"] == "unasked" for item in stale))

    def test_a_decision_naming_an_old_ask_does_not_answer_the_new_id_in_its_body(self) -> None:
        rows: list[dict] = [{"path": f"/w/{name}", "class": "waiting", "repo": "app",
                 "claude": {"final_text": "Waiting.", "final_at": "2026-10-28T01:00:00Z"}}
                for name in ("old-lane", "new-lane")]
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps(rows))
        write_jsonl(self.root / "inbox.jsonl", [
            {"id": ask_id, "kind": "ask", "worktree": name, "text": "Proceed?", "ts": "2026-10-28T01:00:00Z"}
            for name, ask_id in (("old-lane", "89d59136-1111"), ("new-lane", "abcd1234-2222"))])
        write_jsonl(self.root / "decisions.jsonl", [{"id": "1", "decision": "ask-89d59136-superseded",
                                                   "answer": "Continue with abcd1234-2222 instead.",
                                                   "decided_at": "2026-10-28T02:00:00Z"}])
        _, stale = self.call("stale-asks", "--from", str(scan_json))
        by_name = {item["name"]: item for item in stale}
        self.assertEqual(by_name["old-lane"]["state"], "stopped_after_answer")
        self.assertEqual(by_name["new-lane"]["state"], "unanswered")
        self.assertEqual(by_name["new-lane"]["candidates"], [])

    def test_unanswered_handoff_key_keeps_a_marked_old_ask_even_on_a_finished_row(self) -> None:
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "class": "finished", "repo": "app",
                                         "claude": {"final_at": "2026-10-29T01:00:00Z", "final_text": "Finished."}}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": ask_id, "kind": "ask", "worktree": "lane",
                                               "text": "Earlier question", "ts": "2026-10-28T01:00:00Z"}
                                              for ask_id in ("89d59136-1111", "abcd1234-2222")])
        write_jsonl(self.root / "answers.jsonl", [{"id": ask_id, "ts": "2026-10-28T02:00:00Z"}
                                                for ask_id in ("89d59136-1111", "abcd1234-2222")])
        write_jsonl(self.root / "handed-off.jsonl", [{"decision": key, "handed_off_at": "2026-10-28T02:00:00Z"}
                                                   for key in ("ask-89d59136", "ask-abcd1234")])
        _, stale = self.call("stale-asks", "--from", str(scan_json))
        self.assertEqual([item["ask"]["id"] for item in stale], ["89d59136-1111", "abcd1234-2222"])
        self.assertEqual((stale[0]["row"], stale[0]["state"], stale[0]["ask"]["marked"]),
                         ("/w/lane", "unanswered", True))
        self.assertEqual(stale[0]["question"], "Earlier question")
        self.assertEqual(stale[0]["decisions"], ["ask-89d59136"])

    def test_blocked_on_miura_includes_current_pr_state_update_and_time_since_answer(self) -> None:
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "class": "stalled", "repo": "app",
                                         "git": {"github": "acme/app"}, "pr": {"number": 10, "state": "OPEN"},
                                         "verdict": {"class": "blocked", "waits_on": "三浦さんの判断", "needs_user": "判断"},
                                         "claude": {"final_text": "Waiting.", "final_at": "2026-10-28T01:00:00Z"}}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": "abcd1234-0000", "kind": "ask", "worktree": "lane",
                                               "text": "Deploy?", "ts": "2026-10-28T01:00:00Z"}])
        write_jsonl(self.root / "decisions.jsonl", [{"id": "1", "decision": "ask-abcd1234", "answer": "Deploy.",
                                                   "decided_at": "2026-10-28T02:00:00Z"}])
        github = FakeGitHub({"acme/app": [pr_node(10, state="MERGED", updated_at="2026-10-28T03:00:00Z")]})
        with patch.object(hourly_state.scan_sessions.subprocess, "run", side_effect=github):
            _, stale = self.call("stale-asks", "--from", str(scan_json))
        self.assertEqual(stale[0]["state"], "stopped_after_answer")
        self.assertEqual(stale[0]["pr"], {"state": "MERGED", "head": "aaa", "updatedAt": "2026-10-28T03:00:00Z"})
        self.assertEqual(stale[0]["answer_age_minutes"], stale[0]["age_minutes"])
        self.assertEqual(len(github.graphql()), 1)
        rows = json.loads(scan_json.read_text())
        rows[0]["claude"]["final_at"] = "2026-10-28T03:00:00Z"
        scan_json.write_text(json.dumps(rows))
        with patch.object(hourly_state.scan_sessions.subprocess, "run", side_effect=github):
            _, continued = self.call("stale-asks", "--from", str(scan_json))
        self.assertEqual(continued[0]["state"], "unasked")
        self.assertEqual(continued[0]["answer_age_minutes"], continued[0]["age_minutes"] + 60)

    def test_appointed_lead_is_replaced_and_is_the_default_transcript_source_for_stale_asks(self) -> None:
        lead = self.root.resolve() / "lead"
        self.call("appoint-lead", "--worktree", str(self.root / "old"))
        code, appointed = self.call("appoint-lead", "--worktree", str(lead / ".." / "lead"),
                                    "--appointed-at", "2026-10-28T11:00:00+09:00")
        self.assertEqual((code, appointed), (0, {"worktree": str(lead), "appointed_at": "2026-10-28T02:00:00+00:00"}))
        self.assertEqual(hourly_state.appointed_lead(self.call("show")[1]), lead)
        transcript = scan.claude_project_dir(self.root / "claude/projects", str(lead)) / "lead.jsonl"
        write_jsonl(transcript, [
            {**user("公開日は未定です"), "timestamp": "2026-10-28T00:00:00Z"},
            {**user("公開日は明日にしましょう"), "timestamp": "2026-10-28T02:00:00Z"},
            {**user("無関係の発言"), "timestamp": "2026-10-28T03:00:00Z"},
            {**user("公開日についての内部通知"), "isMeta": True, "timestamp": "2026-10-28T04:00:00Z"},
        ])
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "class": "waiting", "repo": "app",
                                         "claude": {"final_text": "公開日を決めますか？", "final_at": "2026-10-28T01:00:00Z"}}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": "abcd1234-0000", "kind": "ask", "worktree": "lane",
                                               "text": "公開日を決めますか？", "ts": "2026-10-28T01:00:00Z"}])
        _, stale = self.call("stale-asks", "--from", str(scan_json))
        self.assertEqual(stale[0]["state"], "unanswered")
        self.assertEqual([item["text"] for item in stale[0]["candidates"]], ["公開日は明日にしましょう"])
        _, explicit = self.call("stale-asks", "--from", str(scan_json), "--asked-in", str(lead))
        self.assertEqual([{**item, "age_minutes": 0} for item in explicit],
                         [{**item, "age_minutes": 0} for item in stale])
        _, elsewhere = self.call("stale-asks", "--from", str(scan_json), "--asked-in", str(self.root / "elsewhere"))
        self.assertEqual(elsewhere[0]["candidates"], [])

    def test_a_saved_scan_takes_the_verdicts_classed_after_it_was_saved(self) -> None:
        # The hourly run saves the scan before orca-row-classify reads its queue; that verdict lands in the store.
        row = {"path": "/w/classed", "environment": None, "repo": "app", "class": "unsure", "linked_pr": None,
               "claude": {"final_at": "2026-10-28T01:00:00Z"}, "codex": [],
               "terminals": [{"handle": "t1", "agent": "claude"}]}
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([row]))
        store = self.root / "verdicts" / "verdicts.json"
        store.parent.mkdir()
        store.write_text(json.dumps({"/w/classed": {"class": "finished",
                                                    "read": hourly_state.scan_sessions.read_key(row)}}))
        original = self.source("original.txt", "直してほしい")
        symptom = self.source("symptom.txt", "止まる")
        self.call("request-open", "--original-file", original, "--symptom-file", symptom, "--owner", "/w/classed")

        code, report = self.call("requests-needing-owner", "--from", str(scan_json), "--verdicts", str(store))

        self.assertEqual(code, 0)
        self.assertEqual([request["owner"] for request in report["needs_owner"]], ["/w/classed"])

    def test_recorded_issue_no_longer_open_is_set_apart_as_a_closure_candidate(self) -> None:
        (self.root / "issue-repos.json").write_text(json.dumps({"repos": ["acme/app", "acme/lib"]}))
        app, lib = "https://github.com/acme/app/issues/", "https://github.com/acme/lib/issues/"
        opened = {"acme/app": [gh_issue(app, 1), gh_issue(app, 2)], "acme/lib": [gh_issue(lib, 3)]}
        with patch.object(hourly_state, "gh_open_issues", side_effect=opened.get), \
                patch.object(hourly_state, "orca_worktrees", return_value=[]):
            self.call("requests-needing-owner")

        def later(repo: str) -> list[dict]:
            if repo == "acme/lib":
                raise subprocess.CalledProcessError(1, ["gh", "issue", "list"])
            return [gh_issue(app, 1)]  # app#2 was closed

        with patch.object(hourly_state, "gh_open_issues", side_effect=later), \
                patch.object(hourly_state, "orca_worktrees", return_value=[]), \
                redirect_stderr(io.StringIO()) as err:
            code, report = self.call("requests-needing-owner")
            with patch.object(hourly_state, "ISSUE_LIMIT", 1):
                _, truncated = self.call("requests-needing-owner")

        self.assertEqual(code, 0)
        # acme/lib could not be read, so its request stays where it was.
        self.assertEqual([request["source"]["url"] for request in report["needs_owner"]], [app + "1", lib + "3"])
        self.assertEqual([request["source"]["url"] for request in report["closed_issues"]], [app + "2"])
        self.assertIn("acme/lib", err.getvalue())
        self.assertIsNone(self.call("show")[1]["requests"][report["closed_issues"][0]["id"]]["closed_at"])
        # A list cut at gh's limit may leave out open issues, so it closes nothing.
        self.assertEqual(truncated["closed_issues"], [])

    def test_issue_list_is_read_once_more_after_a_failure(self) -> None:
        (self.root / "issue-repos.json").write_text(json.dumps({"repos": ["acme/app", "acme/lib"]}))
        app, lib = "https://github.com/acme/app/issues/", "https://github.com/acme/lib/issues/"
        outcomes = {"acme/app": [subprocess.CalledProcessError(1, "gh"), [gh_issue(app, 1)]],
                    "acme/lib": [subprocess.TimeoutExpired("gh", 60), ValueError("bad JSON"), [gh_issue(lib, 2)]]}

        def gh(repo: str) -> list[dict]:
            outcome = outcomes[repo].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(hourly_state, "gh_open_issues", side_effect=gh), \
                patch.object(hourly_state, "orca_worktrees", return_value=[]), \
                redirect_stderr(io.StringIO()) as err:
            code, report = self.call("requests-needing-owner")

        self.assertEqual(code, 0)
        self.assertEqual([request["source"]["url"] for request in report["needs_owner"]], [app + "1"])
        # Each repository was asked twice; acme/lib's third answer was never requested.
        self.assertEqual(outcomes, {"acme/app": [], "acme/lib": [[gh_issue(lib, 2)]]})
        self.assertIn("acme/lib", err.getvalue())

    def test_unreachable_paired_mac_is_given_up_on_quickly(self) -> None:
        (self.root / "issue-repos.json").write_text(json.dumps({"repos": ["acme/app"]}))
        url = "https://github.com/acme/app/issues/"
        local = {"worktrees": [{"path": "/w/app", "repo": "app", "linkedIssue": 1, "linkedItems": [],
                                "linkedPR": {"number": 4, "url": "https://github.com/acme/app/pull/4"}}],
                 "hostScope": {"omittedHostSelectors": [
                     {"hostId": "remote-1", "selector": "--environment office-mac-mini"}]}}
        replies: dict[tuple[str, ...], dict] = {("worktree", "ps"): local, ("terminal", "list"): {"terminals": []},
                   ("environment", "list"): {"environments": []}}
        remote_waits = []

        def run(cmd: list[str], *, timeout: float, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 0, json.dumps([gh_issue(url, 1), gh_issue(url, 2)]), "")
            if "--environment" in cmd:
                remote_waits.append(timeout)
                raise subprocess.TimeoutExpired(cmd, timeout)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": replies[tuple(cmd[1:3])]}), "")

        with patch.object(hourly_state.subprocess, "run", side_effect=run), \
                redirect_stderr(io.StringIO()) as err:
            code, report = self.call("requests-needing-owner", "--orca", "orca", "--live-owner", "/w/app")

        self.assertEqual(code, 0)
        self.assertEqual([request["source"]["url"] for request in report["needs_owner"]], [url + "2"])
        self.assertIn("office-mac-mini", err.getvalue())
        self.assertTrue(remote_waits)
        self.assertLessEqual(max(remote_waits), 15)

    def test_request_commands_accept_existing_state_without_requests(self) -> None:
        self.state.write_text(json.dumps({"version": 1, "left": {}, "questions": {}, "handoffs": {}}))
        self.assertEqual(self.call("requests-needing-owner"), (0, NOTHING_OWED))
        self.assertIn("requests", self.call("show")[1])
        original = self.source("original.txt", "担当を立てて直してほしい")
        symptom = self.source("symptom.txt", "direnv が止まる")
        _, request = self.call("request-open", "--original-file", original, "--symptom-file", symptom)
        self.assertIsNone(request["owner"])
        self.assertEqual(self.call("requests-needing-owner")[1]["needs_owner"][0]["id"], request["id"])

    def test_same_path_on_another_mac_does_not_hide_an_unowned_request(self) -> None:
        original = self.source("original.txt", "動作が遅い")
        symptom = self.source("symptom.txt", "起動が止まる")
        local_owner = scan.row_identity({"path": "/w/shared", "environment": None})
        remote_owner = scan.row_identity({"path": "/w/shared", "environment": "macbook-pro-16"})
        _, local = self.call("request-open", "--original-file", original,
                             "--symptom-file", symptom, "--owner", local_owner)
        _, remote = self.call("request-open", "--original-file", original,
                              "--symptom-file", symptom,
                              "--owner", remote_owner)
        code, needs_owner = self.call("requests-needing-owner", "--live-owner", remote_owner)
        self.assertEqual(code, 0)
        self.assertEqual([request["id"] for request in needs_owner["needs_owner"]], [local["id"]])
        self.assertNotEqual(local["owner"], remote["owner"])

    def test_request_owner_requires_scan_row_identity(self) -> None:
        original = self.source("original.txt", "動作が遅い")
        symptom = self.source("symptom.txt", "起動が止まる")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = hourly_state.main(["--state-file", str(self.state), "request-open",
                                      "--original-file", original, "--symptom-file", symptom,
                                      "--owner", "skills/shared-lane"])
        self.assertEqual(code, 1)
        self.assertFalse(self.state.exists())

    def run_main(self, *args: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = hourly_state.main(["--state-file", str(self.state), *args])
        return code, out.getvalue(), err.getvalue()

    def gh(self, reply: Callable[[list[str]], tuple[int, object, str]]) -> AbstractContextManager[object]:
        """subprocess.run answering each gh command with reply(args) = (exit code, JSON stdout, stderr)."""
        real = subprocess.run

        def run(cmd: list[str], *, timeout: float, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[0] != "gh":
                return real(cmd, capture_output=True, text=True, timeout=timeout)
            code, out, err = reply(cmd[1:])
            return subprocess.CompletedProcess(cmd, code, "" if out is None else json.dumps(out), err)

        return patch.object(hourly_state.subprocess, "run", side_effect=run)

    def lane(self, origin: str = "git@github.com:acme/app.git") -> Path:
        lane = self.root / "lane"
        subprocess.run(["git", "init", "-q", str(lane)], check=True)
        subprocess.run(["git", "-C", str(lane), "remote", "add", "origin", origin], check=True)
        return lane

    def test_stale_asks_counts_an_answer_send_delivered_without_decision(self) -> None:
        # 2026-11-11: montly-report and tohoku-ui-consult stayed unanswered after send had delivered their answers.
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "repo": "app", "class": "waiting", "claude": {
            "last_user_at": "2026-11-11T01:00:00Z", "final_at": "2026-11-11T01:05:00Z", "final_text": "待ちます"}}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": "ab12cd34-0000-ask", "ts": "2026-11-11T10:02:00+0900",
                                                 "kind": "ask", "worktree": "lane", "repo": "app", "text": "公開しますか？"}])
        write_jsonl(self.root / "answers.jsonl", [{"id": "ab12cd34-0000-ask", "ts": "2026-11-11T10:30:00+09:00",
                                                   "how": "send", "note": "三浦さんの答え: 公開する"}])
        code, stale = self.call("stale-asks", "--from", str(scan_json))
        self.assertEqual(code, 0)
        self.assertEqual([(item["state"], item["answer"]["text"]) for item in stale],
                         [("stopped_after_answer", "三浦さんの答え: 公開する")])

    def test_a_withdrawn_and_reasked_question_is_named_in_pending_stale_asks_and_answered(self) -> None:
        # 2026-11-10: 8ddb8b28 was withdrawn and asked again as cbb319be; its old choices were put and answered.
        old, new = "8ddb8b28-0000-ask", "cbb319be-0000-ask"
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "repo": "app", "class": "waiting", "claude": {
            "last_user_at": "2026-11-10T13:00:00Z", "final_at": "2026-11-10T13:30:00Z", "final_text": "待ちます"}}]))
        write_jsonl(self.root / "inbox.jsonl", [
            {"id": old, "ts": "2026-11-10T22:05:00+0900", "kind": "ask", "worktree": "lane", "repo": "app",
             "text": "CI が通ったら merge しますか？"},
            {"id": new, "ts": "2026-11-10T22:25:00+0900", "kind": "ask", "worktree": "lane", "repo": "app",
             "text": "CI はありません。今の検証で merge しますか？", "withdraws": [old]}])
        write_jsonl(self.root / "answers.jsonl", [{"id": old, "ts": "2026-11-10T22:25:00+09:00",
                                                   "how": "triage-notify --withdraws", "note": "取り下げ"}])
        lines = self.root / "asked.jsonl"
        lines.write_text(json.dumps({"ask": old[:8], "decision": f"ask:app/lane::{old[:8]}:1",
                                     "question": "CI が通ったら merge しますか？"}) + "\n")
        _, (asked,) = self.call("asked-many", "--file", str(lines), "--from", str(scan_json))

        _, pending = self.call("pending")
        code, stale = self.call("stale-asks", "--from", str(scan_json))
        with redirect_stdout(io.StringIO()) as out:
            hourly_state.main(["--state-file", str(self.state), "stale-asks", "--from", str(scan_json), "--summary"])
        answer = self.source("answer.txt", "CI が通ったら進める")
        answered_code, answered, err = self.run_main("answered", "--id", asked["id"], "--answer-file", answer)

        self.assertEqual([reissue["id"] for reissue in pending["questions"][0]["reissued"]], [new])
        self.assertEqual((code, [(item["ask"]["id"], [reissue["id"] for reissue in item["reissued"]])
                                 for item in stale]), (0, [(old, [new])]))
        self.assertIn(f"出し直しあり（{new}）", out.getvalue())
        self.assertEqual(answered_code, 0)
        self.assertEqual(json.loads(answered)["reissued_by"], [{"id": new, "ts": "2026-11-10T22:25:00+0900",
                                                                "withdrew": [old]}])
        self.assertIn("取り下げて出し直しています", err)

    def test_asked_many_names_each_line_it_did_not_record_after_the_output_and_exits_2(self) -> None:
        # 2026-11-10: the line naming a closed #170 was not recorded, and tail hid it.
        lane = self.lane()
        lines = self.root / "asked.jsonl"
        lines.write_text("".join(json.dumps({"row": str(lane), "decision": f"close:{number}", "final_message": "どうしますか",
                                             "question": f"#{number} の値をどこに置きますか？"}) + "\n" for number in (10, 11)))
        nodes = {"item_10": {"__typename": "PullRequest", "state": "OPEN"},
                 "item_11": {"__typename": "Issue", "state": "CLOSED", "updatedAt": "2026-11-10T01:00:00Z"}}
        with patch.object(scan, "graphql_repository", return_value=nodes):
            code, out, err = self.run_main("asked-many", "--file", str(lines))
        self.assertEqual(code, 2)
        self.assertEqual([item.get("recorded", True) for item in json.loads(out)], [True, False])
        self.assertEqual(len(self.call("show")[1]["questions"]), 1)
        self.assertIn("記録しなかった: 行 2（問いの #番号がすべて閉じている: acme/app#11 CLOSED）", err)

    def test_pending_names_a_waiting_asked_question_no_record_accounts_for(self) -> None:
        # 2026-11-10 23:00: three questions were put and two recorded; the third waited on no list.
        put = ["REPORT-42 の承認ルート 8 本を devel に先に登録してよいですか？",
               "api-server が読む OAuth アプリの値を、どこに置いて運用の担当に渡しますか？",
               "web2api・job2api・cli2api の client_secret を作り直しますか？"]
        asked_at = "2026-11-10T14:09:22.522Z"
        write_jsonl(self.root / "claude/projects/-w-triage/triage-run.jsonl", [{
            **assistant({"type": "tool_use", "name": "AskUserQuestion", "id": "toolu_9",
                         "input": {"questions": [{"question": question} for question in put]}}),
            "timestamp": asked_at}])
        lines = self.root / "asked.jsonl"
        lines.write_text("".join(json.dumps({"row": "/w/lane", "decision": f"d{index}", "final_message": "伺います",
                                             "question": question, "asked_at": "2026-11-10T14:09:12+00:00"}) + "\n"
                                 for index, question in enumerate(["REPORT-42: devel に承認ルート 8 本を先に登録するか",
                                                                   "web2api・job2api・cli2api の client_secret を作り直すか"])))
        self.call("asked-many", "--file", str(lines))

        _, pending = self.call("pending")
        self.assertEqual([(item["question"], item["since"]) for item in pending["unrecorded_questions"]],
                         [(put[1], asked_at)])
        # Recorded at the time it was put, it is accounted for.
        lines.write_text(json.dumps({"row": "/w/lane", "decision": "d2", "final_message": "伺います",
                                     "question": "OAuth アプリの値の置き場所", "asked_at": asked_at}) + "\n")
        self.call("asked-many", "--file", str(lines))
        self.assertEqual(self.call("pending")[1]["unrecorded_questions"], [])

    def test_a_record_that_accounts_for_an_earlier_call_does_not_hide_a_later_unrecorded_one(self) -> None:
        # PR #176 review: a question recorded and answered at 01:01 paired with an unrecorded one put at 01:05.
        transcript = self.root / "claude/projects/-w-triage/triage-run.jsonl"
        write_jsonl(transcript, [
            {**assistant({"type": "tool_use", "name": "AskUserQuestion", "id": "toolu_1",
                          "input": {"questions": [{"question": "REPORT-42 を devel に登録しますか？"}]}}),
             "timestamp": "2026-11-10T01:01:00.000Z"},
            {**user([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "登録する"}]),
             "timestamp": "2026-11-10T01:02:00.000Z"},
            {**assistant({"type": "tool_use", "name": "AskUserQuestion", "id": "toolu_2",
                          "input": {"questions": [{"question": "API の鍵を作り直しますか？"}]}}),
             "timestamp": "2026-11-10T01:05:00.000Z"}])
        lines = self.root / "asked.jsonl"
        lines.write_text(json.dumps({"row": "/w/lane", "decision": "d1", "final_message": "伺います",
                                     "question": "REPORT-42: devel に登録するか",
                                     "asked_at": "2026-11-10T01:00:50+00:00"}) + "\n")
        self.call("asked-many", "--file", str(lines))

        self.assertEqual([item["question"] for item in self.call("pending")[1]["unrecorded_questions"]],
                         ["API の鍵を作り直しますか？"])

    def test_a_workspace_with_no_record_yet_is_read_for_unrecorded_questions(self) -> None:
        # PR #176 review: with no question recorded, a run that recorded none of its questions showed nothing, and a
        # relative --asked-in was read as no workspace.
        here = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        projects = self.root / "claude/projects"
        for workspace, session, asked in (("/w/triage", "old-run", "#5 の今夜の実行を止めますか？"),
                                          (str(Path.cwd() / "lead"), "lead-run", "PR を閉じますか？")):
            write_jsonl(scan.claude_project_dir(projects, workspace) / f"{session}.jsonl", [{
                **assistant({"type": "tool_use", "name": "AskUserQuestion", "id": f"toolu_{len(asked)}",
                             "input": {"questions": [{"question": asked}]}}),
                "timestamp": "2026-11-10T01:05:00.000Z"}])

        _, pending = self.call("pending", "--asked-in", "lead")
        self.assertEqual(sorted(item["question"] for item in pending["unrecorded_questions"]),
                         ["#5 の今夜の実行を止めますか？", "PR を閉じますか？"])
        # PR #176 review: a later run records it with that call's session and time, and it is accounted for, even
        # once the PR it names has closed.
        (unrecorded,) = [item for item in pending["unrecorded_questions"] if item["workspace"] == "/w/triage"]
        question = self.source("question.txt", unrecorded["question"])
        closed = {"item_5": {"__typename": "PullRequest", "state": "CLOSED", "headRefOid": "c5"}}
        with patch.object(scan, "graphql_repository", return_value=closed):
            _, recorded = self.call("asked", "--row", str(self.lane()), "--decision", "stop:tonight",
                                    "--final-message-file", question, "--question-file", question,
                                    "--asked-at", unrecorded["since"], "--session", unrecorded["session"])
        self.assertEqual(recorded["session"], unrecorded["session"])
        (left,) = self.call("pending", "--asked-in", "lead")[1]["unrecorded_questions"]
        self.assertEqual(left["question"], "PR を閉じますか？")
        # Recorded here, a question the lead's session put is accounted for by its session.
        self.call("asked", "--row", "/w/lane", "--decision", "close:pr", "--final-message-file", question,
                  "--question-file", self.source("lead.txt", left["question"]), "--asked-at", left["since"],
                  "--session", left["session"])
        self.assertEqual(self.call("pending", "--asked-in", "lead")[1]["unrecorded_questions"], [])

    def test_an_ordinary_word_before_a_number_leaves_it_in_the_rows_repository(self) -> None:
        # PR #176 review: in "merge #10", merge was read as a repository, so #10 went unread.
        lane = self.lane()
        question = self.source("question.txt", "merge #10 と cvm #12 を進めますか？")

        def graphql(slug: str, query: str) -> dict:
            return {"item_10": {"__typename": "PullRequest", "state": "OPEN", "headRefOid": slug}}

        with self.gh(lambda args: (0, [{"name": "app"}, {"name": "web-cvm-server"}], "")), \
                patch.object(scan, "graphql_repository", side_effect=graphql):
            self.call("asked", "--row", str(lane), "--decision", "merge:#10", "--final-message-file", question,
                      "--question-file", question)
            _, pending = self.call("pending")
        # cvm names a part of web-cvm-server, so its #12 stays unread rather than read as the row's own.
        self.assertEqual([(pr["repo"], pr["number"], pr["state"]) for pr in pending["questions"][0]["prs"]],
                         [("acme/app", 10, "OPEN"), (None, 12, None)])

    def test_a_repository_named_before_a_number_is_read_in_the_rows_organization(self) -> None:
        # 2026-11-11: infra-aws#526 was read as the row's own #526, merged in May.
        lane = self.lane()
        question = self.source("question.txt", "infra-aws#526 を merge しますか？")

        def graphql(slug: str, query: str) -> dict:
            return {"item_526": {"__typename": "PullRequest", "state": "OPEN" if slug == "acme/infra-aws" else "CLOSED",
                                 "headRefOid": slug}}

        with self.gh(lambda args: (0, [{"name": "app"}, {"name": "infra-aws"}], "")), \
                patch.object(scan, "graphql_repository", side_effect=graphql):
            _, asked = self.call("asked", "--row", str(lane), "--decision", "merge:infra-aws#526",
                                 "--final-message-file", question, "--question-file", question)
            _, pending = self.call("pending")
        self.assertIsNone(asked["answer"])
        self.assertEqual([(pr["repo"], pr["number"], pr["state"]) for pr in pending["questions"][0]["prs"]],
                         [("acme/infra-aws", 526, "OPEN")])

    def test_asked_names_prs_merged_after_the_ask_for_an_issue_its_question_names(self) -> None:
        # 2026-11-11: a question rested on an issue comment that a PR merged the same day had made untrue.
        scan_json = self.root / "scan.json"
        scan_json.write_text(json.dumps([{"path": "/w/lane", "repo": "app", "class": "waiting"}]))
        write_jsonl(self.root / "inbox.jsonl", [{"id": "c1170000-0000-ask", "kind": "ask", "worktree": "lane",
                                                 "repo": "app", "ts": "2026-11-10T10:00:00+0900", "text": "公開しますか？"}])
        lines = self.root / "asked.jsonl"
        lines.write_text(json.dumps({"ask": "c1170000", "decision": "publish:acme/app#1170",
                                     "question": "acme/app#1170 の Web 検索を公開しますか？"}) + "\n")

        def pr(number: int, merged_at: str) -> dict:
            return {"number": number, "title": f"PR {number}", "url": f"https://github.com/acme/app/pull/{number}",
                    "mergedAt": merged_at, "repository": {"nameWithOwner": "acme/app"}}

        def graphql(slug: str, query: str) -> dict:
            if "issueOrPullRequest" in query:
                return {"item_1170": {"__typename": "Issue", "state": "OPEN"}}
            return {"issue_1170": {"closedByPullRequestsReferences": {"nodes": [pr(1216, "2026-11-10T06:00:00Z")]},
                                   "timelineItems": {"nodes": [{"source": pr(1180, "2026-11-09T00:00:00Z")}, {}]}}}

        with patch.object(scan, "graphql_repository", side_effect=graphql):
            code, out, err = self.run_main("asked-many", "--file", str(lines), "--from", str(scan_json))
        self.assertEqual(code, 0)
        (recorded,) = json.loads(out)
        self.assertEqual([(found["issue"], [item["pr"] for item in found["prs"]]) for found in recorded["merged_after_basis"]],
                         [("acme/app#1170", ["acme/app#1216"])])
        self.assertIn("acme/app#1216 2026-11-10T06:00:00Z", err)

    def test_user_wait_is_read_now_by_how_it_is_checked_and_an_unreadable_repository_says_why(self) -> None:
        # 2026-11-10: a guessed repository name and 2>/dev/null printed 0 secrets for a repository that held one.
        text = self.source("wait.txt", "LiveKit の CLA に署名し、SHARED_PAT を登録する")
        self.assertEqual(self.run_main("user-wait", "--text-file", text)[0], 1)
        code, wait = self.call("user-wait", "--text-file", text, "--started-at", "2026-11-10T11:06:00+00:00",
                               "--pr-check", "livekit/agents#3911", "license/cla",
                               "--secret", "SHARED_PAT", "acme/users", "acme/ffs", "acme/gone")
        self.assertEqual(code, 0)
        (self.root / "inbox.jsonl").write_text("")
        scan_json = self.root / "scan.json"
        scan_json.write_text("[]")

        def gh(args: list[str]) -> tuple[int, object, str]:
            if args[:2] == ["pr", "checks"]:
                return 8, [{"name": "license/cla", "bucket": "pending"}, {"name": "tests", "bucket": "pass"}], ""
            repo = args[args.index("-R") + 1]
            if repo == "acme/gone":
                return 1, None, "failed to get secrets: HTTP 404: Not Found"
            return 0, [{"name": "SHARED_PAT"}] if repo == "acme/users" else [], ""

        with self.gh(gh):
            _, stale = self.call("stale-asks", "--from", str(scan_json))
            _, summary_line, _ = self.run_main("stale-asks", "--from", str(scan_json), "--summary")
        self.assertEqual([(item["state"], item["id"], item["checks_done"]) for item in stale],
                         [("user_wait", wait["id"], False)])
        self.assertIn("livekit/agents#3911 license/cla pending; SHARED_PAT: acme/users 有り, acme/ffs 無し, "
                      "acme/gone 読めない（failed to get secrets: HTTP 404: Not Found）", summary_line)
        self.assertEqual([item["id"] for item in self.call("pending")[1]["user_waits"]], [wait["id"]])
        evidence = self.source("evidence.txt", "license/cla が pass、3 つとも登録を確かめた")
        self.call("user-wait-close", "--id", wait["id"], "--evidence-file", evidence)
        with self.gh(gh):
            self.assertEqual(self.call("stale-asks", "--from", str(scan_json))[1], [])


class PrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ps = Path(self.tmp.name) / "ps.json"
        self.runs = Path(self.tmp.name) / "runs.json"

    def run_precheck(self, worktrees: list[dict], runs: Sequence[dict] = (), workspace: str = "/w/triage", truncated: bool = False) -> tuple[int, str]:
        self.ps.write_text(json.dumps({"result": {"worktrees": worktrees, "truncated": truncated}}))
        self.runs.write_text(json.dumps({"result": {"runs": runs}}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = precheck.main(["--ps-json", str(self.ps), "--runs-json", str(self.runs),
                                  "--workspace", workspace])
        return code, out.getvalue()

    def test_a_working_turn_holds_the_next_run(self) -> None:
        for turn in ("working", "done"):
            with self.subTest(turn=turn):
                row = worktree("/w/triage", "working", turn=turn)
                row["agents"][0]["paneKey"] = "scheduled-pane"
                code, out = self.run_precheck([row], [{"status": "dispatched", "trigger": "scheduled",
                                                      "terminalPaneKey": "scheduled-pane",
                                                      "workspaceId": "repo::/w/triage"}])
                self.assertEqual(code, 1)
                self.assertIn("skip: a scheduled turn in /w/triage is working", out)

    def test_completed_scheduled_run_with_working_human_followup_allows_next_run(self) -> None:
        row = worktree("/w/triage", "working")
        row["agents"][0]["paneKey"] = "old-pane"
        row["agents"][0]["mainAgent"] = {"state": "working"}
        runs = [{"status": "completed", "trigger": "scheduled", "terminalPaneKey": "old-pane",
                 "workspaceId": "repo::/w/triage"}]
        code, out = self.run_precheck([row], runs)
        self.assertEqual(code, 0)
        self.assertIn("run: start a fresh session", out)

    def test_background_job_from_completed_run_still_blocks(self) -> None:
        row = worktree("/w/triage", "working", turn="done")
        row["agents"][0]["paneKey"] = "old-pane"
        runs = [{"status": "completed", "trigger": "scheduled", "terminalPaneKey": "old-pane",
                 "workspaceId": "repo::/w/triage"}]
        self.assertEqual(self.run_precheck([row], runs)[0], 1)

    def test_working_agent_without_pane_identity_is_unverifiable(self) -> None:
        row = worktree("/w/triage", "working")
        code, out = self.run_precheck([row], [{"status": "completed", "trigger": "scheduled",
                                              "terminalPaneKey": "old-pane",
                                              "workspaceId": "repo::/w/triage"}])
        self.assertEqual(code, 2)
        self.assertIn("could not identify", out)

    def test_waiting_blocked_and_done_turns_allow_a_fresh_session(self) -> None:
        for state in ("waiting", "blocked", "done"):
            with self.subTest(state=state):
                code, out = self.run_precheck([worktree("/w/triage", state),
                                               worktree("/w/lane", "working")])
                self.assertEqual(code, 0)
                self.assertIn("run: start a fresh session", out)

    def test_matches_a_symlinked_workspace(self) -> None:
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(real)
        code, _ = self.run_precheck([worktree(str(link), "waiting")], workspace=str(real))
        self.assertEqual(code, 0)

    def test_requires_a_known_workspace(self) -> None:
        code, out = self.run_precheck([worktree("/w/lane", "done")])
        self.assertEqual(code, 2)
        self.assertIn("not an Orca worktree", out)
        code, out = self.run_precheck([worktree("/w/lane", "done")], truncated=True)
        self.assertEqual(code, 2)
        self.assertIn("before cutting the list short", out)

    def test_failure_to_read_orca_skips_the_run(self) -> None:
        refusing = Path(self.tmp.name) / "refusing-orca"
        refusing.write_text("#!/bin/sh\necho 'runtime not reachable' >&2\nexit 3\n")
        refusing.chmod(0o755)
        out = io.StringIO()
        with redirect_stdout(out):
            code = precheck.main(["--orca", str(refusing), "--workspace", "/w/triage"])
        self.assertEqual(code, 2)
        self.assertIn("runtime not reachable", out.getvalue())


class HourlyCleanupCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.transcript = self.root / "old-session.jsonl"
        write_jsonl(self.transcript, [{**assistant(text("終了しました")), "timestamp": "2026-10-26T08:00:00Z"}])
        self.state = self.root / "state.json"
        self.status = self.root / "status.json"
        self.status.write_text(json.dumps({"entries": {"old:leaf": {
            "worktreeId": "repo::/w/triage", "providerSession": {
                "id": "old-session", "transcriptPath": str(self.transcript)}}}}))
        self.ps_failures = 0
        self.sent = []
        self.closed = []
        self.response_started = True
        self.response_active = False
        self.screen = {"source": "screen", "tail": ["────────", "❯", "────────"]}

    def test_cleanup_dismisses_only_a_selector_whose_entire_batch_is_recorded_settled(self) -> None:
        questions = ["公開しますか？", "確認を終えましたか？"]
        for settled, extra, started, active in ((True, False, True, False), (False, False, True, False),
                                               (True, True, True, False), (True, False, False, False),
                                               (True, False, True, True)):
            expect_close = settled and not extra and started and not active
            with self.subTest(settled=settled, unrecorded=extra, started=started, active=active):
                self.closed.clear()
                self.sent.clear()
                self.response_started, self.response_active = started, active
                write_jsonl(self.transcript, [{**assistant({"type": "tool_use", "name": "AskUserQuestion",
                    "id": "toolu_1", "input": {"questions": [{"question": q} for q in questions]}}),
                    "timestamp": "2026-10-26T08:00:00Z"}])
                records = {"q1": {"owner_pane": "old:leaf", "session": "old-session", "asked_in": "/w/triage",
                    "asked_at": "2026-10-26T07:59:00Z", "question": questions[0], "answer": "はい"},
                    "q2": {"owner_pane": "old:leaf", "session": "old-session", "asked_in": "/w/triage",
                    "asked_at": "2026-10-26T07:59:00Z", "question": questions[1], "answer": None,
                    "withdrawn_at": "2026-10-26T08:01:00Z" if settled else None}}
                if extra:
                    records.pop("q2")
                self.state.write_text(json.dumps({"questions": records}))
                self.screen = {"source": "screen", "tail": ["────────", questions[0], "❯ 1. はい", "2. いいえ",
                                                                  "Enter to select · ↑/↓ to navigate · Esc to cancel"]}
                code, shown = self.call()
                self.assertEqual(code, 0)
                self.assertEqual(bool(self.closed), expect_close, shown)
                self.assertEqual(bool(self.sent), settled and not extra)
                if expect_close:
                    self.assertEqual(self.sent[0], ["terminal", "send", "--terminal", "old", "--text", "\x1b"])
                    self.assertIn("別のタブで答え済み", self.sent[1])
                    self.assertIn("closed:", shown)

    def orca(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[1:-1]
        if command == ["automations", "list"]:
            result = {"automations": [triage_automation("hourly", "/w/triage")]}
        elif command == ["automations", "runs", "--id", "hourly"]:
            result = {"runs": [{"status": "dispatched", "trigger": "scheduled", "terminalPaneKey": "now:leaf",
                       "workspaceId": "repo::/w/triage", "startedAt": 2},
                      {"status": "completed", "trigger": "scheduled", "terminalPaneKey": "old:leaf",
                       "workspaceId": "repo::/w/triage", "startedAt": 1,
                       "usage": {"providerSessionId": "old-session"}}]}
        elif command == ["terminal", "list"]:
            result = {"terminals": [{"worktreeId": "repo::/w/triage", "tabId": name, "leafId": "leaf",
                       "handle": name, "connected": True} for name in ("now", "old")]}
        elif command == ["worktree", "ps"]:
            if self.ps_failures:
                self.ps_failures -= 1
                raise subprocess.CalledProcessError(2, args, stderr="Orca runtime is reconnecting")
            state = "working" if self.response_active and len(self.sent) >= 2 else "done"
            result = {"worktrees": [{"path": "/w/triage", "agents": [{"paneKey": "old:leaf", "state": state,
                       "mainAgent": {"state": state}}]}]}
        elif command[:2] == ["terminal", "read"]:
            result = self.screen
        elif command[:2] == ["terminal", "send"]:
            self.sent.append(command)
            result = {"prompt": {"stages": ["input_accepted"] + (["turn_started"] if self.response_started else [])}}
            self.screen = {"source": "screen", "tail": ["────────", "❯", "────────"]}
            if "--enter" in command:
                with self.transcript.open("a") as stream:
                    stream.write(json.dumps(tool_result("answered elsewhere", {})) + "\n")
                    stream.write(json.dumps({**assistant(text("終了しました")),
                                             "timestamp": "2026-10-26T08:10:00Z"}) + "\n")
        elif command[:2] == ["terminal", "wait"]:
            result = {"completed": True}
        elif command[:2] == ["terminal", "close"]:
            self.closed.append(command)
            result = {}
        else:
            self.fail(f"unexpected Orca call: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps({"result": result}))

    def call(self) -> tuple[int, str]:
        with patch("subprocess.run", side_effect=self.orca), redirect_stdout(io.StringIO()) as out:
            code = cleanup_hourly.main(["--workspace", "/w/triage", "--orca", "fixture-orca",
                "--state-file", str(self.state), "--agent-status-file", str(self.status)])
        return code, out.getvalue()

    def test_cleanup_retries_worktree_ps_once_and_keeps_orca_stderr_on_failure(self) -> None:
        with patch("time.sleep") as sleep:
            self.ps_failures = 1
            code, shown = self.call()
            self.assertEqual(code, 0)
            self.assertEqual(len(self.closed), 1)
            sleep.assert_called_once_with(2)
            self.closed.clear()
            self.ps_failures = 2
            code, shown = self.call()
        self.assertEqual((code, self.closed), (1, []))
        self.assertIn("keep:", shown)
        self.assertIn("Orca runtime is reconnecting", shown)


class HourlyReadTests(unittest.TestCase):
    def test_parallel_reads_keep_outputs_and_report_each_failure_in_a_unique_run_directory(self) -> None:
        hourly_read = load_script("hourly_read")
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            initial = Barrier(10, timeout=5)
            seen = []
            def run(args: list[str], *, stdout: TextIO, stderr: TextIO,
                    text: bool, timeout: float) -> subprocess.CompletedProcess[str]:
                seen.append(args)
                stale = "stale-asks" in args
                if not stale:
                    initial.wait()
                if Path(args[0]).name == "topic":
                    stderr.write("topic unavailable\n")
                    return subprocess.CompletedProcess(args, 7)
                if Path(args[0]).name == "backlog-watch":
                    raise subprocess.TimeoutExpired(args, 30)
                if "--out" in args:
                    Path(args[args.index("--out") + 1]).write_text("[]")
                elif Path(args[0]).name == "asana99_status.py":
                    Path(args[1]).write_text("[]")
                else:
                    stdout.write("[]\n")
                if "--review-out" in args:
                    Path(args[args.index("--review-out") + 1]).write_text(json.dumps({"rows": [{"path": "/w/a"}]}))
                return subprocess.CompletedProcess(args, 0)
            with patch("subprocess.run", side_effect=run), redirect_stdout(io.StringIO()) as out:
                code = hourly_read.main(["--workspace", "/w/triage", "--output-root", root])
            self.assertEqual(code, 1)
            [run_dir] = directory.iterdir()
            result = json.loads((run_dir / "results.json").read_text())
            self.assertEqual(len(seen), 11)
            self.assertEqual({item["name"]: item["exit_code"] for item in result}, {
                "cleanup-hourly-tabs": 0, "lead-state": 0, "open-asks": 0, "unanswered": 0,
                "pending": 0, "handoff-last": 0, "scan": 0, "backlog-watch": 124,
                "asana99-status": 0, "topic-check": 7, "stale-asks": 0})
            self.assertEqual(json.loads((run_dir / "scan.json").read_text()), [])
            self.assertEqual(json.loads((run_dir / "lead-state.json").read_text()), [])
            self.assertEqual(json.loads((run_dir / "asana99-status.json").read_text()), [])
            self.assertIn("topic unavailable", (run_dir / "topic-check.stderr").read_text())
            self.assertIn("topic-check: exit 7", out.getvalue())
            self.assertIn(str(run_dir), out.getvalue())
            self.assertIn(f"review queue: {run_dir.resolve() / 'review-queue.json'}（1 行）／分類: orca-row-classify {run_dir.resolve()}",
                          out.getvalue())


class HourlyTabCleanupTests(unittest.TestCase):
    def test_unmatched_workspace_tabs_are_kept_with_a_reason(self) -> None:
        terms = [{"worktreeId": "repo::/w/triage", "tabId": name, "leafId": "leaf",
                  "handle": name, "connected": True, "orphaned": orphaned}
                 for name, orphaned in (("restored", False), ("detached", True))]
        terms.append({**terms[0], "handle": "elsewhere", "worktreeId": "repo::/w/other"})
        with patch.object(cleanup_hourly.hourly_runtime, "scheduled_runs", return_value=[]), \
                patch.object(cleanup_hourly.hourly_runtime, "orca_result", side_effect=[
                    {"terminals": terms}, {"worktrees": [{"path": "/w/triage", "agents": []}]}]) as calls, \
                redirect_stdout(io.StringIO()) as out:
            code = cleanup_hourly.main(["--workspace", "/w/triage", "--agent-status-file", "/missing/status.json"])
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for handle, line in zip(("restored", "detached"), lines):
            self.assertIn(f"keep: {handle}", line)
            self.assertIn("run の記録に無い", line)
            self.assertIn("scheduled=-, started=-", line)
        self.assertEqual(calls.call_count, 2)  # no screen read, send, or close without a run

    def test_rendered_empty_composer_overrides_an_old_orca_draft(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transcript = Path(root) / "session.jsonl"
            write_jsonl(transcript, [{**assistant(text("終了しました")), "timestamp": "2026-10-26T08:00:00Z"}])
            now = datetime(2026, 10, 26, 9, tzinfo=timezone.utc)
            screen = {"source": "screen", "draft": "古い提案", "tail": ["────────", "❯", "────────"]}
            self.assertEqual(cleanup_hourly.close_blockers({}, transcript, screen, 5, now), [])
            screen["tail"] = ["────────", "❯ まだ送っていない文", "────────"]
            blockers = cleanup_hourly.close_blockers({}, transcript, screen, 5, now)
            self.assertTrue(any("draft_text='古い提案'" in reason and "composer_empty=False" in reason
                                for reason in blockers))

    def test_detached_scheduled_tabs_match_recorded_panes_and_transcripts(self) -> None:
        for run_pane, run_status in (("old:leaf", "completed"), (None, "completed"),
                                     ("old:leaf", "dispatch_failed"), (None, "dispatch_failed")):
            for final in ("作業を終えました。", "どちらの案にしますか？"):
                with self.subTest(run_pane=run_pane, final=final), tempfile.TemporaryDirectory() as root:
                    home = Path(root)
                    transcript = home / "profile/projects/-w-triage/session-1.jsonl"
                    write_jsonl(transcript, [
                        {**user("/orca-session-triage hourly"), "timestamp": "2026-10-26T09:00:10Z"},
                        {**assistant(text(final)), "timestamp": "2026-10-26T09:02:00Z"}])
                    runs = [{"status": "dispatched", "trigger": "scheduled", "terminalPaneKey": "current:leaf",
                             "workspaceId": "repo::/w/triage", "startedAt": 1793008800000},
                            {"status": run_status, "trigger": "scheduled", "terminalPaneKey": run_pane,
                             "workspaceId": "repo::/w/triage", "scheduledFor": 1793005140000,
                             "startedAt": 1793005210000}]
                    terms = [{"worktreeId": "repo::/w/triage", "tabId": "current", "leafId": "leaf",
                              "handle": "current-handle", "connected": True},
                             {"worktreeId": "repo::/w/triage", "tabId": "pty:old", "leafId": "pty:old",
                              "recordedPaneKey": "old:leaf", "handle": "old-handle",
                              "connected": True, "orphaned": True, "title": "Claude Code"},
                             {"worktreeId": "repo::/w/triage", "tabId": "pty:other", "leafId": "pty:other",
                              "recordedPaneKey": "other:leaf", "handle": "other-handle",
                              "connected": True, "orphaned": True}]
                    status = home / "last-status.json"
                    status.write_text(json.dumps({"version": 2, "entries": {"old:leaf": {
                        "worktreeId": "repo::/w/triage", "paneKey": "old:leaf",
                        "providerSession": {"id": "session-1", "transcriptPath": str(transcript)},
                        "payload": {"state": "done", "mainAgent": {"state": "done"}}}}}))

                    def orca(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                        args = command[1:-1]
                        if args == ["automations", "list"]:
                            result = {"automations": [triage_automation("a1", "/w/triage")]}
                        elif args == ["automations", "runs", "--id", "a1"]:
                            result = {"runs": runs}
                        elif args == ["terminal", "list"]:
                            result = {"terminals": terms}
                        elif args == ["worktree", "ps"]:
                            result = {"worktrees": [{"path": "/w/triage", "agents": []}]}
                        elif args == ["terminal", "read", "--terminal", "old-handle", "--screen"]:
                            result = {"source": "screen", "tail": ["finished", "❯"]}
                        elif args == ["terminal", "close", "--terminal", "old-handle"]:
                            result = {}
                        else:
                            self.fail(f"unexpected Orca call: {command}")
                        return subprocess.CompletedProcess(command, 0, json.dumps({"result": result}))

                    with patch.object(cleanup_hourly.subprocess, "run", side_effect=orca) as calls, \
                            redirect_stdout(io.StringIO()) as out:
                        code = cleanup_hourly.main(["--workspace", "/w/triage", "--orca", "test-orca",
                                                    "--agent-status-file", str(status),
                                                    "--state-file", str(home / "state.json")])
                    self.assertEqual(code, 0)
                    closed = [call.args[0] for call in calls.call_args_list if call.args[0][1:3] ==
                              ["terminal", "close"]]
                    self.assertEqual(closed, [
                        ["test-orca", "terminal", "close", "--terminal", "old-handle", "--json"]])
                    lines = out.getvalue().splitlines()
                    carried = ["carry: old-handle: 2026-10-26T09:02:00Z [前日以前]: どちらの案にしますか？"] if "？" in final else []
                    self.assertEqual(lines[:len(carried)], carried)
                    self.assertIn("closed: old-handle", lines[len(carried)])
                    self.assertIn(f"run {run_status}", lines[len(carried)])
                    due = datetime.fromtimestamp(1793005140).strftime("%m/%d %H:%M")
                    started = datetime.fromtimestamp(1793005210).strftime("%m/%d %H:%M")
                    self.assertIn(f"scheduled={due}, started={started}", lines[len(carried)])
                    self.assertIn("keep: other-handle", lines[-1])
                    self.assertIn("run の記録に無い", lines[-1])
                    self.assertEqual(len(lines), len(carried) + 2)

    def test_transcript_lookup_finds_unique_scheduled_start_without_usage(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            project = home / ".claude/projects/-w-triage"
            project.mkdir(parents=True)
            target = project / "session-1.jsonl"
            target.write_text(json.dumps({"type": "user", "timestamp": "2026-10-26T09:00:10Z",
                                          "message": {"content": "<command-name>/orca-session-triage</command-name>"
                                                  "<command-args>hourly report</command-args>"}}) + "\n")
            (project / "older.jsonl").write_text(json.dumps({"type": "user",
                "timestamp": "2026-10-26T08:00:10Z",
                "message": {"content": "/orca-session-triage hourly"}}) + "\n")
            run = {"workspaceId": "repo::/w/triage", "startedAt": 1793005200000,
                   "usage": {"providerSessionId": None}}
            self.assertEqual(cleanup_hourly.transcript_for(run, home), target)
            (project / "collision.jsonl").write_text(target.read_text())
            self.assertIsNone(cleanup_hourly.transcript_for(run, home))

    def test_only_completed_idle_older_scheduled_tab_is_a_candidate(self) -> None:
        runs: list[dict[str, object]] = [{"status": "dispatched", "terminalPaneKey": "current"},
                                      {"status": "completed", "terminalPaneKey": "old"}]
        terms = [{"worktreeId": "repo::/w/triage", "tabId": "old", "leafId": "leaf",
                  "handle": "old-handle", "connected": True, "orphaned": False}]
        runs[1]["terminalPaneKey"] = "old:leaf"
        agents = [{"paneKey": "old:leaf", "state": "done", "mainAgent": {"state": "done"}}]
        self.assertEqual([term["handle"] for _, term in cleanup_hourly.prior_tabs(
            runs, terms, agents, "/w/triage", "current")], ["old-handle"])
        agents[0]["state"] = "working"  # a background job still owns the old tab
        runs[1].update(scheduledFor=1793005140000, startedAt=1793005210000)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(list(cleanup_hourly.prior_tabs(runs, terms, agents, "/w/triage", "current")), [])
        self.assertIn("keep: old-handle", out.getvalue())
        due = datetime.fromtimestamp(1793005140).strftime("%m/%d %H:%M")
        started = datetime.fromtimestamp(1793005210).strftime("%m/%d %H:%M")
        self.assertIn(f"scheduled={due}, started={started}", out.getvalue())
        agents[0]["state"] = "done"
        runs[1]["status"] = "dispatched"
        self.assertEqual(list(cleanup_hourly.prior_tabs(runs, terms, agents, "/w/triage", "current")), [])

    def test_pending_shared_question_keeps_previous_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "hourly-state.json"
            state.write_text(json.dumps({"questions": {"q1": {"answer": None,
                                                            "owner_pane": "old-pane"}}}))
            self.assertFalse(cleanup_hourly.no_pending_questions(state, "old-pane"))
            self.assertTrue(cleanup_hourly.no_pending_questions(state, "another-pane"))
            state.write_text(json.dumps({"questions": {"q1": {"answer": None}}}))
            self.assertFalse(cleanup_hourly.no_pending_questions(state, "another-pane"))
            state.write_text(json.dumps({"questions": {"q1": {"answer": "yes"}}}))
            self.assertTrue(cleanup_hourly.no_pending_questions(state, "old-pane"))

    def test_rendered_screen_and_draft_must_be_safe_to_close(self) -> None:
        from datetime import timezone
        with tempfile.TemporaryDirectory() as root:
            transcript = Path(root) / "session.jsonl"
            transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "作業を終えました。"}]},
                "timestamp": "2026-10-26T08:00:00Z"}) + "\n")
            now = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
            term = {"title": "Claude Code"}
            for source, tail, draft, expected in (("screen", ["finished", "❯"], None, False),
                                                  ("screen", ["finished", "❯"], "unsent reply", True),
                                                  ("stream", ["finished", "❯"], None, True),
                                                  ("screen", ["finished", "❯ draft reply"], None, True),
                                                  ("screen", ["finished"], None, True),
                                                  ("screen", ["Switch to gpt-6-luna?", "  2. Keep current model",
                                                              "  enter select · esc back",
                                                              "› Ask Codex to do anything"], None, True)):
                with self.subTest(source=source, tail=tail, draft=draft):
                    blockers = cleanup_hourly.close_blockers(term, transcript,
                        {"source": source, "tail": tail, "draft": draft}, 5, now)
                    self.assertEqual(bool(blockers), expected)
            self.assertIn("recent or unknown transcript activity", cleanup_hourly.close_blockers(
                term, transcript, {"source": "screen", "tail": ["❯"]}, 5,
                datetime(2026, 10, 26, 8, 3, tzinfo=timezone.utc)))
            with transcript.open("a") as stream:
                stream.write(json.dumps({"type": "user", "message": {"content": "前の話の続きです"},
                                         "timestamp": "2026-10-26T08:01:00Z"}) + "\n")
                stream.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "どちらの案にしますか。\n判断に必要な背景は前段の説明です。"}]},
                    "timestamp": "2026-10-26T08:02:00Z"}) + "\n")
            self.assertEqual(cleanup_hourly.close_blockers(
                term, transcript, {"source": "screen", "tail": ["❯"]}, 5, now), [])
            with transcript.open("a") as stream:
                stream.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion", "input": {}}]},
                    "timestamp": "2026-10-26T08:02:30Z"}) + "\n")
            self.assertIn("unanswered conversation question", cleanup_hourly.close_blockers(
                term, transcript, {"source": "screen", "tail": ["❯"]}, 5, now))
            with transcript.open("a") as stream:
                stream.write(json.dumps({"type": "user", "message": {"content": "Aでお願いします"},
                                         "timestamp": "2026-10-26T08:03:00Z"}) + "\n")
                stream.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "Aを適用し、作業を終えました。"}]},
                    "timestamp": "2026-10-26T08:04:00Z"}) + "\n")
            self.assertEqual(cleanup_hourly.close_blockers(
                term, transcript, {"source": "screen", "tail": ["❯"]}, 5, now), [])


def triage_automation(automation_id: str, workspace: str, prompt: str = "/orca-session-triage hourly") -> dict:
    return {"id": automation_id, "prompt": prompt, "workspaceId": "repo1::" + workspace}


def automation_run(status: str, started: str) -> dict:
    return {"status": status, "startedAt": int(datetime.fromisoformat(started).timestamp() * 1000)}


def tool_result(content: str, result: dict) -> dict:
    """A transcript entry holding a tool's result, as Claude Code writes it."""
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": content}]}, "toolUseResult": result}


class HourlyRunsTests(unittest.TestCase):
    def test_tail_and_questions_read_each_runs_last_utterance_and_match_results_by_tool_id(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            projects = home / ".claude/projects/-w-triage"
            question = {"type": "tool_use", "name": "AskUserQuestion", "id": "toolu_1",
                        "input": {"questions": [{"question": "Which release?"}]}}
            asked = {**assistant(text("Choose a release."), question), "timestamp": "2026-10-28T01:00:00Z"}
            unrelated = tool_result("another tool finished", {})
            unrelated["message"]["content"][0]["tool_use_id"] = "other_tool"
            write_jsonl(projects / "waiting.jsonl", [asked, unrelated,
                         {**user("Hold on."), "timestamp": "2026-10-28T01:01:00Z"}])
            write_jsonl(projects / "answered.jsonl", [asked,
                {**tool_result("answered", {"answers": {"Which release?": "Release B"}}),
                 "timestamp": "2026-10-28T02:00:00Z"},
                {**assistant(text("Release B is queued.")), "timestamp": "2026-10-28T02:01:00Z"}])
            runs = [{"status": "completed", "usage": {"providerSessionId": name}, "workspaceId": "repo::/w/triage"}
                    for name in ("waiting", "answered")]
            with patch.object(hourly_runtime, "scheduled_runs", return_value=runs), \
                    patch.object(Path, "home", return_value=home), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(hourly_runs.main(["--workspace", "/w/triage", "--tail", "--questions", "--json"]), 0)
            waiting, answered = json.loads(out.getvalue())
        self.assertEqual((waiting["tail"]["role"], waiting["tail"]["text"]), ("user", "Hold on."))
        self.assertTrue(waiting["ask_user_question_waiting"])
        self.assertEqual(waiting["questions"][0]["asked_at"], "2026-10-28T01:00:00Z")
        self.assertIsNone(waiting["questions"][0]["answered_at"])
        self.assertEqual(answered["tail"]["text"], "Release B is queued.")
        self.assertFalse(answered["ask_user_question_waiting"])
        self.assertEqual((answered["questions"][0]["question"], answered["questions"][0]["answer"],
                          answered["questions"][0]["answered_at"]),
                         ("Which release?", "Release B", "2026-10-28T02:00:00Z"))

    def test_lists_each_scheduled_run_with_its_precheck_reason_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "Application Support" / "home"
            transcript = home / ".claude/projects/-w-triage/sid-1.jsonl"
            write_jsonl(transcript, [user("/orca-session-triage hourly")])
            runs = [{"status": "completed", "scheduledFor": 1793084400000, "startedAt": 1793084415827,
                     "terminalPaneKey": "tab:leaf", "usage": {"providerSessionId": "sid-1"},
                     "workspaceId": "repo::/w/triage"},
                    {"status": "skipped_precheck", "scheduledFor": 1793080800000, "startedAt": 1793080828333,
                     "terminalPaneKey": None, "workspaceId": "repo::/w/triage",
                     "precheckResult": {"exitCode": 1, "stdout": "skip: a scheduled turn is working\n"}}]
            with patch.object(hourly_runtime, "scheduled_runs", return_value=runs), \
                    patch.object(Path, "home", return_value=home), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(hourly_runs.main(["--workspace", "/w/triage"]), 0)

        def local(ms: float) -> str:
            return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")
        self.assertEqual(out.getvalue().splitlines(), [
            f"{local(1793084400000)} | completed | started {local(1793084415827)} | - | pane tab:leaf | {transcript}",
            f"{local(1793080800000)} | skipped_precheck | started {local(1793080828333)}"
            " | skip: a scheduled turn is working | pane - | -",
        ])

    def test_set_prompt_replaces_the_triage_automations_prompt_and_fails_when_orca_reads_back_another(self) -> None:
        saved = {"triage": "/orca-session-triage hourly 旧", "other": "/orca-session-triage hourly 別"}
        edits: list[list[str]] = []
        keep = {"value": lambda prompt: prompt}

        def orca(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[1:3] == ["automations", "edit"]:
                edits.append(args)
                saved[args[3]] = keep["value"](args[args.index("--prompt") + 1])
            result = {"automations": [{"id": key, "workspaceId": f"repo::/w/{key}", "prompt": prompt}
                                      for key, prompt in saved.items()]}
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": "1", "ok": True, "result": result}))

        def run(*argv: str) -> tuple[int, str, str]:
            with patch.object(hourly_runtime.subprocess, "run", side_effect=orca), \
                    redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                code = hourly_runs.main(["--workspace", "/w/triage", "--orca", "test-orca", *argv])
            return code, out.getvalue(), err.getvalue()

        with tempfile.TemporaryDirectory() as root:
            prompt = Path(root) / "prompt.md"
            self.assertEqual(run("--prompt")[:2], (0, "/orca-session-triage hourly 旧\n"))
            prompt.write_text("/orca-session-triage hourly 新\n二行目\n", encoding="utf-8")
            self.assertEqual(run("--set-prompt", str(prompt))[0], 0)
            self.assertEqual(edits[-1], ["test-orca", "automations", "edit", "triage", "--prompt",
                                         "/orca-session-triage hourly 新\n二行目", "--json"])
            self.assertEqual(saved["other"], "/orca-session-triage hourly 別")
            keep["value"] = lambda prompt: prompt.splitlines()[0]
            code, _, error = run("--set-prompt", str(prompt))
            self.assertEqual(code, 1)
            self.assertIn("-二行目", error)
            # Another prompt would hide the automation from every tool that finds the scheduled runs by it.
            prompt.write_text("hourly 新\n", encoding="utf-8")
            self.assertEqual(run("--set-prompt", str(prompt))[0], 1)
            self.assertEqual(len(edits), 2)


class InboxWatchServiceTests(unittest.TestCase):
    def test_plist_runs_the_existing_watch_with_the_existing_offset(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            payload = inbox_service.plistlib.loads(inbox_service.plist_bytes(
                root, python="/test/bin/python3", orca="/app/bin/orca"))
            self.assertEqual(payload["ProgramArguments"], [
                "/test/bin/python3",
                f"{root}/.cache/orca-briefs/bin/watch-inbox",
                f"{root}/.cache/orca-briefs/offsets/close-done-triage.offset"])
            self.assertTrue(payload["KeepAlive"])
            self.assertIn("/app/bin", payload["EnvironmentVariables"]["PATH"])

    def test_activation_refuses_a_second_reader_of_the_offset(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            plist, watch, offset, _ = inbox_service.paths(root)
            watch.parent.mkdir(parents=True)
            watch.write_text("#!/bin/sh\n")
            watch.chmod(0o700)
            with patch.object(inbox_service, "status", return_value=(False, "stopped")), \
                    patch.object(inbox_service, "offset_readers", return_value=[44]):
                with self.assertRaisesRegex(RuntimeError, "PID 44"):
                    inbox_service.activate(root)
            self.assertFalse(plist.exists())

    def test_status_requires_launchd_pid_to_own_the_offset(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with patch.object(inbox_service, "launchd_pid", return_value=42), \
                    patch.object(inbox_service, "offset_readers", return_value=[43]):
                self.assertFalse(inbox_service.status(root)[0])
            with patch.object(inbox_service, "launchd_pid", return_value=42), \
                    patch.object(inbox_service, "offset_readers", return_value=[42, 43]):
                self.assertFalse(inbox_service.status(root)[0])
            with patch.object(inbox_service, "launchd_pid", return_value=42), \
                    patch.object(inbox_service, "offset_readers", return_value=[42]):
                self.assertTrue(inbox_service.status(root)[0])

    def test_alert_service_uses_a_separate_cursor_and_notifier(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            payload = inbox_service.plistlib.loads(inbox_service.alert_plist_bytes(
                root, python="/test/bin/python3", tools=("/o/bin/orca", "/n/bin/terminal-notifier")))
            self.assertEqual(payload["Label"], inbox_service.ALERT_LABEL)
            self.assertEqual(payload["ProgramArguments"], [
                "/test/bin/python3", str(SCRIPTS / "inbox_alert.py")])
            # launchd's PATH lacks both tools the notification needs to open the sender's tab.
            self.assertLessEqual({"/o/bin", "/n/bin", "/usr/bin"},
                                 set(payload["EnvironmentVariables"]["PATH"].split(":")))
            self.assertNotIn("close-done-triage.offset", str(payload))

    def test_alert_activation_starts_after_existing_inbox_records(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            _, inbox, cursor, _ = inbox_service.alert_paths(root)
            write_jsonl(inbox, [{"kind": "ask", "worktree": "old", "text": "already handled"}])
            with patch.object(inbox_service, "alert_status", side_effect=[(False, "stopped"), (True, "running")]), \
                    patch.object(inbox_service.subprocess, "run") as launchctl:
                inbox_service.activate_alerts(root)
            self.assertEqual(int(cursor.read_text()), inbox.stat().st_size)
            self.assertEqual(launchctl.call_args.args[0][:2], ["launchctl", "bootstrap"])


class InboxReadTests(unittest.TestCase):
    def setUp(self) -> None:
        # Not the Orca tab that runs the tests, nor this Mac's answer records.
        answers = tempfile.TemporaryDirectory()
        self.addCleanup(answers.cleanup)
        external = patch.dict(os.environ, {"ORCA_CLI_COMMAND": "missing-orca-for-offline-tests", "ORCA_PANE_KEY": "",
                                           "TRIAGE_ASK_ANSWERS": str(Path(answers.name) / "answers.jsonl")})
        external.start()
        self.addCleanup(external.stop)

    def test_read_names_the_scheduled_runs_after_this_tab_s_run_before_the_entries(self) -> None:
        # 11/12: the 10:00 run worked past noon; the 11:00 run was skipped for it and the 12:00 run began unseen.
        hour, ten = 3_600_000, int(datetime(2026, 11, 12, 10, 0).timestamp() * 1000)
        runs = [{"status": "dispatched", "scheduledFor": ten + 2 * hour, "startedAt": ten + 2 * hour + 480_000,
                 "terminalPaneKey": "pane-12"},
                {"status": "skipped_precheck", "scheduledFor": ten + hour, "startedAt": ten + hour + 136_000,
                 "terminalPaneKey": None,
                 "precheckResult": {"stdout": "skip: a scheduled turn in /w/triage is working\n"}},
                {"status": "dispatched", "scheduledFor": ten, "startedAt": ten + 422_000, "terminalPaneKey": "pane-10"},
                {"status": "completed", "scheduledFor": ten - hour, "startedAt": ten - hour, "terminalPaneKey": "p-9"}]
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            inbox, orca = base / "inbox.jsonl", base / "orca"
            write_jsonl(inbox, [{"id": "abcd1234", "kind": "info", "ts": "2026-11-12T12:09:00+09:00",
                                 "worktree": "lane", "text": "経過"}])
            orca.write_text(
                "#!/usr/bin/env python3\nimport json, sys\n"
                f"automations = {[triage_automation('a1', '/w/triage')]!r}\n"
                f"runs = {[{**run, 'trigger': 'scheduled', 'workspaceId': 'repo1::/w/triage'} for run in runs]!r}\n"
                "result = {'automations': automations} if sys.argv[1:3] == ['automations', 'list'] else {'runs': runs}\n"
                "print(json.dumps({'result': result}))\n")
            orca.chmod(0o755)
            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(base / "read.offset"),
                                         "ORCA_CLI_COMMAND": str(orca), "ORCA_PANE_KEY": "pane-10",
                                         "ORCA_WORKTREE_ID": "repo1::/w/triage"}), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(inbox_read.main(["read", "--summary"]), 0)
        self.assertEqual(out.getvalue().splitlines()[:3], [
            "注意: 11/12 11:00 の定時の回は、この回が動いていたため skipped_precheck になった（11/12 11:02）",
            "注意: この回の後に 11/12 12:00 の定時の回が始まった（dispatched、11/12 12:08 開始、pane pane-12）。"
            "既読位置と引き継ぎはその回のもの",
            "2026-11-12T12:09:00+09:00 info - lane id=abcd1234  経過"])

    def test_read_marks_the_asks_and_questions_the_answer_records_hold(self) -> None:
        # 11/11 19:20: three asks answered by then were read from --summary as open and put in a handoff.
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            inbox, answers = base / "inbox.jsonl", base / "answers.jsonl"
            write_jsonl(inbox, [
                {"id": "aaaa1111", "kind": "ask", "ts": "t1", "worktree": "a", "text": "merge しますか？"},
                {"id": "bbbb2222", "kind": "ask", "ts": "t2", "worktree": "b", "text": "2 問です",
                 "questions": [{"header": "merge", "question": "merge しますか？"},
                               {"header": "base", "question": "base は main ですか？"}]},
                {"id": "cccc3333", "kind": "ask", "ts": "t3", "worktree": "c", "text": "未回答"}])
            write_jsonl(answers, [{"id": "aaaa1111", "ts": "2026-11-11T19:15:14+09:00", "how": "send"},
                                  {"id": "bbbb2222:1", "ts": "2026-11-11T19:20:00+09:00", "how": "answered"}])
            outputs = {}
            for mode in ("--summary", "--brief"):
                with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_ASK_ANSWERS": str(answers),
                                             "TRIAGE_READ_OFFSET": str(base / "read.offset")}), \
                        redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(inbox_read.main(["read", mode]), 0)
                outputs[mode] = out.getvalue().splitlines()
        summary, brief = outputs["--summary"], outputs["--brief"]
        self.assertIn("t1 ask - a id=aaaa1111 回答済み（11/11 19:15・send）  merge しますか？", summary)
        self.assertIn("t2 ask - b id=bbbb2222  2 問です  [質問 2 件: merge（回答済み 11/11 19:20・answered）・base]",
                      summary)
        self.assertIn("t3 ask - c id=cccc3333  未回答", summary)
        self.assertIn("t1 ask - a id=aaaa1111 回答済み（11/11 19:15・send）", brief)
        self.assertIn("Q1 id=bbbb2222:1 [merge] 回答済み（11/11 19:20・answered） merge しますか？（推奨: None）", brief)
        self.assertIn("Q2 id=bbbb2222:2 [base] base は main ですか？（推奨: None）", brief)

    def test_open_asks_distinguishes_an_old_claim_from_a_question_still_on_screen(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            inbox, claims = base / "inbox.jsonl", base / "claims.jsonl"
            write_jsonl(inbox, [{"id": "abcd1234", "kind": "ask", "ts": "2026-11-01T01:00:00Z",
                                 "worktree": "lane", "text": "公開しますか？"}])
            write_jsonl(claims, [{"id": "abcd1234", "by": "old-lead", "ts": "2026-11-01T02:00:00Z"}])

            def orca(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                result = ({"worktrees": [{"worktreeId": "repo::/w/old-lead", "path": "/w/old-lead",
                                          "agents": [{"state": "done", "stateStartedAt": 1793498400000}]}]}
                          if args[1:3] == ["worktree", "ps"] else {"terminals": []})
                return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, "result": result}))

            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_ASK_CLAIMS": str(claims),
                                          "TRIAGE_ASK_ANSWERS": str(base / "answers.jsonl")}), \
                    patch.object(inbox_read.subprocess, "run", side_effect=orca):
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(inbox_read.main(["open-asks", "--json", "--orca", "test-orca",
                                                       "--claude-projects", str(base / "projects")]), 0)
                claim = json.loads(out.getvalue())[0]["claimed"]
                self.assertGreater(claim["inactive_minutes"], 15)
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(inbox_read.main(["open-asks", "--orca", "test-orca",
                                                       "--claude-projects", str(base / "projects")]), 0)
                self.assertIn("伺っていない（claim のみ、", out.getvalue())
                self.assertNotIn("伺い中", out.getvalue())
                saved = base / "scan.json"
                saved.write_text(json.dumps([{"path": "/w/lane", "repo": "app", "class": "waiting",
                    "claude": {"final_at": "2026-11-01T01:00:00Z", "final_text": "公開しますか？"}}]))
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(hourly_state.main(["--state-file", str(base / "state.json"), "stale-asks",
                        "--from", str(saved), "--orca", "test-orca", "--claude-projects", str(base / "projects"),
                        "--decisions-file", str(base / "decisions.jsonl")]), 0)
                self.assertGreater(json.loads(out.getvalue())[0]["ask"]["claimed"]["inactive_minutes"], 15)

    def test_a_filtered_read_gives_no_offset_to_acknowledge(self) -> None:
        # A filtered watch acknowledged the asks it hid (HOSYU_TEST-329 and 325, 2026-10-27 to 28).
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "read.offset"
            write_jsonl(inbox, [
                {"ts": "t1", "kind": "ask", "host": "host1", "repo": "repo-a",
                 "worktree": "same", "text": "質問"},
                {"ts": "t2", "kind": "info", "host": "host1", "repo": "repo-a",
                 "worktree": "same", "text": "経過"},
                {"ts": "t3", "kind": "done", "host": "host1", "repo": "repo-a",
                 "worktree": "same", "text": "完了"},
                {"ts": "t4", "kind": "ask", "host": "host1", "repo": "repo-b",
                 "worktree": "same", "text": "別の送り元"},
                {"ts": "t5", "kind": "ask", "host": "host1", "worktree": "old",
                 "text": "旧形式"},
            ])
            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox_read.main([
                        "read", "--kind", "ask,done", "--source", "repo-a/same"]), 0)
                self.assertEqual(output.getvalue(),
                                 "t1 ask host1 repo-a/same\n質問\n\n"
                                 "t3 done host1 repo-a/same\n完了\n\n")
                self.assertFalse(cursor.exists())
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox_read.main(["read", "--source", "old"]), 0)
                self.assertEqual(output.getvalue(),
                                 "t5 ask host1 old\n旧形式\n\n")

    def test_an_empty_filtered_read_names_matching_entries_before_the_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox, cursor = (Path(root) / name for name in ("inbox.jsonl", "read.offset"))
            write_jsonl(inbox, [
                {"id": "early", "ts": "2026-11-04T08:00:00+09:00", "kind": "ask", "repo": "r",
                 "worktree": "a", "text": "古い質問"},
                {"id": "latest", "ts": "2026-11-04T10:00:00+09:00", "kind": "ask", "repo": "r",
                 "worktree": "a", "text": "最新の質問"},
                {"id": "remote", "ts": "2026-11-04T09:00:00+09:00", "kind": "ask", "repo": "r",
                 "worktree": "a", "text": "遅れて届いた質問"},
                {"id": "other", "ts": "2026-11-04T11:00:00+09:00", "kind": "done", "repo": "r",
                 "worktree": "b", "text": "別の送り元"},
            ])
            acknowledged = str(inbox.stat().st_size)
            cursor.write_text(acknowledged)
            with inbox.open("a") as stream:
                stream.write(json.dumps({"id": "new", "kind": "info", "worktree": "c", "text": "経過"}) + "\n")

            def run(*argv: str) -> tuple[int, str, str]:
                with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    code = inbox_read.main(["read", *argv])
                return code, out.getvalue(), err.getvalue()

            for filters in (("--kind", "ask"), ("--source", "r/a"), ("--kind", "ask", "--source", "r/a")):
                with self.subTest(filters=filters):
                    code, output, error = run(*filters)
                    self.assertEqual((code, output), (0, ""))
                    self.assertIn("before the cursor: 3", error)
                    self.assertIn("id=latest", error)
                    self.assertIn("2026-11-04T10:00:00+09:00", error)
            self.assertIn("before the cursor: 0", run("--source", "missing")[2])
            self.assertEqual(cursor.read_text(), acknowledged)

    def test_source_takes_a_worktree_name_alone_and_names_near_sources_when_none_has_it(self) -> None:
        # 2026-11-06: backlog-watch's worktree names passed to --source read 0 entries.
        with tempfile.TemporaryDirectory() as root:
            inbox, cursor = (Path(root) / name for name in ("inbox.jsonl", "read.offset"))
            write_jsonl(inbox, [
                {"ts": "t1", "kind": "done", "host": "h", "repo": "construction-materials",
                 "worktree": "general-205-close-check", "text": "投稿した"},
                {"ts": "t2", "kind": "info", "host": "h", "worktree": "general-205-close-check", "text": "旧形式"},
                {"ts": "t3", "kind": "ask", "host": "h", "repo": "repo-a", "worktree": "same", "text": "a"},
                {"ts": "t4", "kind": "ask", "host": "h", "repo": "repo-b", "worktree": "same", "text": "b"},
            ])

            def run(source: str) -> tuple[str, str]:
                with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(inbox_read.main(["read", "--source", source]), 0)
                return out.getvalue(), err.getvalue()

            self.assertEqual(run("general-205-close-check")[0],
                             "t1 done h construction-materials/general-205-close-check\n投稿した\n\n"
                             "t2 info h general-205-close-check\n旧形式\n\n")
            output, error = run("general-205-close")
            self.assertEqual(output, "")
            self.assertIn("construction-materials/general-205-close-check", error)
            # A worktree name two repos have names neither; both are near.
            output, error = run("same")
            self.assertEqual(output, "")
            self.assertIn("repo-a/same, repo-b/same", error)

    def test_text_finds_a_word_past_the_summary_cut_wherever_the_cursor_stands(self) -> None:
        # The summary's 160 characters cut the word the lead searched for.
        with tempfile.TemporaryDirectory() as root:
            inbox, cursor = (Path(root) / name for name in ("inbox.jsonl", "read.offset"))
            write_jsonl(inbox, [{"id": "old", "ts": "t1", "kind": "done", "host": "h", "repo": "r", "worktree": "a",
                                 "text": "経過 " + "x" * 200 + "\nPR #12 を Merge した\n別の行"}])
            acknowledged = str(inbox.stat().st_size)
            cursor.write_text(acknowledged)
            with inbox.open("a", encoding="utf-8") as stream:
                for row in ({"id": "new", "ts": "t2", "kind": "ask", "host": "h", "worktree": "b", "text": "質問",
                             "questions": [{"header": "公開", "question": "merge しますか？", "recommended": "A"}]},
                            {"id": "miss", "ts": "t3", "kind": "info", "host": "h", "worktree": "c", "text": "無関係"}):
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}), \
                    redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                self.assertEqual(inbox_read.main(["read", "--text", "MERGE"]), 0)
            self.assertEqual(out.getvalue(), "t1 done h r/a id=old 既読\nPR #12 を Merge した\n\n"
                                             "t2 ask h b id=new\nQ1 id=new:1 [公開] merge しますか？（推奨: A）\n\n")
            self.assertEqual(cursor.read_text(), acknowledged)

    def test_since_reads_acknowledged_entries_by_time_without_an_offset_to_acknowledge(self) -> None:
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        with tempfile.TemporaryDirectory() as root:
            inbox, cursor = (Path(root) / name for name in ("inbox.jsonl", "read.offset"))
            write_jsonl(inbox, [
                {"id": "before", "ts": (midnight - timedelta(seconds=1)).isoformat(), "kind": "ask",
                 "worktree": "a", "text": "昨日の質問"},
                {"id": "since", "ts": midnight.isoformat(), "kind": "ask", "worktree": "a", "text": "今日の質問"},
                {"id": "info", "ts": midnight.isoformat(), "kind": "info", "worktree": "a", "text": "経過"},
                {"id": "bad-time", "ts": "unknown", "kind": "ask", "worktree": "a", "text": "時刻なし"},
            ])
            acknowledged = str(inbox.stat().st_size)
            cursor.write_text(acknowledged)
            for since in ("00:00", midnight.isoformat()):
                with self.subTest(since=since), \
                        patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    self.assertEqual(inbox_read.main(["read", "--since", since]), 0)
                    self.assertIn("id=since", out.getvalue())
                    self.assertIn("id=info", out.getvalue())
                    for hidden in ("id=before", "id=bad-time", "NEXT_OFFSET"):
                        self.assertNotIn(hidden, out.getvalue())
                with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    self.assertEqual(inbox_read.main([
                        "read", "--since", since, "--kind", "ask", "--source", "a", "--summary"]), 0)
                self.assertEqual(len(out.getvalue().splitlines()), 1)
                self.assertIn("id=since", out.getvalue())
            self.assertEqual(cursor.read_text(), acknowledged)

    def test_questions_come_four_at_a_time_ready_for_ask_user_question(self) -> None:
        def question(n: int, **extra: object) -> dict:
            return {"question": f"Q{n} をどうしますか？", "header": f"判断{n}", "multiSelect": False,
                    "options": [{"label": "残す", "description": "今の帳票のまま"},
                                {"label": "直す", "description": "移した帳票に合わせる"}],
                    "recommended": "直す", **extra}

        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            write_jsonl(inbox, [
                {"id": "r1", "ts": "t1", "kind": "ask", "host": "h", "repo": "report", "worktree": "decisions",
                 "handle": "term_a", "text": "判断 4 件", "questions": [
                     question(1, issue="spin-dd/taihei-report#239", context="移した帳票を出す作業です。"),
                     question(2), question(3), question(4)]},
                {"id": "r2", "ts": "t2", "kind": "ask", "host": "h", "worktree": "plain", "text": "文章だけの質問"},
                {"id": "r3", "ts": "t3", "kind": "ask", "host": "h", "repo": "report", "worktree": "decisions",
                 "text": "判断 2 件", "questions": [question(5), question(6)]},
            ])
            environment = {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(Path(root) / "read.offset"),
                           "TRIAGE_ASK_ANSWERS": str(Path(root) / "answers.jsonl")}

            def run(*argv: str) -> str:
                with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(inbox_read.main(list(argv)), 0)
                return out.getvalue()

            run("answered", "r1:2", "--note", "Q2 は直す")
            first = json.loads(run("questions"))
            rest = json.loads(run("questions", "--after", first["next_after"]))
            # The lead answers the batch, then asks for the one after it.
            run("answered", ",".join(item["id"] for item in first["items"]))
            after_answered = json.loads(run("questions", "--after", first["next_after"]))

        self.assertEqual([item["id"] for item in first["items"]], ["r1:1", "r1:3", "r1:4", "r3:1"])
        self.assertEqual(first["questions"][0], {
            "question": "移した帳票を出す作業です。 Q1 をどうしますか？（spin-dd/taihei-report#239）", "header": "判断1",
            "multiSelect": False,
            "options": [{"label": "直す（推奨）", "description": "移した帳票に合わせる"},
                        {"label": "残す", "description": "今の帳票のまま"}]})
        self.assertEqual((first["items"][0]["source"], first["items"][0]["handle"]), ("report/decisions", "term_a"))
        self.assertEqual((first["remaining"], rest["remaining"], rest["next_after"]), (1, 0, None))
        self.assertEqual([item["id"] for item in rest["items"]], ["r3:2"])
        self.assertEqual([item["id"] for item in after_answered["items"]], ["r3:2"])

    def test_open_asks_lists_unanswered_asks_oldest_first_until_answered(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            two = [{"question": f"Q{n}？", "header": f"Q{n}", "recommended": "A", "multiSelect": False,
                    "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}
                   for n in (1, 2)]
            write_jsonl(inbox, [
                {"id": "late", "ts": "2026-10-28T09:00:00+0900", "kind": "ask", "host": "h", "worktree": "a",
                 "text": "文章の質問"},
                {"id": "done1", "ts": "2026-10-28T08:30:00+0900", "kind": "done", "host": "h", "worktree": "a",
                 "text": "完了"},
                {"id": "early", "ts": "2026-10-28T08:00:00+09:00", "kind": "ask", "host": "mbp16", "repo": "r",
                 "worktree": "b", "text": "判断 2 件", "questions": two},
                {"id": "c8f79c0f-1111-4222-8333-444455556666", "ts": "2026-10-28T09:30:00+09:00", "kind": "ask",
                 "host": "h", "worktree": "c", "text": "merge してよいか"},
            ])
            environment = {"TRIAGE_INBOX": str(inbox), "TRIAGE_ASK_ANSWERS": str(Path(root) / "answers.jsonl")}

            def run(*argv: str) -> tuple[int, str, str]:
                with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()) as out, \
                        redirect_stderr(io.StringIO()) as err:
                    code = inbox_read.main(list(argv))
                return code, out.getvalue(), err.getvalue()

            listed = json.loads(run("open-asks", "--json")[1])
            run("answered", "late,early:1", "--note", "send --decision で答えた")
            partly = json.loads(run("open-asks", "--json")[1])
            run("answered", "early:2")
            by_prefix = run("answered", "c8f79c0f")
            unknown = run("answered", "done1")
            recorded = [json.loads(line)["id"] for line in (Path(root) / "answers.jsonl").read_text().splitlines()]

            self.assertEqual([(ask["id"], ask["source"], ask["open_questions"]) for ask in listed],
                             [("early", "r/b", [{"number": n, "claimed": None, **question} for n, question in enumerate(two, 1)]),
                              ("late", "a", []),
                              ("c8f79c0f-1111-4222-8333-444455556666", "c", [])])
            self.assertEqual([(ask["id"], ask["open_questions"]) for ask in partly],
                             [("early", [{"number": 2, "claimed": None, **two[1]}]), ("c8f79c0f-1111-4222-8333-444455556666", [])])
            self.assertEqual((by_prefix[0], recorded[-1]), (0, "c8f79c0f-1111-4222-8333-444455556666"))
            self.assertEqual(run("open-asks")[1], "")
            self.assertEqual(unknown[0], 1)
            self.assertIn("done1", unknown[2])

    def test_open_asks_closes_an_adoption_notice_once_a_lane_holds_its_proposal(self) -> None:
        # 2026-11-05: finding the lane of 14 adoption asks took a grep -r cut off at 120 seconds.
        ledger_script = ROOT / "adoption-triage/scripts/ledger.py"
        with tempfile.TemporaryDirectory() as root:
            inbox, answers, events, imports, writer = (
                Path(root) / name for name in ("inbox.jsonl", "answers.jsonl", "events.jsonl", "imports", "writer.json"))
            writer.write_text('{"decision_writer": true, "name": "board"}')

            def ledger(command: str, body: dict) -> dict:
                result = subprocess.run([sys.executable, str(ledger_script), "--events", str(events), "--imports",
                                         str(imports), "--decision-config", str(writer), command],
                                        input=json.dumps(body, ensure_ascii=False), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            held, waiting = (ledger("propose", {
                "source": {"session": "triage", "worktree": "triage"}, "fact": "事実", "proposal": "案",
                "reason": "理由", "recommendation": "adopt", "priority": "normal"})["proposal_id"] for _ in range(2))
            for proposal_id in (held, waiting):
                adoption = ledger("decide", {"proposal_id": proposal_id, "decision": "adopt", "actor": "board"})["id"]
                ledger("record", {"proposal_id": proposal_id, "decision_id": adoption, "outcome": "handed_off",
                                  "actor": "triage-retro", "result": "担当へ渡した"})
                if proposal_id == held:
                    ledger("record", {"proposal_id": proposal_id, "decision_id": adoption, "outcome": "handed_off",
                                      "actor": "adopt-lane", "result": "担当の handle term_1・指示書 /briefs/b.md"})
            write_jsonl(inbox, [
                {"id": f"{name}-ask", "ts": f"2026-11-06T15:1{n}:00+0900", "kind": "ask", "repo": "skills",
                 "worktree": "adopt-triage-inbox",
                 "text": f"採用 triage の採用に担当を立ててください。\n案の ID: {proposal_id}\n案: 案\n決定: 採用"}
                for n, (name, proposal_id) in enumerate((("held", held), ("waiting", waiting)))])
            environment = {"TRIAGE_INBOX": str(inbox), "TRIAGE_ASK_ANSWERS": str(answers),
                           "ADOPTION_TRIAGE_SCRIPT": str(ledger_script), "ADOPTION_TRIAGE_EVENTS": str(events),
                           "ADOPTION_TRIAGE_IMPORTS": str(imports)}

            def run(*argv: str) -> tuple[int, str, str]:
                with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()) as out, \
                        redirect_stderr(io.StringIO()) as err:
                    code = inbox_read.main(list(argv))
                return code, out.getvalue(), err.getvalue()

            code, output, error = run("open-asks", "--json")
            self.assertEqual((code, [ask["id"] for ask in json.loads(output)]), (0, ["waiting-ask"]))
            self.assertIn("held-ask", error)
            self.assertIn("adopt-lane", error)
            code, output, error = run("open-asks")
            self.assertEqual((code, output.count("\n"), error), (0, 1, ""))
            self.assertIn("id=waiting-ask", output)

    def test_open_asks_leaves_marked_handoffs_to_lead_decisions(self) -> None:
        question = {"question": "続けますか？", "header": "継続", "recommended": "続ける",
                    "options": [{"label": "続ける", "description": "作業を再開する"},
                                {"label": "待つ", "description": "次の判断を待つ"}]}
        with tempfile.TemporaryDirectory() as root:
            inbox, answers = (Path(root) / name for name in ("inbox.jsonl", "answers.jsonl"))
            write_jsonl(inbox, [
                {"id": "handoff", "kind": "ask", "worktree": "triage", "questions": [question],
                 "text": "判断をお願いします\n\n—— 送る直前に道具が足した事実 ——\n走査の開始後に届いた報告: 0 件（なし）"},
                {"id": "normal", "kind": "ask", "worktree": "triage", "questions": [question],
                 "text": "引き継ぎのない通常の質問"},
            ])
            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_ASK_ANSWERS": str(answers)}):
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(inbox_read.main(["open-asks", "--json"]), 0)
                self.assertEqual([ask["id"] for ask in json.loads(out.getvalue())], ["normal"])
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(inbox_read.main(["questions"]), 0)
                self.assertEqual([item["id"] for item in json.loads(out.getvalue())["items"]], ["normal:1"])
            self.assertFalse(answers.exists())

    def test_answered_before_marks_only_asks_an_answer_proves_and_names_the_rest(self) -> None:
        # 2026-10-29: a bulk mark took an unanswered ask out of open-asks, and it waited 25 hours.
        options = [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]
        # JSON escapes the quotes and the line break; the question is read from the parsed answers.
        two: list[dict] = [{"question": text, "header": f"Q{n}", "recommended": "A", "options": options}
               for n, text in enumerate(('"dry-run" で進めてよいですか？', "2 行に\nわたる質問ですか？"), 1)]
        merge = {"question": "この PR を merge してよいですか？", "header": "merge", "recommended": "A", "options": options}
        alike = {**merge, "question": "同じ問いですか？"}
        with tempfile.TemporaryDirectory() as root:
            inbox, answers, log = (Path(root) / name for name in ("inbox.jsonl", "answers.jsonl", "decisions.jsonl"))
            write_jsonl(inbox, [
                {"id": "4bd9cae3-a6d6-4835", "ts": "2026-10-28T09:18:41+0900", "kind": "ask",
                 "worktree": "report-decisions", "text": "c1 の全文と投稿先の案です"},
                {"id": "bc7e1884-0000", "ts": "2026-10-28T20:02:05+0900", "kind": "ask", "worktree": "deeplink",
                 "text": "写真の進め方", "questions": two},
                {"id": "77aa0001-0000", "ts": "2026-10-28T12:00:00+0900", "kind": "ask", "worktree": "fix-login",
                 "text": "acme/app#4512 を merge してよいか"},
                {"id": "cc330000-0000", "ts": "2026-10-28T12:00:00+0900", "kind": "ask", "worktree": "est-177",
                 "text": "2 つの判断", "questions": [{**question, "question": "est の" + question["question"]}
                                                    for question in two]},
                # The same question about two PRs, told apart by the issue the user saw beside it.
                {"id": "ee440000-0000", "ts": "2026-10-28T12:00:00+0900", "kind": "ask", "worktree": "pr-123",
                 "text": "merge", "questions": [{**merge, "issue": "acme/app#123"}]},
                {"id": "ff550000-0000", "ts": "2026-10-28T12:00:00+0900", "kind": "ask", "worktree": "pr-124",
                 "text": "merge", "questions": [{**merge, "issue": "acme/app#124"}]},
                # Two asks show the same question: an answer to it cannot tell which one it answers.
                {"id": "aa660000-0000", "ts": "2026-10-28T12:00:00+0900", "kind": "ask", "worktree": "lane-a",
                 "text": "問い", "questions": [alike]},
                {"id": "bb770000-0000", "ts": "2026-10-28T12:00:00+0900", "kind": "ask", "worktree": "lane-b",
                 "text": "問い", "questions": [alike]},
            ])
            # Q1 of the last ask was answered through send --decision; its Q2 is still open.
            write_jsonl(answers, [{"id": "cc330000-0000:1", "ts": "2026-10-28T13:00:00+09:00",
                                   "how": "send --decision", "note": "A"}])
            write_jsonl(log, [
                {"id": "1", "decision": "answer:report-decisions::c1", "answer": "ask 4bd9cae3-a6d6-4835 はこの全文で出す",
                 "decided_at": "2026-10-28T10:00:00+00:00"},
                # Only the worktree or the PR: another of its questions may be the one answered.
                {"id": "2", "decision": "answer:fix-login::other", "answer": "別の件",
                 "decided_at": "2026-10-28T04:00:00+00:00"},
                {"id": "3", "decision": "merge:acme/app#4512", "answer": "merge してよい",
                 "decided_at": "2026-10-28T04:10:00+00:00"},
                {"id": "4", "decision": "remove:local::/w/fix-login", "answer": "消してよい",
                 "decided_at": "2026-10-28T04:20:00+00:00"},
            ])
            lead = Path(root) / "triage-lead"
            transcript = scan.claude_project_dir(Path(root) / "projects", str(lead)) / "lead.jsonl"
            transcript.parent.mkdir(parents=True)
            asked = [*two, {**merge, "question": merge["question"] + "（acme/app#123）"}, alike]
            write_jsonl(transcript, [{"type": "user", "timestamp": "2026-10-29T01:21:00Z", "toolUseResult": {
                "questions": asked, "answers": {question["question"]: "A" for question in asked}}}])
            environment = {"TRIAGE_INBOX": str(inbox), "TRIAGE_ASK_ANSWERS": str(answers)}

            with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()) as out, \
                    redirect_stderr(io.StringIO()) as err:
                code = inbox_read.main(["answered", "--before", "2026-10-29T12:00:00+09:00", "--note", "答え済み",
                                        "--decisions-file", str(log), "--asked-in", str(lead),
                                        "--claude-projects", str(Path(root) / "projects")])

            marked = [record["id"] for record in map(json.loads, answers.read_text().splitlines())
                      if record["how"] == "inbox_read.py answered"]
            self.assertEqual((code, out.getvalue()), (1, "answered: 3\n"))
            self.assertEqual(sorted(marked), ["4bd9cae3-a6d6-4835", "bc7e1884-0000", "ee440000-0000"])
            refused = err.getvalue()
            for ask_id in ("77aa0001-0000", "cc330000-0000", "ff550000-0000", "aa660000-0000", "bb770000-0000"):
                self.assertIn(f"id={ask_id}", refused)
            self.assertIn("lead_decisions answer:fix-login::other", refused)
            self.assertIn("lead_decisions merge:acme/app#4512", refused)
            self.assertNotIn("remove:", refused)

    def test_watch_shows_every_ask_and_done_and_reminds_of_asks_left_unanswered(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "watch.offset"
            old = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(timespec="seconds")
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            write_jsonl(inbox, [
                {"id": "a1", "ts": old, "kind": "ask", "host": "h", "repo": "epm", "worktree": "x", "text": "古い質問"},
                {"id": "i1", "ts": now, "kind": "info", "host": "h", "repo": "epm", "worktree": "x", "text": "経過"},
                {"id": "a2", "ts": now, "kind": "ask", "host": "h", "repo": "cvm", "worktree": "y", "text": "新しい質問"},
                {"id": "d1", "ts": now, "kind": "done", "host": "h", "repo": "cvm", "worktree": "y", "text": "完了"},
            ])
            environment = {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor),
                           "TRIAGE_ASK_ANSWERS": str(Path(root) / "answers.jsonl")}
            with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()) as refused:
                self.assertEqual(inbox_read.main(["watch", "--once"]), 1)
            self.assertIn("initialize", refused.getvalue())
            with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()) as shared:
                del os.environ["TRIAGE_READ_OFFSET"]
                self.assertEqual(inbox_read.main(["watch", "--once"]), 1)
            self.assertIn("TRIAGE_READ_OFFSET", shared.getvalue())
            cursor.write_text("0")

            def watch() -> list[str]:
                with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(inbox_read.main(["watch", "--once"]), 0)
                return out.getvalue().splitlines()

            first = watch()
            with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()):
                inbox_read.main(["answered", "a1"])
            with inbox.open("a") as stream:
                stream.write(json.dumps({"id": "d2", "ts": now, "kind": "done", "host": "h", "worktree": "z",
                                         "text": "次の完了"}, ensure_ascii=False) + "\n")
            second = watch()

        self.assertEqual([line.split("  ")[0] for line in first], [
            f"{old} ask h epm/x id=a1", f"{now} ask h cvm/y id=a2", f"{now} done h cvm/y id=d1",
            f"unanswered 30 min: {old} ask h epm/x id=a1"])
        self.assertEqual([line.split("  ")[0] for line in second], [f"{now} done h z id=d2"])

    def test_a_claimed_ask_shows_as_being_asked_without_a_reminder_or_a_second_asking(self) -> None:
        # 11/04 16:00: the hourly tab and the lead both asked TOUGOU_KAIKEI-799 and #172.
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "watch.offset"
            old = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(timespec="seconds")
            two = [{"question": f"Q{n}？", "header": f"Q{n}", "recommended": "A", "multiSelect": False,
                    "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}
                   for n in (1, 2)]
            write_jsonl(inbox, [
                {"id": "a1", "ts": old, "kind": "ask", "host": "h", "worktree": "x", "text": "投稿の 2 問",
                 "questions": two},
                {"id": "a2", "ts": old, "kind": "ask", "host": "h", "worktree": "y", "text": "merge してよいか"},
            ])
            cursor.write_text(str(inbox.stat().st_size))
            environment = {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor),
                           "TRIAGE_ASK_ANSWERS": str(Path(root) / "answers.jsonl"),
                           "TRIAGE_ASK_CLAIMS": str(Path(root) / "claims.jsonl")}

            def run(*argv: str) -> tuple[int, str, str]:
                with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()) as out, \
                        redirect_stderr(io.StringIO()) as err:
                    code = inbox_read.main(list(argv))
                return code, out.getvalue(), err.getvalue()

            self.assertEqual(run("claim", "a1", "--by", "triage-hourly")[0], 0)
            # Both tabs read the batch before either claimed it: the second claim is refused.
            refused = run("claim", "a1:2", "--by", "triage")
            self.assertEqual(run("claim", "a2:1", "--by", "triage-hourly")[0], 1)
            listed = run("open-asks")[1].splitlines()
            viewed = {ask["id"]: ask for ask in json.loads(run("open-asks", "--json")[1])}
            batch = json.loads(run("questions")[1])
            watched = run("watch", "--once")[1].splitlines()

        self.assertEqual(refused[0], 1)
        self.assertIn("a1 は 伺い中 triage-hourly", refused[2])
        self.assertIn("伺い中 triage-hourly", listed[0])
        self.assertIn("id=a1", listed[0])
        self.assertNotIn("伺い中", listed[1])
        self.assertEqual(viewed["a1"]["claimed"]["by"], "triage-hourly")
        self.assertIsNone(viewed["a2"]["claimed"])
        self.assertEqual(batch["questions"], [])
        self.assertEqual([(item["id"], item["by"]) for item in batch["claimed"]],
                         [("a1:1", "triage-hourly"), ("a1:2", "triage-hourly")])
        self.assertEqual([line.split("  ")[0] for line in watched],
                         [f"伺い中 triage-hourly: {old} ask h x id=a1", f"unanswered 30 min: {old} ask h y id=a2"])

    def test_taking_over_an_ask_takes_the_questions_claimed_one_by_one(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            two = [{"question": f"Q{n}？", "header": f"Q{n}", "recommended": "A", "multiSelect": False,
                    "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}
                   for n in (1, 2)]
            write_jsonl(inbox, [{"id": ask_id, "ts": "2026-11-05T10:00:00+09:00", "kind": "ask", "host": "h",
                                 "worktree": "x", "text": "2 問", "questions": two} for ask_id in ("a1", "a2")])
            environment = {"TRIAGE_INBOX": str(inbox), "TRIAGE_ASK_ANSWERS": str(Path(root) / "answers.jsonl"),
                           "TRIAGE_ASK_CLAIMS": str(Path(root) / "claims.jsonl")}

            def run(*argv: str) -> int:
                with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()), \
                        redirect_stderr(io.StringIO()):
                    return inbox_read.main(list(argv))

            codes = [run("claim", "a1:1", "--by", "triage-hourly"), run("claim", "a1", "--by", "triage", "--take-over"),
                     run("claim", "a1", "--by", "triage"),
                     # The other way: the questions of an ask another tab claimed whole.
                     run("claim", "a2", "--by", "triage-hourly"),
                     run("claim", "a2:1,a2:2", "--by", "triage", "--take-over"), run("claim", "a2:1", "--by", "triage"),
                     run("claim", "a2", "--by", "triage")]
            with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()) as out:
                inbox_read.main(["questions"])
            with patch.dict(os.environ, environment), redirect_stdout(io.StringIO()) as listed:
                inbox_read.main(["open-asks", "--json"])

        self.assertEqual(codes, [0, 0, 0, 0, 0, 0, 0])
        self.assertEqual([(item["id"], item["by"]) for item in json.loads(out.getvalue())["claimed"]],
                         [("a1:1", "triage"), ("a1:2", "triage"), ("a2:1", "triage"), ("a2:2", "triage")])
        self.assertEqual([ask["claimed"]["by"] for ask in json.loads(listed.getvalue())], ["triage", "triage"])

    def test_summary_prints_one_short_line_per_entry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "read.offset"
            write_jsonl(inbox, [
                {"ts": "t1", "kind": "ask", "host": "host1", "repo": "repo-a",
                 "worktree": "same", "text": "問" * 161 + "\n続き"},
                {"ts": "t2", "kind": "info", "host": "host1", "repo": "repo-b",
                 "worktree": "other", "text": "作業中\n補足"},
                {"ts": "t3", "kind": "done", "host": "host1", "worktree": "old",
                 "text": "完了"},
            ])
            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox_read.main(["read", "--summary"]), 0)
                self.assertEqual(output.getvalue().splitlines(), [
                    "t1 ask host1 repo-a/same  " + "問" * 160 + "…",
                    "t2 info host1 repo-b/other  作業中 補足",
                    "t3 done host1 old  完了",
                    f"NEXT_OFFSET={inbox.stat().st_size}",
                ])

    def test_summary_names_each_entry_and_id_reads_only_those_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "read.offset"
            write_jsonl(inbox, [
                {"id": "a1", "ts": "t1", "kind": "ask", "host": "host1", "repo": "repo-a",
                 "worktree": "same", "text": "前半の質問\n" + "詳" * 200},
                {"id": "b2", "ts": "t2", "kind": "info", "host": "host1", "repo": "repo-b",
                 "worktree": "other", "text": "経過"},
                {"id": "c3", "ts": "t3", "kind": "done", "host": "host1", "repo": "repo-c",
                 "worktree": "last", "text": "完了"},
            ])
            end = inbox.stat().st_size
            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox_read.main(["read", "--summary"]), 0)
                self.assertEqual([line.split("  ")[0] for line in output.getvalue().splitlines()], [
                    "t1 ask host1 repo-a/same id=a1", "t2 info host1 repo-b/other id=b2",
                    "t3 done host1 repo-c/last id=c3", f"NEXT_OFFSET={end}"])
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox_read.main(["read", "--brief", "--id", "c3,a1"]), 0)
                self.assertEqual(output.getvalue(),
                                 "t1 ask host1 repo-a/same id=a1\n前半の質問\n" + "詳" * 200 + "\n\n"
                                 "t3 done host1 repo-c/last id=c3\n完了\n\n")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(inbox_read.main(["read", "--id", "a1,zz"]), 1)
                self.assertIn("zz", error.getvalue())
                self.assertFalse(cursor.exists())

    def test_id_reads_an_8_character_prefix_and_acknowledged_lines_without_moving_the_cursor(self) -> None:
        # 2026-10-27 20:10: a handoff named "id c8f79c0f"; the full UUID copied by hand was mistyped.
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "read.offset"
            handoff, twin = "beee5c90-48b7-425b-b802-568f7e486716", "beee5c90-9999-425b-b802-568f7e486716"
            done = "c8f79c0f-1111-4222-8333-444455556666"
            write_jsonl(inbox, [
                {"id": handoff, "ts": "t1", "kind": "ask", "host": "h", "worktree": "a", "text": "前の回の引き継ぎ"},
                {"id": twin, "ts": "t2", "kind": "info", "host": "h", "worktree": "b", "text": "経過"},
                {"ts": "t0", "kind": "info", "host": "h", "worktree": "old", "text": "id の無い古い行"},
            ])
            acknowledged = str(inbox.stat().st_size)
            cursor.write_text(acknowledged)
            with inbox.open("a") as stream:
                stream.write(json.dumps({"id": done, "ts": "t3", "kind": "done", "host": "h", "worktree": "c",
                                         "text": "完了"}, ensure_ascii=False) + "\n")

            def run(*argv: str) -> tuple[int, str, str]:
                with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    return inbox_read.main(["read", *argv]), out.getvalue(), err.getvalue()

            read = run("--id", "c8f79c0f,beee5c90-48b7")
            ambiguous = run("--id", "beee5c90")
            short = run("--id", "beee5c9")
            position = cursor.read_text()

        self.assertEqual(read, (0, f"t1 ask h a id={handoff} 既読\n前の回の引き継ぎ\n\n"
                                   f"t3 done h c id={done}\n完了\n\n", read[2]))
        self.assertEqual((ambiguous[0], ambiguous[1]), (1, ""))
        self.assertIn(handoff, ambiguous[2])
        self.assertIn(twin, ambiguous[2])
        self.assertEqual(short[0], 1)
        self.assertEqual(position, acknowledged)

    def test_an_ask_s_questions_show_in_full_reads_and_in_summaries(self) -> None:
        # 2026-10-28: read --id of epm-11432-review-fix's ask d57cdf88 showed only its one-line text.
        asked = [{"context": "二重送信を止める直しです。", "question": "どちらで直しますか？", "header": "方針",
                  "multiSelect": False, "recommended": "A", "issue": "spin-dd/taihei-epm-server#11432",
                  "options": [{"label": "A", "description": "画面で止める"}, {"label": "B", "description": "API で止める"}]},
                 {"question": "いつ出しますか？", "header": "時期", "multiSelect": False, "recommended": "今日",
                  "options": [{"label": "今日", "description": "devel へ"}, {"label": "明日", "description": "本番と同時"}]}]
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            write_jsonl(inbox, [{"id": "d57cdf88-1111-4222-8333-444455556666", "ts": "t1", "kind": "ask", "host": "h",
                                 "worktree": "epm-11432-review-fix", "text": "伺い直します", "questions": asked}])

            def run(*argv: str) -> str:
                with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox),
                                             "TRIAGE_READ_OFFSET": str(Path(root) / "read.offset")}), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    self.assertEqual(inbox_read.main(["read", *argv]), 0)
                return out.getvalue()

            full, summary = run("--id", "d57cdf88"), run("--summary")

        self.assertIn("伺い直します\nQ1 id=d57cdf88-1111-4222-8333-444455556666:1 [方針] 二重送信を止める直しです。\n"
                      "  どちらで直しますか？（推奨: A）\n"
                      "  - A: 画面で止める\n  - B: API で止める\n  関わる issue: spin-dd/taihei-epm-server#11432\n"
                      "Q2 id=d57cdf88-1111-4222-8333-444455556666:2 [時期]", full)
        self.assertIn("伺い直します  [質問 2 件: 方針・時期]", summary.splitlines()[0])

    def test_brief_keeps_asks_and_done_full_and_shortens_only_info(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "read.offset"
            long_info = "詳" * 161
            write_jsonl(inbox, [
                {"ts": "t1", "kind": "ask", "host": "host1", "repo": "repo-a",
                 "worktree": "same", "text": "質問全文\n二行目"},
                {"ts": "t2", "kind": "info", "host": "host1", "repo": "repo-b",
                 "worktree": "same", "text": long_info},
                {"ts": "t3", "kind": "done", "host": "host1", "worktree": "old",
                 "text": "完了全文\n二行目"},
            ])
            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox_read.main(["read", "--brief"]), 0)
                rendered = output.getvalue()
                self.assertIn("t1 ask host1 repo-a/same\n質問全文\n二行目", rendered)
                self.assertIn("t2 info host1 repo-b/same\n" + "詳" * 160 + "…", rendered)
                self.assertNotIn(long_info, rendered)
                self.assertIn("t3 done host1 old\n完了全文\n二行目", rendered)
                self.assertEqual(rendered.splitlines()[-1], f"NEXT_OFFSET={inbox.stat().st_size}")
                self.assertFalse(cursor.exists())

    def test_live_tab_of_a_run_marked_dispatch_failed_keeps_the_default_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "default.offset"
            write_jsonl(inbox, [{"kind": "ask", "text": "new question"}])
            cursor.write_text("0")
            runs = [{"status": "dispatch_failed", "terminalPaneKey": "tab15:leaf"},
                    {"status": "completed", "terminalPaneKey": "tab14:leaf"}]
            # A detached tab is listed under its pty id and keeps its pane only as recordedPaneKey.
            terminals = [{"tabId": "pty:w@@15", "leafId": "pty:w@@15", "recordedPaneKey": "tab15:leaf",
                          "connected": True, "orphaned": True},
                         {"tabId": "tab14", "leafId": "leaf", "connected": True}]
            env = {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": "",
                   "ORCA_WORKTREE_ID": "repo::/w/triage", "ORCA_PANE_KEY": "tab14:leaf"}
            with patch.dict(os.environ, env), patch.object(inbox_read, "paths", return_value=(inbox, cursor)), \
                    patch.object(hourly_runtime, "scheduled_runs", return_value=runs), \
                    patch.object(hourly_runtime, "orca_result", return_value={"terminals": terminals}):
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(inbox_read.main(["ack", str(inbox.stat().st_size)]), 1)
                self.assertIn("latest scheduled", error.getvalue())
                os.environ["ORCA_PANE_KEY"] = "tab15:leaf"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(inbox_read.main(["ack", str(inbox.stat().st_size)]), 0)
        # A failed dispatch whose tab is gone still leaves the cursor with the newest live run.
        self.assertEqual(hourly_runtime.latest_pane(runs, frozenset({"tab14:leaf"})), "tab14:leaf")

    def test_a_refused_ack_names_the_scheduled_run_that_owns_the_default_cursor(self) -> None:
        # 11/10 09:05: after the Mac woke, the 02:00 tab was refused and thought itself the latest run.
        scheduled = int(datetime(2026, 11, 10, 7, tzinfo=timezone.utc).timestamp() * 1000)
        runs = [{"status": "dispatched", "terminalPaneKey": "tab07:leaf", "scheduledFor": scheduled,
                 "startedAt": scheduled + 54_000, "usage": {"providerSessionId": "36647b53"}},
                {"status": "completed", "terminalPaneKey": "tab02:leaf", "scheduledFor": scheduled - 5 * 3_600_000,
                 "usage": {"providerSessionId": "1b7ed179"}}]
        with tempfile.TemporaryDirectory() as root:
            inbox, cursor = Path(root) / "inbox.jsonl", Path(root) / "default.offset"
            write_jsonl(inbox, [{"kind": "ask", "text": "new question"}])
            transcript = Path(root) / ".claude/projects/-w-triage/36647b53.jsonl"
            write_jsonl(transcript, [user("/orca-session-triage hourly")])
            env = {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": "", "HOME": root,
                   "ORCA_WORKTREE_ID": "repo::/w/triage", "ORCA_PANE_KEY": "tab02:leaf"}
            with patch.dict(os.environ, env), patch.object(inbox_read, "paths", return_value=(inbox, cursor)), \
                    patch.object(hourly_runtime, "scheduled_runs", return_value=runs), \
                    redirect_stderr(io.StringIO()) as error:
                self.assertEqual(inbox_read.main(["ack", str(inbox.stat().st_size)]), 1)
        refusal = error.getvalue()
        self.assertIn(datetime.fromtimestamp(scheduled / 1000).astimezone().isoformat(timespec="seconds"), refusal)
        self.assertIn("dispatched", refusal)
        self.assertIn("pane tab07:leaf", refusal)
        self.assertIn(str(transcript), refusal)
        self.assertIn("not this tab (pane tab02:leaf)", refusal)

    def test_closed_latest_run_does_not_return_cursor_ownership_to_older_tab(self) -> None:
        runs = [{"status": "completed", "terminalPaneKey": None},
                {"status": "completed", "terminalPaneKey": "old-pane"}]
        self.assertIsNone(hourly_runtime.latest_pane(runs))

    def test_only_latest_scheduled_pane_advances_default_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "default.offset"
            write_jsonl(inbox, [{"kind": "ask", "text": "new question"}])
            cursor.write_text("0")
            env = {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": "",
                   "ORCA_WORKTREE_ID": "repo::/w/triage", "ORCA_PANE_KEY": "old-pane"}
            with patch.dict(os.environ, env), patch.object(inbox_read, "paths", return_value=(inbox, cursor)), \
                    patch.object(hourly_runtime, "scheduled_runs", return_value=[
                        {"status": "completed", "terminalPaneKey": "new-pane"},
                        {"status": "completed", "terminalPaneKey": "old-pane"}]):
                for options in ([], ["--summary"], ["--kind", "ask"]):
                    with self.subTest(options=options), redirect_stdout(io.StringIO()) as out, \
                            redirect_stderr(io.StringIO()):
                        self.assertEqual(inbox_read.main(["read", *options]), 0)
                    self.assertIn("new question", out.getvalue())
                    self.assertEqual(cursor.read_text(), "0")
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(inbox_read.main(["ack", str(inbox.stat().st_size)]), 1)
                self.assertIn("latest scheduled", error.getvalue())
                self.assertIn("inbox --since HH:MM --wt NAME", error.getvalue())
                self.assertEqual(cursor.read_text(), "0")
                os.environ["ORCA_PANE_KEY"] = "new-pane"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(inbox_read.main(["ack", str(inbox.stat().st_size)]), 0)
                self.assertEqual(int(cursor.read_text()), inbox.stat().st_size)

    def test_late_remote_report_is_read_even_with_an_old_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "read.offset"
            write_jsonl(inbox, [{"ts": "2026-10-25T10:00:00+09:00", "kind": "done", "text": "first"}])
            with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor)}):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(inbox_read.main(["initialize"]), 0)
                with inbox.open("a") as stream:
                    stream.write(json.dumps({"ts": "2026-10-24T10:00:00+09:00", "kind": "ask",
                                             "text": "late from remote"}) + "\n")
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox_read.main(["read"]), 0)
                self.assertIn("late from remote", output.getvalue())
                self.assertNotIn("first", output.getvalue())
                position = int(output.getvalue().split("NEXT_OFFSET=")[1])
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(inbox_read.main(["ack", str(position)]), 0)
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(inbox_read.main(["read"]), 0)
                self.assertEqual(output.getvalue(), f"NEXT_OFFSET={position}\n")


class InboxAlertTests(unittest.TestCase):
    def test_alerts_only_new_complete_asks_and_advances_its_own_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "alert.offset"
            write_jsonl(inbox, [{"kind": "ask", "worktree": "old", "text": "old"}])
            cursor.write_text(str(inbox.stat().st_size))
            with inbox.open("a") as stream:
                stream.write(json.dumps({"kind": "info", "worktree": "info"}) + "\n")
                stream.write(json.dumps({"kind": "ask", "worktree": "sample-lane", "text": "first\nsecret"}) + "\n")
                stream.write('{"kind":"ask","worktree":"partial"')
            seen = []
            inbox_alert.process(inbox, cursor, seen.append)
            self.assertEqual([x["worktree"] for x in seen], ["sample-lane"])
            self.assertEqual(inbox_alert.process(inbox, cursor, seen.append), int(cursor.read_text()))
            self.assertEqual(len(seen), 1)

    def test_notification_uses_the_first_line_as_arguments(self) -> None:
        item = {"kind": "ask", "worktree": "sample-lane", "text": "first line\nprivate second line"}
        with patch.object(inbox_alert.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            inbox_alert.notify(item, NoTab(), inbox_alert.AppleScript())
        args = run.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/osascript")
        self.assertIn("sample-lane", args[-2])
        self.assertEqual(args[-1], "first line")
        self.assertTrue(run.call_args.kwargs["check"])

    def test_pressing_the_notification_runs_the_senders_click_action(self) -> None:
        item = {"kind": "ask", "worktree": "sample-lane", "text": "first line\nprivate second line"}
        notifier = inbox_alert.TerminalNotifier("/n/terminal-notifier")
        switch = inbox_alert.Command(("/o/orca", "terminal", "switch", "--terminal", "term a"))
        cases = [(switch, "-execute", "/o/orca terminal switch --terminal 'term a'"),
                 (inbox_alert.Link("orca://terminal/term_a"), "-open", "orca://terminal/term_a")]
        for action, option, value in cases:
            with patch.object(inbox_alert.subprocess, "run") as run, redirect_stdout(io.StringIO()):
                inbox_alert.notify(item, NoTab(action), notifier)
            args = run.call_args.args[0]
            self.assertEqual(args[0], "/n/terminal-notifier")
            self.assertEqual(args[args.index("-message") + 1], "first line")
            self.assertEqual(args[args.index(option) + 1], value)
        with patch.object(inbox_alert.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            inbox_alert.notify(item, NoTab(), notifier)
        self.assertFalse({"-execute", "-open"} & set(run.call_args.args[0]))
        # Without notification permission terminal-notifier exits 3; the ask still shows.
        refused = subprocess.CalledProcessError(3, "terminal-notifier")
        with patch.object(inbox_alert.subprocess, "run", side_effect=[refused, None]) as run, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            inbox_alert.notify(item, NoTab(switch), notifier)
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/osascript")

    def test_orca_switches_to_the_sender_tab_on_the_mac_that_sent_the_ask(self) -> None:
        terminals = {
            None: [{"handle": "term_a", "connected": True, "agentIdentity": "claude", "worktreePath": "/w/repo/lane-a"},
                   {"handle": "term_b", "connected": True, "agentIdentity": "codex", "worktreePath": "/w/repo/lane-b"},
                   {"handle": "term_shell", "connected": True, "worktreePath": "/w/repo/lane-b"}],
            "laptop": [{"handle": "term_c", "connected": True, "agentIdentity": "claude",
                                "worktreePath": "/r/repo/lane-c"}],
        }
        machines = {"mini": "Mac-mini", "laptop": "Laptop-16"}
        tabs = inbox_alert.OrcaTabs("/o/orca", local_host="this-mac")
        items = [
            {"host": "this-mac", "worktree": "lane-a", "handle": "term_a"},
            {"worktree": "lane-a", "handle": "term_a"},
            # A closed tab's handle falls back to the one agent tab in that worktree.
            {"host": "this-mac", "worktree": "lane-b", "handle": "term_closed"},
            {"host": "Laptop-16.local", "worktree": "lane-c", "handle": "term_c"},
            {"host": "this-mac", "worktree": "gone", "handle": "term_closed"},
            {"host": "unpaired-mac", "worktree": "lane-c", "handle": "term_c"},
        ]
        with patch.object(inbox_alert.subprocess, "run", side_effect=fake_orca(terminals, machines)), \
                redirect_stderr(io.StringIO()):
            actions = [tabs.action(item) for item in items]
        switch = ("/o/orca", "terminal", "switch", "--terminal")
        self.assertEqual(actions, [
            inbox_alert.Command((*switch, "term_a")),
            inbox_alert.Command((*switch, "term_a")),
            inbox_alert.Command((*switch, "term_b")),
            inbox_alert.Command((*switch, "term_c", "--environment", "laptop")),
            None,
            None,
        ])

    def test_a_slow_paired_server_still_switches_to_the_reported_tab(self) -> None:
        answer = fake_orca({}, {"laptop": "Laptop-16"})
        calls = []

        def run(args: list[str], *, timeout: float, **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(args[1:3]))
            if calls[-1] == ("terminal", "list"):
                raise subprocess.TimeoutExpired(args, timeout)
            return answer(args, timeout=timeout, **kwargs)

        tabs = inbox_alert.OrcaTabs("/o/orca", local_host="this-mac")
        item = {"host": "Laptop-16.local", "worktree": "lane-c", "handle": "term_c"}
        with patch.object(inbox_alert.subprocess, "run", side_effect=run), redirect_stderr(io.StringIO()):
            actions = [tabs.action(item), tabs.action(item)]
        switch = inbox_alert.Command(("/o/orca", "terminal", "switch", "--terminal", "term_c",
                                      "--environment", "laptop"))
        self.assertEqual(actions, [switch, switch])
        # Each ask from that Mac waited again on the server's name before the cache.
        self.assertEqual(calls.count(("host", "name")), 1)


class NoTab:
    def __init__(self, action: str | None = None) -> None:
        self.result = action

    def action(self, item: dict) -> str | None:
        return self.result


def fake_orca(terminals: Mapping[str | None, list[dict]], machines: Mapping) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Answer the Orca CLI calls the alert makes, keyed by the --environment they target."""
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = args[args.index("--environment") + 1] if "--environment" in args else None
        command = tuple(args[1:3])
        if command == ("terminal", "list"):
            result = {"terminals": terminals[environment]}
        elif command == ("environment", "list"):
            result = {"environments": [{"name": name} for name in machines]}
        elif command == ("host", "name"):
            result = {"machineName": machines[environment]}
        else:
            raise AssertionError(f"unexpected orca call {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, "result": result}), "")
    return run


def published(url: str, live: str = "arming") -> dict:
    return tool_result(f"Published page.html at {url} (Version 1)\n\nLive subscription: {live} in the background",
                       {"url": url, "liveSubscription": live})


def unwatched(url: str) -> dict:
    return tool_result(f"Stopped watching {url}; republishes of it will no longer be reported in this session.",
                       {"unwatch": {"url": url, "was_watching": True}})


def watch_ended(url: str) -> dict:
    """Claude Code's notice that a watch ended on its own, as the connection kept failing."""
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": (
        "<task-notification>\n<task-type>artifact-watch-lifecycle</task-type>\n"
        f"<event>Watch on {url} ended — the live connection kept failing and reconnecting has stopped.</event>\n"
        "</task-notification>")}}


def blocking_decision(value: dict | None) -> dict:
    assert value is not None
    return value


class StopCheckTests(unittest.TestCase):
    """The Stop hook keeps a triage turn open while its review queue is not classed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "s1"
        self.dir.mkdir()
        (self.root / "inbox.jsonl").write_text("")
        (self.root / "read.offset").write_text("0")
        # The answer hook reads the hourly state and the inbox; a host's real ones must never reach a test run.
        for isolated in (patch.object(stopcheck, "HOURLY_STATE", str(self.root / "hourly-state.json")),
                         patch.dict(os.environ, {"TRIAGE_INBOX": str(self.root / "inbox.jsonl")})):
            isolated.start()
            self.addCleanup(isolated.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def queue(self, *paths: str, at: str = "2026-10-07T10:00:00+00:00") -> None:
        (self.dir / "review-queue.json").write_text(json.dumps(
            {"generated_at": at, "rows": [{"path": p} for p in paths]}))

    def result(self, *paths: str, at: str = "2026-10-07T10:00:00+00:00") -> None:
        (self.dir / "review-result.json").write_text(json.dumps(
            {"queue_generated_at": at, "rows": [{"path": p, "class": "blocked"} for p in paths]}))

    def check(self, active: bool = False, cwd: str | None = None, orca: str = "false", transcript: Path | None = None, watch_running: bool = True) -> dict | None:
        hook = {"session_id": "s1", "stop_hook_active": active}
        if cwd:
            hook["cwd"] = cwd
        if transcript:
            hook["transcript_path"] = str(transcript)
        paths = {"TRIAGE_INBOX": os.environ.get("TRIAGE_INBOX", str(self.root / "inbox.jsonl")),
                 "TRIAGE_READ_OFFSET": os.environ.get("TRIAGE_READ_OFFSET", str(self.root / "read.offset"))}
        with patch.dict(os.environ, paths), patch.object(stopcheck, "inbox_service_running", return_value=watch_running):
            return stopcheck.decide(hook, self.root, orca=orca)

    def fake_orca(self, automations: list[dict], runs: dict[str, list[dict]]) -> str:
        """An Orca CLI that answers `automations list` and `automations runs --id <id>`."""
        script = self.root / "orca"
        script.write_text(
            "#!/usr/bin/env python3\nimport json, sys\n"
            f"automations = json.loads({json.dumps(automations)!r})\n"
            f"runs = json.loads({json.dumps(runs)!r})\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['automations', 'list']:\n"
            "    print(json.dumps({'result': {'automations': automations}}))\n"
            "else:\n"
            "    print(json.dumps({'result': {'runs': runs.get(args[args.index('--id') + 1], [])}}))\n")
        script.chmod(0o755)
        return str(script)

    def test_lets_the_turn_end_without_a_queue_or_with_an_empty_one(self) -> None:
        self.assertIsNone(self.check())
        self.queue()
        self.assertIsNone(self.check())

    def test_hourly_question_hook_rejects_questions_without_kana_or_kanji(self) -> None:
        orca = self.fake_orca([triage_automation("a1", "/w/triage")], {"a1": [
            {"status": "dispatched", "trigger": "scheduled", "terminalPaneKey": "current",
             "workspaceId": "repo1::/w/triage", "startedAt": 1793005200000}]})
        japanese = {"question": "公開しますか？", "options": [{"label": "公開", "description": "今日出します"}]}
        question = {"question": "Which option?", "header": "方針",
                    "options": [{"label": "Keep", "description": "Leave it as it is"}]}
        for field in (None, "description"):
            with self.subTest(field=field):
                asked = json.loads(json.dumps(question))
                if field:
                    asked["options"][0][field] = "今のままにします"
                hook = {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": "s1",
                        "cwd": "/w/triage", "tool_input": {"questions": [japanese, asked]}}
                with patch.dict(os.environ, {"ORCA_PANE_KEY": "current", "ORCA_CLI_COMMAND": orca}), \
                        patch.object(stopcheck, "STATE_DIR", str(self.root)), \
                        patch.object(stopcheck.sys, "stdin", io.StringIO(json.dumps(hook))), \
                        redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(stopcheck.main(self.root), 0)
                if field is None:
                    decision = json.loads(out.getvalue())["hookSpecificOutput"]
                    self.assertEqual((decision["hookEventName"], decision["permissionDecision"]),
                                     ("PreToolUse", "deny"))
                    self.assertIn("日本語", decision["permissionDecisionReason"])
                else:
                    self.assertEqual(out.getvalue(), "")

    def test_answer_hook_asks_to_check_a_note_on_an_outward_choice(self) -> None:
        post = {"question": "TOUGOU_KAIKEI-799 の返事を投稿しますか？", "header": "799 投稿", "multiSelect": False,
                "options": [{"label": "下書きのとおり投稿 (Recommended)", "description": "吉岡さんに返事を出します"},
                            {"label": "直してから", "description": "下書きを直させます"}]}
        record = {"question": "記録だけにしますか？", "header": "記録", "multiSelect": False,
                  "options": [{"label": "書かずに記録", "description": "台帳に残すだけ"},
                              {"label": "書く", "description": "skill に書きます"}]}

        def answer(answers: dict[str, str], notes: dict[str, str]) -> str:
            hook = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "session_id": "s1",
                    "cwd": "/w/triage", "tool_input": {"questions": [post, record]},
                    "tool_response": {"questions": [post, record], "answers": answers,
                                      "annotations": {question: {"notes": note} for question, note in notes.items()}}}
            with patch.object(stopcheck.sys, "stdin", io.StringIO(json.dumps(hook))), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(stopcheck.main(self.root), 0)
            return out.getvalue()

        chosen = {post["question"]: "下書きのとおり投稿 (Recommended)", record["question"]: "書かずに記録"}
        decision = json.loads(answer(chosen, {post["question"]: "不要."}))
        self.assertEqual(decision["decision"], "block")
        self.assertIn("799 投稿", decision["reason"])
        self.assertIn("不要.", decision["reason"])
        self.assertIn("1 問で確かめ", decision["reason"])
        for answers, notes in ((chosen, {}), (chosen, {record["question"]: "あとで見る"}),
                               ({post["question"]: "(notes only)"}, {post["question"]: "不要."})):
            with self.subTest(notes=notes, answers=answers):
                self.assertEqual(answer(answers, notes), "")

    def test_answer_hook_names_a_later_ask_that_withdrew_or_names_the_answered_ask(self) -> None:
        # 2026-11-11: an answer to an ask withdrawn and asked again with other choices was recorded and relayed.
        old, new = "8ddb8b28-0000-ask", "cbb319be-0000-ask"
        write_jsonl(self.root / "inbox.jsonl", [
            {"id": old, "ts": "2026-11-10T22:05:00+0900", "kind": "ask", "worktree": "lane", "text": "merge しますか？"},
            {"id": new, "ts": "2026-11-10T22:25:00+0900", "kind": "ask", "worktree": "lane",
             "text": f"ask {old[:8]} を直します。今の検証で merge しますか？"}])
        (self.root / "hourly-state.json").write_text(json.dumps({"version": 1, "left": {}, "handoffs": {}, "questions": {
            "q1": {"row": "/w/lane", "decision": f"ask:app/lane::{old[:8]}:1", "question": "infra-aws#526 を merge しますか？",
                   "asked_at": "2026-11-10T13:10:00+00:00", "session": "s1", "answer": None}}}))
        question = {"question": "CI が通ったら merge しますか？", "header": "merge",
                    "options": [{"label": "CI が通ったら進める", "description": "CI を待つ"}]}
        later = {"question": "今夜の実行を止めますか？", "header": "実行",
                 "options": [{"label": "止める", "description": "今夜は流さない"}]}
        transcript = self.root / "s1.jsonl"
        write_jsonl(transcript, [
            {**assistant({"type": "tool_use", "name": "AskUserQuestion", "id": "toolu_a", "input": {"questions": [question]}}),
             "timestamp": "2026-11-10T13:10:30.000Z"},
            {**user([{"type": "tool_result", "tool_use_id": "toolu_a", "content": "CI が通ったら進める"}]),
             "timestamp": "2026-11-10T13:11:00.000Z"},
            {**assistant({"type": "tool_use", "name": "AskUserQuestion", "id": "toolu_b", "input": {"questions": [later]}}),
             "timestamp": "2026-11-10T13:20:00.000Z"}])

        def decided(asked: dict, call: str) -> dict | None:
            hook = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "session_id": "s1",
                    "cwd": "/w/triage", "transcript_path": str(transcript), "tool_use_id": call,
                    "tool_input": {"questions": [asked]},
                    "tool_response": {"questions": [asked], "answers": {asked["question"]: asked["options"][0]["label"]}}}
            with patch.object(stopcheck.sys, "stdin", io.StringIO(json.dumps(hook))), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(stopcheck.main(self.root), 0)
            return json.loads(out.getvalue()) if out.getvalue() else None

        decision = decided(question, "toolu_a") or {}
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn(f"ask {old[:8]}（infra-aws#526 を merge しますか？）→ ask {new[:8]}", decision["reason"])
        # The record went with the earlier call: an answer to another question in the same session names nothing.
        self.assertIsNone(decided(later, "toolu_b"))

    def test_hourly_question_hook_requires_a_handoff_from_this_run(self) -> None:
        orca = self.fake_orca([triage_automation("a1", "/w/triage")], {"a1": [
            {"status": "completed", "trigger": "scheduled", "terminalPaneKey": "older",
             "workspaceId": "repo1::/w/triage", "startedAt": 1793001600000},
            {"status": "dispatched", "trigger": "scheduled", "terminalPaneKey": "current",
             "workspaceId": "repo1::/w/triage", "startedAt": 1793005200000}]})
        hook = {"tool_name": "AskUserQuestion", "session_id": "s1", "cwd": "/w/triage",
                "tool_input": {"questions": [{"question": "どうしますか？",
                    "options": [{"label": "残す", "description": "今のままにします"}]}]}}
        state = self.root / "state"
        state.mkdir()
        with patch.dict(os.environ, {"ORCA_PANE_KEY": "current"}):
            # With no lead appointed, the run asks the user itself.
            self.assertIsNone(stopcheck.question_decision(hook, orca, state, self.root))
            (state / "hourly-state.json").write_text(json.dumps({"lead": {"worktree": "/w/lead"}}))
            self.assertEqual(stopcheck.question_decision(hook, orca, state, self.root)["hookSpecificOutput"]
                             ["permissionDecision"], "deny")
            for stamp in ("2026-10-26T08:59:59+00:00", "2026-10-26T09:00:00+00:00",
                          "2026-10-26T09:00:01+00:00"):
                with self.subTest(stamp=stamp):
                    sent = datetime.fromisoformat(stamp)
                    stopcheck.handoff.SavedHandoff("引き継ぎ", "ask", [], stamp, 0).save(self.root, sent)
                    decision = stopcheck.question_decision(hook, orca, state, self.root)
                    self.assertEqual(decision is None, sent.hour >= 9)
            self.assertIsNone(stopcheck.question_decision({**hook, "tool_name": "Bash"}, orca, state, self.root))
            self.assertIsNone(stopcheck.question_decision({**hook, "cwd": "/w/other"}, orca, state, self.root))

    def test_hourly_question_hook_uses_session_start_when_the_run_has_no_pane(self) -> None:
        orca = self.fake_orca([triage_automation("a1", "/w/triage")], {"a1": [
            {"status": "completed", "trigger": "scheduled", "terminalPaneKey": None,
             "workspaceId": "repo1::/w/triage", "startedAt": 1793001600000}]})
        config = self.root / "claude"
        (config / "sessions").mkdir(parents=True)
        (config / "sessions/123.json").write_text(json.dumps({"sessionId": "s1", "startedAt": 1793005200000}))
        hook = {"tool_name": "AskUserQuestion", "session_id": "s1", "cwd": "/w/triage",
                "tool_input": {"questions": [{"question": "保存しますか？",
                    "options": [{"label": "保存", "description": "ファイルを保存します"}]}]}}
        (config / "hourly-state.json").write_text(json.dumps({"lead": {"worktree": "/w/lead"}}))
        with patch.dict(os.environ, {"ORCA_PANE_KEY": "current", "CLAUDE_CONFIG_DIR": str(config),
                                     "CLAUDE_PID": "123"}):
            for stamp in ("2026-10-26T08:59:59+00:00", "2026-10-26T09:00:00+00:00"):
                with self.subTest(stamp=stamp):
                    sent = datetime.fromisoformat(stamp)
                    stopcheck.handoff.SavedHandoff("引き継ぎ", "ask", [], stamp, 0).save(self.root, sent)
                    decision = stopcheck.question_decision(hook, orca, config, self.root)
                    self.assertEqual(decision is None, sent.hour == 9)

    def test_blocks_while_queued_rows_are_not_classed(self) -> None:
        self.queue("/w/a", "/w/b")
        blocked = self.check()
        assert blocked is not None
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("/w/a", blocked["reason"])
        self.assertIn("orca-row-classify", blocked["reason"])

        self.result("/w/a")
        self.assertIn("/w/b", blocking_decision(self.check())["reason"])
        self.assertNotIn("/w/a", blocking_decision(self.check())["reason"])

    def test_a_result_for_an_older_queue_does_not_count(self) -> None:
        self.result("/w/a", at="2026-10-07T09:00:00+00:00")
        self.queue("/w/a")
        self.assertEqual(blocking_decision(self.check())["decision"], "block")

    def test_lets_the_turn_end_once_every_row_is_classed(self) -> None:
        self.queue("/w/a", "/w/b")
        self.result("/w/a", "/w/b")
        self.assertIsNone(self.check())

    def test_unread_ask_blocks_even_on_repeated_stop_until_ack(self) -> None:
        inbox = self.root / "inbox.jsonl"
        cursor = self.root / "read.offset"
        first = {"kind": "info", "worktree": "earlier", "text": "progress"}
        write_jsonl(inbox, [first])
        cursor.write_text(str(inbox.stat().st_size))
        with inbox.open("a") as stream:
            stream.write(json.dumps({"kind": "ask", "worktree": "sample-lane", "text": "question"}) + "\n")
            stream.write('{"kind":"ask","worktree":"partial"')
        orca = self.fake_orca([triage_automation("a1", "/w/triage")], {"a1": [
            {"status": "completed", "trigger": "scheduled", "terminalPaneKey": "new-pane",
             "workspaceId": "repo1::/w/triage"}]})
        with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor),
                                  "ORCA_PANE_KEY": "new-pane", "ORCA_WORKTREE_ID": "repo1::/w/triage"}):
            for active in (False, True):
                result = self.check(active=active, cwd="/w/triage", orca=orca)
                assert result is not None
                self.assertEqual(result["decision"], "block")
                self.assertIn("sample-lane", result["reason"])
                self.assertNotIn("partial", result["reason"])
            os.environ["ORCA_PANE_KEY"] = "old-pane"
            self.assertIsNone(self.check(active=True, cwd="/w/triage", orca=orca))
            os.environ["ORCA_PANE_KEY"] = "new-pane"
            cursor.write_text(str(inbox.stat().st_size - len('{"kind":"ask","worktree":"partial"')))
            self.assertIsNone(self.check(active=True, cwd="/w/triage", orca=orca))

    def test_an_ask_the_scheduled_tab_sent_itself_is_not_unread(self) -> None:
        inbox = self.root / "inbox.jsonl"
        cursor = self.root / "read.offset"
        write_jsonl(inbox, [{"kind": "ask", "host": "mac-a", "worktree": "triage", "text": "定時の引き継ぎ"}])
        cursor.write_text("0")
        orca = self.fake_orca([triage_automation("a1", "/w/triage")], {"a1": [
            {"status": "completed", "trigger": "scheduled", "terminalPaneKey": "new-pane",
             "workspaceId": "repo1::/w/triage"}]})
        with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(cursor),
                                     "ORCA_PANE_KEY": "new-pane", "ORCA_WORKTREE_ID": "repo1::/w/triage",
                                     "TRIAGE_HOST": "mac-a"}):
            self.assertIsNone(self.check(active=True, cwd="/w/triage", orca=orca))
            with inbox.open("a") as stream:
                stream.write(json.dumps({"kind": "ask", "host": "mac-b", "worktree": "triage", "text": "別の Mac"}) + "\n")
            self.assertEqual(blocking_decision(self.check(active=True, cwd="/w/triage", orca=orca))["decision"], "block")

    def test_blocks_once_then_lets_a_repeated_stop_through(self) -> None:
        self.queue("/w/a")
        self.assertEqual(blocking_decision(self.check())["decision"], "block")
        self.assertIsNone(self.check(active=True))

    def test_a_verdict_without_a_known_class_does_not_count(self) -> None:
        self.queue("/w/a")
        (self.dir / "review-result.json").write_text(json.dumps(
            {"queue_generated_at": "2026-10-07T10:00:00+00:00", "rows": [{"path": "/w/a", "class": "maybe"}]}))
        self.assertEqual(blocking_decision(self.check())["decision"], "block")

    def test_warns_when_the_external_inbox_service_is_stopped(self) -> None:
        self.queue()
        orca = self.fake_orca([triage_automation("a1", "/w/triage")], {"a1": []})
        reason = blocking_decision(self.check(cwd="/w/triage", orca=orca, watch_running=False))["reason"]
        self.assertIn("watch-inbox", reason)
        self.assertIn("~/.cache/orca-briefs/offsets/close-done-triage.offset", reason)
        self.assertIsNone(self.check(cwd="/w/triage", orca=orca, watch_running=True))

    def test_monitor_in_another_tab_does_not_replace_external_service(self) -> None:
        with patch.object(stopcheck, "triage_automations", return_value=[{"id": "a1"}]), \
                patch.object(stopcheck, "inbox_service_running", return_value=False):
            reason = stopcheck.missing_inbox_watch("/w/triage", "orca")
        self.assertIn("external triage inbox service", reason)

    def test_external_service_status_controls_stop_notice(self) -> None:
        with patch.object(stopcheck.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "running")) as run:
            self.assertTrue(stopcheck.inbox_service_running())
        self.assertEqual(run.call_args.args[0][-1], "status")
        with patch.object(stopcheck.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1, "stopped")):
            self.assertFalse(stopcheck.inbox_service_running())

    def test_blocks_while_an_artifact_this_session_published_is_still_watched(self) -> None:
        transcript = self.root / "session.jsonl"
        write_jsonl(transcript, [published("https://claude.ai/artifact/A"),
                                 published("https://claude.ai/artifact/B", live="flag_off")])

        blocked = self.check(transcript=transcript)
        assert blocked is not None

        self.assertEqual(blocked["decision"], "block")
        self.assertIn("https://claude.ai/artifact/A", blocked["reason"])
        self.assertNotIn("artifact/B", blocked["reason"])
        self.assertIn("ArtifactComments", blocked["reason"])
        self.assertIsNone(self.check(active=True, transcript=transcript))

        def append(entry: dict) -> None:
            with transcript.open("a") as f:
                f.write(json.dumps(entry) + "\n")

        append(user("a later turn that never stops the watch"))
        self.assertEqual(blocking_decision(self.check(transcript=transcript))["decision"], "block")
        append(unwatched("https://claude.ai/artifact/A"))
        self.assertIsNone(self.check(transcript=transcript))

    def test_a_watch_that_ended_on_its_own_lets_the_turn_end(self) -> None:
        transcript = self.root / "session.jsonl"
        write_jsonl(transcript, [published("https://claude.ai/artifact/A"), watch_ended("https://claude.ai/artifact/A")])

        self.assertIsNone(self.check(transcript=transcript))

    def test_a_damaged_queue_or_result_lets_the_turn_end(self) -> None:
        (self.dir / "review-queue.json").write_text(json.dumps({"generated_at": "g", "rows": [{"nopath": 1}]}))
        self.assertIsNone(self.check())
        (self.dir / "review-queue.json").write_text("[]")
        self.assertIsNone(self.check())


class BashTimeoutHookTests(unittest.TestCase):
    """On a heavily loaded Mac, a Bash call runs with the longest timeout instead of moving to the background."""

    def hook_output(self, load: float, tool_input: dict) -> str:
        hook = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1",
                "cwd": "/w/triage", "tool_input": tool_input}
        with patch.object(bash_timeout.os, "getloadavg", return_value=(load, 1.0, 1.0)), \
                patch.object(bash_timeout.sys, "stdin", io.StringIO(json.dumps(hook))), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bash_timeout.main(), 0)
        return out.getvalue()

    def test_a_heavy_load_returns_the_call_with_only_its_timeout_raised(self) -> None:
        call = {"command": "python3 handoff.py last", "description": "Read the last handoff"}
        for given in ({}, {"timeout": 120000}):
            with self.subTest(given=given):
                self.assertEqual(json.loads(self.hook_output(390.0, {**call, **given})),
                                 {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                         "updatedInput": {**call, "timeout": 600000}}})

    def test_a_light_load_or_the_longest_timeout_leaves_the_call_alone(self) -> None:
        call = {"command": "ls", "description": "List files"}
        self.assertEqual(self.hook_output(100.0, call), "")
        self.assertEqual(self.hook_output(390.0, {**call, "timeout": 600000}), "")

    def test_worktree_removal_gets_ten_minutes_even_at_low_load(self) -> None:
        command = "~/.cache/orca-briefs/bin/rm-check-batch --remove-ok --summary '/w/done -'"
        call = {"command": command, "timeout": 120000}
        self.assertEqual(json.loads(self.hook_output(1.0, call))["hookSpecificOutput"]["updatedInput"],
                         {**call, "timeout": 600000})
        self.assertEqual(self.hook_output(1.0, {"command": command.replace("--remove-ok", "")}), "")
        self.assertEqual(self.hook_output(1.0, {**call, "run_in_background": True}), "")

    def test_a_call_started_in_the_background_keeps_its_input(self) -> None:
        # Its timeout would set how long it may run in the background, not keep it in the foreground.
        self.assertEqual(self.hook_output(390.0, {"command": "ls", "run_in_background": True}), "")


class VerdictStoreTests(unittest.TestCase):
    """A careful read is kept only while what it read still holds."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root / "verdicts.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def row(path: str, final_at: str | None = "t1", pr: dict | None = None, dirty: int = 0, cls: str = "stalled", codex_at: str | None = None) -> dict:
        return {"path": path, "class": cls, "jev": {"asks": 0.1}, "pr": pr, "git": {"dirty": dirty},
                "claude": {"final_at": final_at} if final_at else None,
                "codex": [{"last_assistant_at": codex_at}] if codex_at else []}

    def test_a_verdict_lapses_when_the_pr_or_the_tree_changed_or_nothing_was_read(self) -> None:
        open_pr = {"state": "OPEN", "number": 7, "head_oid": "a"}
        read = self.row("/w/a", pr=open_pr)
        verdicts = {"/w/a": {"read": scan.read_key(read), "class": "blocked"},
                    "/w/none": {"read": scan.read_key(self.row("/w/none", final_at=None)), "class": "blocked"}}

        self.assertEqual(scan.review_queue([read], verdicts), [])
        for changed in (self.row("/w/a", pr={**open_pr, "state": "MERGED"}),
                        self.row("/w/a", pr={**open_pr, "head_oid": "b"}),
                        self.row("/w/a", pr=open_pr, dirty=2),
                        self.row("/w/a", pr=open_pr, final_at="t2"),
                        {**self.row("/w/a", pr=open_pr), "comment": "解消しました"}):
            self.assertEqual([e["path"] for e in scan.review_queue([changed], verdicts)], ["/w/a"])
        self.assertEqual([e["path"] for e in scan.review_queue([self.row("/w/none", final_at=None)], verdicts)],
                         ["/w/none"])
        self.assertNotIn("verdict", scan.with_verdicts([self.row("/w/none", final_at=None)], verdicts)[0])
        self.assertNotIn("verdict", scan.with_verdicts([{**read, "class": "working"}], verdicts)[0])
        self.assertEqual(scan.with_verdicts([read], verdicts)[0]["verdict"]["class"], "blocked")

    def test_an_inbox_ask_carries_its_answer_once_every_question_has_one(self) -> None:
        # masters-ui-17: three questions answered at 20:30 came back as the row's question.
        inbox, answers = self.root / "inbox.jsonl", self.root / "answers.jsonl"
        write_jsonl(inbox, [{"id": "ask-1", "ts": "2026-10-28T20:00:00+09:00", "kind": "ask",
                            "repo": "app", "worktree": "ask", "text": "Two choices", "questions": [{}, {}]}])
        first = {"id": "ask-1:1", "ts": "2026-10-28T20:30:00+09:00", "note": "問い1 このまま入れる"}
        write_jsonl(answers, [first])
        partial = scan.inbox_reports(inbox, answers)[("app", "ask")]["ask"]
        write_jsonl(answers, [first, {"id": "ask-1:2", "ts": "2026-10-28T20:31:00+09:00", "note": "問い2 先に出す"}])
        complete = scan.inbox_reports(inbox, answers)[("app", "ask")]["ask"]

        self.assertNotIn("answered", partial)
        self.assertEqual(complete["answered"], {"at": "2026-10-28T20:31:00+09:00", "note": "問い2 先に出す"})
        row = {**self.row("/w/app/ask"), "repo": "app"}
        self.assertNotEqual(scan.read_key({**row, "inbox": {"ask": partial}}),
                            scan.read_key({**row, "inbox": {"ask": complete}}))

    def test_a_remote_screen_reply_without_a_time_keeps_its_verdict_in_saved_views(self) -> None:
        # MBP16's test-338 and test-339 were classed, yet --rows kept them unsure: their queue rows had read null.
        row = {**self.row("/remote/test-338", final_at=None, cls="unsure"), "environment": "macbook-pro-16",
               "orca_message": {"text": "Reported completion to the coordinator.", "at": None, "from": "screen"}}
        [queued] = scan.review_queue([row])
        self.write_session("screen-read", "g1", [queued], "g1", [
            {"path": row["path"], "identity": queued["identity"], "class": "blocked"}])
        scan.absorb_verdicts(self.store)
        saved = self.root / "scan.json"
        saved.write_text(json.dumps([row]))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--verdicts", str(self.store), "--rows"]), 0)

        self.assertIn("blocked (scan: unsure)", out.getvalue())

    def test_a_new_human_review_returns_a_row_to_the_review_queue(self) -> None:
        pr = {"state": "OPEN", "number": 42, "head_oid": "a",
              "latest_human_review": {"author": "reviewer", "state": "COMMENTED",
                                      "submitted_at": "2026-10-21T01:00:00Z"}}
        read = self.row("/w/review-lane", pr=pr)
        verdicts = {read["path"]: {"read": scan.read_key(read), "class": "blocked"}}
        self.assertEqual(scan.review_queue([read], verdicts), [])

        changed = self.row(read["path"], pr={**pr, "latest_human_review": {
            "author": "reviewer", "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-10-21T02:00:00Z"}})
        self.assertEqual([row["path"] for row in scan.review_queue([changed], verdicts)], [read["path"]])

    def test_remote_and_local_rows_with_the_same_path_keep_separate_verdicts(self) -> None:
        local = self.row("/same/path")
        remote = {**self.row("/same/path"), "environment": "macbook-pro-16"}
        queued = scan.review_queue([local, remote])
        self.assertNotIn("identity", queued[0])
        self.assertEqual(queued[1]["identity"], "macbook-pro-16::/same/path")
        self.assertEqual([scan.row_identity(local), scan.row_identity(remote)],
                         ["/same/path", "macbook-pro-16::/same/path"])
        verdicts = {scan.row_identity(local): {"read": scan.read_key(local), "class": "blocked"}}
        self.assertEqual([entry.get("identity") for entry in scan.review_queue([local, remote], verdicts)],
                         ["macbook-pro-16::/same/path"])

        session = self.root / "remote-session"
        session.mkdir()
        (session / "review-queue.json").write_text(json.dumps(
            {"generated_at": "g1", "rows": queued}))
        (session / "review-result.json").write_text(json.dumps(
            {"queue_generated_at": "g1", "rows": [
                {"path": "/same/path", "class": "blocked"},
                {"path": "/same/path", "identity": queued[1]["identity"], "class": "in_progress"}]}))
        self.assertEqual({key: value["class"] for key, value in scan.classed_queue(session).items()},
                         {"/same/path": "blocked", "macbook-pro-16::/same/path": "in_progress"})

    def test_a_new_pr_comment_returns_a_row_to_the_review_queue(self) -> None:
        old = self.row("/w/review-lane", pr={"state": "OPEN", "number": 42,
                                             "latest_human_activity": None})
        verdicts = {old["path"]: {"read": scan.read_key(old), "class": "blocked"}}
        updated = self.row(old["path"], pr={"state": "OPEN", "number": 42,
                                           "latest_human_activity": {"author": "reviewer",
                                                                     "kind": "comment",
                                                                     "at": "2026-10-21T02:00:00Z"}})
        self.assertEqual(scan.review_queue([old], verdicts), [])
        self.assertEqual([row["path"] for row in scan.review_queue([updated], verdicts)], [old["path"]])

    def test_the_newer_of_the_claude_and_codex_messages_is_what_was_read(self) -> None:
        self.assertEqual(scan.row_final_at(self.row("/w", final_at="2026-10-07T01:00:00Z",
                                                     codex_at="2026-10-07T02:00:00Z")), "2026-10-07T02:00:00Z")
        self.assertEqual(scan.row_final_at(self.row("/w", final_at="2026-10-07T03:00:00Z",
                                                     codex_at="2026-10-07T02:00:00Z")), "2026-10-07T03:00:00Z")

    def write_session(self, name: str, queue_at: str, rows: list[dict], result_at: str, verdicts: list[dict]) -> None:
        session = self.root / name
        session.mkdir()
        (session / "review-queue.json").write_text(json.dumps({"generated_at": queue_at, "rows": rows}))
        (session / "review-result.json").write_text(json.dumps({"queue_generated_at": result_at, "rows": verdicts}))

    def test_every_session_s_classed_queue_feeds_the_store_and_bad_verdicts_do_not(self) -> None:
        self.write_session("s1", "g1", [{"path": "/w/a", "read": "ka"}, {"path": "/w/b", "read": "kb"}], "g1",
                           [{"path": "/w/a", "class": "blocked", "read": "forged"},
                            {"path": "/w/b", "class": "made-up"}])
        self.write_session("s2", "g2", [{"path": "/w/c", "read": "kc"}], "g2",
                           [{"path": "/w/c", "class": "finished"}])
        self.write_session("s3", "g3", [{"path": "/w/d", "read": "kd"}], "older",
                           [{"path": "/w/d", "class": "finished"}])
        self.store.write_text(json.dumps({"/w/old": {"read": "ko", "class": "finished"}}))

        verdicts = scan.absorb_verdicts(self.store)

        self.assertEqual(verdicts, {"/w/old": {"read": "ko", "class": "finished"},
                                    "/w/a": {"class": "blocked", "read": "ka", "queued_at": "g1"},
                                    "/w/c": {"class": "finished", "read": "kc", "queued_at": "g2"}})
        self.assertEqual(json.loads(self.store.read_text()), verdicts)

    def test_hourly_read_verdicts_feed_saved_rows_and_removal_specs_before_the_next_scan(self) -> None:
        row = {**self.row("/w/app/done", cls="unsure"), "repo": "app", "meaningful_tabs": []}
        queued = scan.review_queue([row])
        (self.root / "reads").mkdir()
        self.write_session("reads/run", "2026-11-14T00:00:00Z", queued, "2026-11-14T00:00:00Z",
                           [{"path": row["path"], "class": "finished"}])
        saved = self.root / "reads/run/scan.json"
        saved.write_text(json.dumps([row]))
        for options, expected in ((["--rows"], "finished (scan: unsure)"),
                                  (["--rm-specs"], "/w/app/done -")):
            with self.subTest(options=options), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scan.main(["--from", str(saved), "--verdicts", str(self.store), *options]), 0)
            self.assertIn(expected, out.getvalue())
        self.assertFalse(self.store.exists(), "--from must not write the verdict store")
        self.assertEqual(scan.absorb_verdicts(self.store)[row["path"]]["class"], "finished")

    def test_an_older_session_s_read_does_not_replace_a_newer_one_in_the_store(self) -> None:
        self.write_session("old", "2026-10-08T12:34:54+00:00", [{"path": "/w/a", "read": "k-old"}],
                           "2026-10-08T12:34:54+00:00", [{"path": "/w/a", "class": "blocked"}])
        self.write_session("new", "2026-10-16T00:08:40+00:00", [{"path": "/w/a", "read": "k-new"}],
                           "2026-10-16T00:08:40+00:00", [{"path": "/w/a", "class": "blocked"}])
        os.utime(self.root / "old" / "review-queue.json", (1, 1))
        self.assertEqual(scan.absorb_verdicts(self.store)["/w/a"]["read"], "k-new")

        # The next scan found /w/a still as read, so the new session's queue no longer holds it.
        (self.root / "new" / "review-queue.json").write_text(
            json.dumps({"generated_at": "2026-10-16T01:08:40+00:00", "rows": []}))
        (self.root / "new" / "review-result.json").write_text(
            json.dumps({"queue_generated_at": "2026-10-16T01:08:40+00:00", "rows": []}))

        self.assertEqual(scan.absorb_verdicts(self.store)["/w/a"]["read"], "k-new")

    def test_a_damaged_store_is_kept_aside_not_overwritten(self) -> None:
        self.store.write_text("[1, 2")
        self.write_session("s1", "g1", [{"path": "/w/a", "read": "ka"}], "g1", [{"path": "/w/a", "class": "blocked"}])

        verdicts = scan.absorb_verdicts(self.store)

        self.assertEqual(verdicts, {"/w/a": {"class": "blocked", "read": "ka", "queued_at": "g1"}})
        self.assertEqual((self.root / "verdicts.json.bad").read_text(), "[1, 2")

if __name__ == "__main__":
    unittest.main()
