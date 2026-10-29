"""Offline behavior tests for the orca-session-triage command line tools."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from functools import partial
import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "orca-session-triage" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = load_script("scan_sessions")
rmcheck = load_script("rm_check")
merged_pr_head = load_script("merged_pr_head")
precheck = load_script("precheck")
hourly_runtime = sys.modules["hourly_runtime"]
hourly_state = load_script("hourly_state")
stopcheck = load_script("stop_check")
inbox_service = load_script("inbox_watch_service")
inbox_read = load_script("inbox_read")
cleanup_hourly = load_script("cleanup_hourly_tabs")
hourly_runs = load_script("hourly_runs")
inbox_alert = load_script("inbox_alert")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def text(value):
    return {"type": "text", "text": value}


def each_ref(lookup):
    """A repository's PR lookup built from one that answers a single ref."""
    return lambda slug, refs: {ref: lookup(slug, ref) for ref in refs}


def pr_node(number, branch="fix", state="OPEN", updated_at="2026-10-28T00:00:00Z", **fields):
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
    other aliases keep their data. A REST read answers from `activity` by endpoint.
    """

    def __init__(self, repos, activity=None):
        self.repos, self.activity, self.calls = repos, activity or {}, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:3] != ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.activity.get(argv[-1], [])]), "")
        query = next(arg for arg in argv if arg.startswith("query=")).removeprefix("query=")
        owner, name = re.search(r'repository\(owner: ("[^"]*"), name: ("[^"]*")\)', query).groups()
        nodes = sorted(self.repos.get(json.loads(owner) + "/" + json.loads(name), []), key=lambda n: -n["number"])
        repository, errors = {"defaultBranchRef": {"name": "main"}}, []
        for alias, number in re.findall(r"(\w+): pullRequest\(number: (\d+)\)", query):
            repository[alias] = next((n for n in nodes if n["number"] == int(number)), None)
            if repository[alias] is None:
                errors.append({"type": "NOT_FOUND", "path": ["repository", alias]})
        for alias, branch in re.findall(r'(\w+): pullRequests\(headRefName: ("(?:[^"\\]|\\.)*")', query):
            repository[alias] = {"nodes": [n for n in nodes if n["headRefName"] == json.loads(branch)][:5]}
        body = {"data": {"repository": repository}, **({"errors": errors} if errors else {})}
        return subprocess.CompletedProcess(argv, 1 if errors else 0, json.dumps(body), "")

    def graphql(self):
        return [call for call in self.calls if call[:3] == ["gh", "api", "graphql"]]

    def rest(self):
        return [call for call in self.calls if call[:3] != ["gh", "api", "graphql"]]


class ClaudeTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)
        self.worktree = "/Users/me/orca/workspaces/app/fix.login_2"

    def tearDown(self):
        self.tmp.cleanup()

    def transcript(self, rows, name="s1.jsonl"):
        path = self.projects / "-Users-me-orca-workspaces-app-fix-login-2" / name
        write_jsonl(path, rows)
        return path

    def test_reads_final_answer_after_last_real_user_message(self):
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

    def test_a_compaction_summary_is_not_the_users_last_word(self):
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

    def test_lists_ask_user_question_without_a_result(self):
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

    def test_skips_entries_whose_message_is_not_an_object(self):
        self.transcript([
            user("go"),
            {"type": "assistant", "message": None},
            {"type": "assistant", "message": "plain string"},
            assistant(text("still readable")),
        ])

        self.assertEqual(scan.read_claude(self.projects, self.worktree)["final_text"], "still readable")

    def test_ignores_lines_that_are_json_but_not_objects(self):
        path = self.transcript([user("go"), assistant(text("answer"))])
        with Path(path).open("a") as handle:
            handle.write("null\n5\n\"text\"\n")

        self.assertEqual(scan.read_claude(self.projects, self.worktree)["final_text"], "answer")

    def test_finds_the_truncated_directory_of_a_long_path(self):
        long_path = "/Users/me/" + "deep-directory/" * 16 + "wt"
        folder = self.projects / (scan.claude_project_dir("", long_path).name[:200] + "-x1y2z3")
        write_jsonl(folder / "s.jsonl", [dict(user("go"), cwd=long_path), assistant(text("found"))])
        write_jsonl(self.projects / (folder.name[:-6] + "other") / "s.jsonl",
                    [dict(user("go"), cwd=long_path + "-sibling"), assistant(text("wrong"))])

        self.assertEqual(scan.read_claude(self.projects, long_path)["final_text"], "found")

    def test_returns_none_without_a_transcript(self):
        self.assertIsNone(scan.read_claude(self.projects, "/nowhere"))


def codex_row(ts, kind, payload):
    return {"timestamp": ts, "type": kind, "payload": payload}


def codex_user(ts, value):
    return codex_row(ts, "response_item", {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": value}]})


def codex_assistant(ts, value):
    return codex_row(ts, "response_item", {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": value}]})


def codex_ask(ts, call_id, questions):
    return codex_row(ts, "response_item", {
        "type": "function_call", "name": "request_user_input_async", "call_id": call_id,
        "arguments": json.dumps({"questions": questions})})


class CodexRolloutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "sessions"

    def tearDown(self):
        self.tmp.cleanup()

    def rollout(self, name, cwd, rows):
        path = self.root / "2026" / "10" / "01" / name
        write_jsonl(path, [codex_row("2026-10-01T00:00:00Z", "session_meta", {"cwd": cwd})] + rows)
        return path

    def test_groups_rollouts_by_cwd_and_keeps_unanswered_requests(self):
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

    def test_api_failure_in_rollout_is_cleared_by_a_later_assistant_message(self):
        path = self.rollout("rollout-error.jsonl", "/w/cut", [
            codex_user("2026-10-01T01:00:00Z", "finish the work"),
            codex_row("2026-10-01T01:01:00Z", "event_msg", {
                "type": "error", "message": "Selected model is at capacity"}),
        ])
        self.assertEqual(scan.summarize_rollout(path)[1]["api_error"], "Selected model is at capacity")
        with Path(path).open("a") as handle:
            handle.write(json.dumps(codex_assistant("2026-10-01T01:02:00Z", "Recovered.")) + "\n")
        self.assertIsNone(scan.summarize_rollout(path)[1]["api_error"])


def worktree(path, state=None, agent="claude", comment="", pr=None, main=False, status="in-progress",
             turn=None):
    """`turn` is the main agent's own state; Orca's `state` stays working while a background job runs."""
    return {
        "worktreeId": "repo::" + path, "path": path, "repo": "app", "branch": "refs/heads/x",
        "workspaceStatus": status, "comment": comment, "linkedPR": pr,
        "isMainWorktree": main,
        "agents": [{"agentType": agent, "state": state, **({"mainAgent": {"state": turn}} if turn else {})}]
        if state else [],
    }


def with_agents(row, *agents):
    """The worktree row holding these Orca agents, each (type, state, main agent's state or None)."""
    return {**row, "agents": [{"agentType": kind, "state": state, **({"mainAgent": {"state": turn}} if turn else {})}
                              for kind, state, turn in agents]}


def terminal(path, agent, handle="term_1", title="t", preview="", quiet_minutes=0, orphaned=False):
    return {"worktreeId": "repo::" + path, "handle": handle, "agentIdentity": agent,
            "title": title, "connected": not orphaned, "orphaned": orphaned, "preview": preview,
            "lastOutputAt": (NOW - quiet_minutes * 60) * 1000}


NOW = 1_800_000_000
PROMPT = "app on main via python\n❯"


def done_agent(kind, pane, said="", asked="", minutes_ago=0, interrupted=False):
    """An agent Orca shows as a done card: its turn ended `minutes_ago` in the pane `tab:leaf`."""
    return {"agentType": kind, "state": "done", "paneKey": pane, "interrupted": interrupted,
            "stateStartedAt": (NOW - minutes_ago * 60) * 1000,
            "lastAssistantMessage": said, "prompt": asked}


def in_pane(term, pane):
    tab, leaf = pane.split(":")
    return {**term, "tabId": tab, "leafId": leaf}


def verdict(asks=0.0, status="done", confidence=0.9):
    return {"asks": asks, "status": status, "confidence": confidence}


def judging(table):
    """A judge reading Jev's verdict from table, sure the work is done otherwise."""
    return lambda message: table.get(message, verdict())


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def claude_says(self, path, *rows):
        write_jsonl(scan.claude_project_dir(self.projects, path) / "s.jsonl", list(rows))

    def inventory(self, worktrees, terminals, codex=None, judge=None):
        return scan.build_inventory(worktrees, terminals, self.projects, codex or {}, "要判断", now=NOW,
                                    judge=judge or judging({}))

    def classes(self, worktrees, terminals, codex=None, judge=None):
        rows = self.inventory(worktrees, terminals, codex, judge)
        return {r["path"]: (r["class"], r["reasons"]) for r in rows}

    def closable(self, worktrees, terminals, codex=None):
        return {r["path"]: r["closable"] for r in self.inventory(worktrees, terminals, codex)}

    def test_a_finished_setup_shell_is_closable_while_the_agent_works(self):
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
        remote = {**worktree("/remote/shells", "working"), "_environment": "macbook-pro-16"}
        terms = [{**terminal(remote["path"], None, handle), "_environment": "macbook-pro-16", "lastOutputAt": None}
                 for handle in ("idle", "busy")]
        screens = {("macbook-pro-16", "idle"): {"source": "screen", "tail": ["direnv: loading .envrc", "➜", ""]},
                   ("macbook-pro-16", "busy"): {"source": "screen", "tail": ["➜", "Receiving objects: 42%"]}}

        row = scan.build_inventory([remote], terms, self.projects, {}, "要判断", now=NOW, screens=screens)[0]

        self.assertEqual((row["closable"], row["meaningful_tabs"]), (["idle"], ["busy"]))

    def test_a_permission_prompt_on_screen_keeps_a_memory_save_reply_waiting(self) -> None:
        self.claude_says("/w/prompt", user("go"), assistant(text("Saved the session memory.")))
        tree = {**worktree("/w/prompt", "waiting", status="completed"),
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

    def test_a_restart_that_dropped_background_waits_is_a_reason(self) -> None:
        line = "3 background shell command tasks didn't finish before the previous session ended"
        tree = {**worktree("/w/lost"), "agents": [done_agent("claude", "tab1:leaf1")]}
        term = in_pane(terminal(tree["path"], "claude", "term_lost"), "tab1:leaf1")

        row = scan.build_inventory([tree], [term], self.projects, {}, "要判断", now=NOW,
                                   screens={(None, "term_lost"): {"source": "screen", "tail": [line]}})[0]

        self.assertIn("background waits lost when the session was replaced (tab term_lost, agent state since "
                      "2027-01-15T08:00:00Z): " + line, row["reasons"])

    def test_a_stalled_worktree_offers_its_agents_but_not_a_busy_or_recent_shell(self):
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

    def test_an_agent_orca_reports_waiting_is_never_closable(self):
        self.claude_says("/w/p", user("go"), assistant(text("Running the migration now.")))
        result = self.inventory([worktree("/w/p", "waiting")], [terminal("/w/p", "claude", "term_blocked")])

        self.assertEqual([(r["class"], r["reasons"], r["closable"]) for r in result],
                         [("waiting", ["Orca agent waiting"], [])])

    def test_finished_work_with_only_closable_tabs_has_no_meaningful_tab(self):
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

    def test_a_completed_board_never_hides_an_open_pr_or_a_working_agent(self):
        self.claude_says("/w/o", user("go"), assistant(text("PR #3 is open.")))
        self.claude_says("/w/k", user("go"), assistant(text("Working on it.")))
        rows = self.inventory(
            [worktree("/w/o", "done", status="completed", pr={"number": 3, "state": "open"}),
             worktree("/w/k", "working", status="completed")],
            [terminal("/w/o", "claude", "term_o"), terminal("/w/k", "claude", "term_k")])

        self.assertEqual([r["class"] for r in rows], ["stalled", "working"])

    def test_a_tab_without_a_handle_still_counts_as_meaningful(self):
        self.claude_says("/w/h", user("go"), assistant(text("Merged.")))
        rows = self.inventory(
            [worktree("/w/h", "done", pr={"number": 1, "state": "merged"})],
            [terminal("/w/h", "claude", "term_h"),
             {key: value for key, value in terminal("/w/h", None, title="dev server").items()
              if key != "handle"}])

        self.assertEqual(rows[0]["meaningful_tabs"], ["(no handle) dev server"])

    def test_orphaned_terminal_records_are_ignored(self):
        result = self.inventory(
            [worktree("/w/c", None)],
            [terminal("/w/c", "codex", "term_gone", orphaned=True)])

        self.assertEqual([(r["class"], r["terminals"], r["closable"]) for r in result],
                         [("stalled", [], [])])

    def test_unreadable_orphaned_shell_is_not_a_meaningful_tab(self):
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

    def test_a_done_card_whose_tab_sleeps_has_no_handle(self):
        rows = self.inventory(
            [{**worktree("/w/s"), "agents": [done_agent("claude", "tab9:leaf9", interrupted=True)]}], [])

        self.assertEqual([(c["agent"], c["handle"], c["interrupted"]) for c in rows[0]["done_cards"]],
                         [("claude", None, True)])

    def test_a_reopened_tab_closes_its_card_when_it_is_the_only_tab_of_that_agent(self):
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

    def test_a_main_checkout_is_read_only_while_it_hosts_an_agent(self):
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
        [card] = scan.done_cards([done_agent("claude", "tab1:leaf1")],
                                 [in_pane(terminal("/w/d", "claude", "term_claude"), "tab1:leaf1")],
                                 {(None, "term_claude"): screen})
        self.assertFalse(card["composer_empty"])

    def test_a_hook_that_fails_for_a_missing_file_is_named_with_its_host_and_path(self):
        # Another Mac lost two hook scripts overnight: every Bash call and stop printed this, and nobody noticed.
        remote = {**worktree("/remote/lane-241", "working"), "_environment": "macbook-pro-16"}
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

    def test_a_remote_tab_without_agent_details_is_read_from_its_screen(self):
        # MBP16's terminal list gave test-338 no agent, title, or preview while a Claude waited after worker_done.
        remote = {**worktree("/remote/test-338"), "_environment": "macbook-pro-16"}
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

    def test_a_tab_orca_detached_but_still_runs_is_read(self):
        detached = {**terminal("/w/u", "claude", "term_detached"), "orphaned": True, "connected": True,
                    "tabId": "pty:u", "leafId": "pty:u"}
        rows = self.inventory([{**worktree("/w/u"), "agents": [done_agent("claude", "tab1:leaf1")]}], [detached])

        self.assertEqual(([t["handle"] for t in rows[0]["terminals"]], rows[0]["done_cards"][0]["handle"]),
                         (["term_detached"], "term_detached"))

    def test_waiting_comes_from_transcripts_and_the_decision_marker(self):
        self.claude_says("/w/a", user("go"), assistant(
            {"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": {"questions": []}}))
        self.claude_says("/w/b", user("go"), assistant(text("PR is up.\nShall I merge it?")))
        result = self.classes(
            [worktree("/w/a", "working"), worktree("/w/b", "done"),
             worktree("/w/c", "working", comment="要判断: which base?"),
             worktree("/w/main", main=True)],
            [terminal("/w/a", "claude"), terminal("/w/b", "claude"), terminal("/w/c", "claude")],
            judge=judging({"PR is up.\nShall I merge it?": verdict(0.9)}))

        self.assertEqual(result["/w/a"], ("waiting", ["AskUserQuestion pending"]))
        self.assertEqual(result["/w/b"], ("waiting", ["final message asks (Jev 0.90)"]))
        self.assertEqual(result["/w/c"], ("waiting", ["decision marker in comment"]))
        self.assertNotIn("/w/main", result)

    def test_a_final_message_jev_judges_as_waiting_on_the_user_is_waiting(self):
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

    def test_a_final_message_jev_is_unsure_about_is_read_before_anything_closes(self):
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

    def test_the_state_jev_reads_decides_what_an_idle_worktree_needs(self):
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

    def test_an_agent_whose_message_says_work_continues_keeps_its_tab(self):
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

    def test_only_the_latest_final_message_is_judged(self):
        lead = "Codex の修正を確かめました。テストは通っています。"
        helper = "commit・push はしていません。"
        self.claude_says("/w/l", user("go"), {**assistant(text(lead)), "timestamp": "2026-10-01T02:00:00Z"})
        self.claude_says("/w/h", user("go"), {**assistant(text("Codex に渡しました。")),
                                              "timestamp": "2026-10-01T01:00:00Z"})
        codex = {path: [{"rollout": "r", "last_user": "fix", "last_role": "assistant", "last_assistant": helper,
                         "last_assistant_at": "2026-10-01T01:30:00Z", "pending_requests": []}]
                 for path in ("/w/l", "/w/h")}
        judged = []

        def judge(message):
            judged.append(message)
            return verdict(0.1, "blocked_on_others")

        rows = self.inventory([worktree("/w/l", "done"), worktree("/w/h", "done")],
                              [terminal("/w/l", "claude"), terminal("/w/h", "claude")], codex, judge)

        self.assertEqual(judged, [lead, helper])
        self.assertEqual([r["jev"] for r in rows], [
            {"source": "claude", **verdict(0.1, "blocked_on_others")},
            {"source": "codex", **verdict(0.1, "blocked_on_others")}])

    def test_claude_speaks_for_the_worktree_when_either_time_is_missing(self):
        self.claude_says("/w/t", user("go"), assistant(text("Done.")))
        codex = {"/w/t": [{"rollout": "r", "last_user": "fix", "last_role": "assistant", "last_assistant": "ok",
                           "last_assistant_at": "2026-10-01T01:30:00Z", "pending_requests": []}]}

        rows = self.inventory([worktree("/w/t", "done")], [terminal("/w/t", "claude")], codex)

        self.assertEqual(rows[0]["jev"]["source"], "claude")

    def test_an_unjudged_final_message_is_read_before_anything_closes(self):
        self.claude_says("/w/u", user("go"), assistant(text("Merged.")))
        self.claude_says("/w/v", user("go"), assistant(text("Running the suite.")))
        judged = []

        def unavailable(message):
            judged.append(message)
            return None

        result = self.inventory(
            [worktree("/w/u", "done", pr={"number": 1, "state": "merged"}), worktree("/w/v", "working")],
            [terminal("/w/u", "claude", "term_u"), terminal("/w/v", "claude", "term_v")], judge=unavailable)

        self.assertEqual([(r["class"], r["reasons"], r["closable"]) for r in result], [
            ("unsure", ["Jev could not judge: read the final message"], []), ("working", [], [])])
        self.assertEqual(judged, ["Merged."])

    def test_a_question_is_found_while_only_a_background_job_keeps_the_agent_working(self):
        asks = "PR #220 を出しました。merge には許可が必要です。進めてよいですか。"
        self.claude_says("/w/bg", user("go"), assistant(text(asks)))

        result = self.classes([worktree("/w/bg", "working", turn="done")], [terminal("/w/bg", "claude")],
                              judge=judging({asks: verdict(0.9, "blocked_on_others")}))

        self.assertEqual(result["/w/bg"], ("waiting", ["final message asks (Jev 0.90)"]))

    def test_a_background_job_keeps_the_worktree_working_whatever_the_final_message_says(self):
        self.claude_says("/w/watch", user("go"), assistant(text("Merged. Watching the 12:00 import on stage.")))

        result = self.inventory(
            [worktree("/w/watch", "working", turn="done", pr={"number": 1, "state": "merged"}, status="completed")],
            [terminal("/w/watch", "claude", "term_w")])

        self.assertEqual([(r["class"], r["reasons"], r["closable"]) for r in result],
                         [("working", ["a background job runs after the agent's turn"], [])])

    def test_an_unsure_question_behind_a_background_job_is_read(self):
        maybe = "PR は出しました。表記をどちらにそろえるかは別に決める必要があります。"
        self.claude_says("/w/m", user("go"), assistant(text(maybe)))
        self.claude_says("/w/n", user("go"), assistant(text("Watching CI.")))

        result = self.classes(
            [worktree("/w/m", "working", turn="done"), worktree("/w/n", "working", turn="done")],
            [terminal("/w/m", "claude", "term_m"), terminal("/w/n", "claude", "term_n")],
            judge=judging({maybe: verdict(0.5, "in_progress"), "Watching CI.": verdict(0.1, "in_progress", 0.4)}))

        self.assertEqual(result["/w/m"], ("unsure", ["Jev unsure whether it asks (0.50): read the final message"]))
        self.assertEqual(result["/w/n"], ("working", ["a background job runs after the agent's turn"]))

    def test_agents_sharing_a_worktree_are_read_together(self):
        asks = "merge してよいですか。"
        for path in ("/w/turn", "/w/wait", "/w/pair"):
            self.claude_says(path, user("go"), assistant(text(asks)))
        judged = []

        def judge(message):
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

    def test_an_agent_without_a_readable_main_state_counts_as_in_its_turn(self):
        self.claude_says("/w/odd", user("go"), assistant(text("merge してよいですか。")))
        odd = worktree("/w/odd", "working")
        for main_agent in (None, {"state": None}, "done", ["done"], True):
            with self.subTest(main_agent=main_agent):
                row = {**odd, "agents": [{**odd["agents"][0], "mainAgent": main_agent}]}

                result = self.classes([row], [terminal("/w/odd", "claude")],
                                      judge=judging({"merge してよいですか。": verdict(0.9)}))

                self.assertEqual(result["/w/odd"], ("working", []))

    def test_a_codex_final_message_that_asks_is_waiting(self):
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

    def test_codex_requests_unstarted_agents_and_merged_work(self):
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

    def test_a_turn_cut_off_by_an_api_error_is_stalled_even_behind_a_background_job(self):
        # 2026-10-06 the Mac slept mid-response; a CI monitor kept Orca at working and the lead sat for 16 hours.
        cut = "API Error: Your computer went to sleep mid-response. The response above may be incomplete."
        self.claude_says("/w/cut", user("go"), assistant(text("critic の判定待ちです。")),
                         {**assistant(text(cut)), "isApiErrorMessage": True})
        judged = []

        def judge(message):
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

    def test_codex_capacity_failure_on_screen_is_stalled_even_with_a_merged_pr(self):
        failure = "■ Selected model is at capacity"
        codex = {"/w/cut": [{"rollout": "r", "last_user": "finish", "last_role": "assistant",
                              "last_assistant": "Done.", "last_assistant_at": "2026-10-01T01:00:00Z",
                              "pending_requests": []}]}
        row = self.inventory(
            [worktree("/w/cut", "done", agent="codex", pr={"number": 1, "state": "merged"})],
            [terminal("/w/cut", "codex", preview="Done.\n" + failure + "\n")], codex)[0]
        self.assertEqual((row["class"], row["reasons"], row["closable"]),
                         ("stalled", ["turn ended with an API error: " + failure], []))

    def test_a_lead_that_left_its_workers_reports_unread_is_stalled_or_says_so_with_its_question(self):
        # 2026-10-06 a lead ended its turn while workers ran; their reports sat unread for 20 hours.
        def at(minutes_ago):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes_ago * 60))

        def message(kind, minutes_ago, read=0, run="run_a"):
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
            mail=mail)

        self.assertEqual({r["path"]: (r["class"], r["reasons"], r["closable"]) for r in result}, {
            "/w/lead": ("stalled", [f"2 orchestration messages unread since its last turn began (oldest {at(50)})"],
                        []),
            "/w/asks": ("waiting", ["final message asks (Jev 0.90)",
                                    f"1 orchestration message unread since its last turn began (oldest {at(20)})"],
                        []),
        })

    def test_a_worktree_no_agent_ever_worked_in_says_so(self):
        result = self.classes([worktree("/w/shell", None, pr={"number": 3, "state": "open"}, status="in-review")],
                              [terminal("/w/shell", None)])

        self.assertEqual(result["/w/shell"], ("stalled", ["no agent session in this worktree"]))


def jev_answer(asks, status="done", confidence=0.9):
    return io.BytesIO(json.dumps({"model": "jev-1.13.0", "answers": {
        "asks_user": {"type": "noul", "noul": asks},
        "status": {"type": "choice", "choice": status, "confidence": confidence,
                   "probabilities": {status: confidence}}}}).encode())


class CutShort(io.BytesIO):
    """A response whose connection closes mid-body."""

    def read(self, *args):
        raise http.client.IncompleteRead(b"{")


class JevTests(unittest.TestCase):
    def test_asks_jev_whether_the_tail_of_a_message_waits_on_the_user_and_how_the_work_stands(self):
        sent = []

        def opener(request, timeout):
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

    def test_a_failed_jev_call_judges_nothing_and_says_why(self):
        failures = [urllib.error.HTTPError("u", 401, "Unauthorized", None, None),
                    TimeoutError("timed out"), urllib.error.URLError("offline"),
                    io.BytesIO(b"not json"), io.BytesIO(b'{"answers": {}}'), jev_answer(float("nan")),
                    jev_answer(1.5), jev_answer("nan"), CutShort(), jev_answer(0.1, "asks_user"),
                    jev_answer(0.1, "done", float("nan")), jev_answer(0.1, "done", 1.2)]
        for failure in failures:
            def opener(request, timeout, failure=failure):
                if isinstance(failure, Exception):
                    raise failure
                return failure

            err = io.StringIO()
            with redirect_stderr(err):
                self.assertIsNone(scan.jev_judge("key-1", opener=opener)("m"))
            self.assertIn("Jev could not judge", err.getvalue())


class NextStepFactTests(unittest.TestCase):
    def test_summarizes_review_merge_and_check_state_of_a_pr(self):
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

    def test_latest_human_review_ignores_newer_bot_reviews(self):
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

    def test_pr_authors_comment_is_not_an_external_review(self):
        review = {"user": {"login": "author", "type": "User"}, "state": "COMMENTED",
                  "submitted_at": "2026-10-21T07:39:59Z"}

        pr = scan.summarize_pr({"number": 42, "author": {"login": "author"}}, [review])

        self.assertIsNone(pr["latest_human_review"])

    def test_each_human_comment_surface_returns_the_wait_to_us_after_the_last_commit(self):
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

    def test_remote_pr_comment_marks_the_row_as_our_turn(self):
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

    def test_gh_prs_reads_human_reviews_and_keeps_pr_when_review_lookup_fails(self):
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
            def failing_reviews(argv, failure=failure, **kwargs):
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

    def test_a_failed_or_missing_gh_degrades_to_no_pr(self):
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

    def test_a_run_asks_github_once_per_repository_and_rereads_activity_only_for_updated_prs(self):
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
            def run():
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

    def test_a_repository_with_more_prs_than_one_query_holds_is_read_25_at_a_time(self):
        github = FakeGitHub({"acme/app": [pr_node(number) for number in range(1, 31)]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
            found = scan.gh_prs("acme/app", list(range(1, 31)), cache_dir=Path(cache))
        self.assertEqual(sorted(pr["number"] for pr in found.values()), list(range(1, 31)))
        self.assertEqual([len(re.findall(r"pullRequest\(", call[-1])) for call in github.graphql()], [25, 5])

    def test_a_missing_pr_leaves_the_others_in_its_query(self):
        github = FakeGitHub({"acme/app": [pr_node(7)]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
            found = scan.gh_prs("acme/app", [7, 404], cache_dir=Path(cache))
        self.assertEqual((found[7]["number"], found[404]), (7, None))
        self.assertEqual((len(github.graphql()), len(github.rest())), (1, 3))

    def test_a_branch_finds_its_pr_as_gh_pr_view_does(self):
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

    def test_names_are_checked_before_they_go_into_a_query(self):
        github = FakeGitHub({"acme/app": [pr_node(5, 'say-"hi"')]})
        with tempfile.TemporaryDirectory() as cache, patch.object(scan.subprocess, "run", side_effect=github):
            found = scan.gh_prs("acme/app", ['say-"hi"', "two words", "x\ny"], cache_dir=Path(cache))
            self.assertEqual(scan.gh_prs('acme/app") { x', [7], cache_dir=Path(cache)), {7: None})
        self.assertEqual({ref: pr and pr["number"] for ref, pr in found.items()},
                         {'say-"hi"': 5, "two words": None, "x\ny": None})
        [query] = github.graphql()
        self.assertNotIn("two words", query[-1])

    def test_a_pr_read_by_graphql_summarizes_as_its_gh_pr_view_json_did(self):
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


    def test_reads_uncommitted_and_unpushed_work_and_the_github_repo(self):
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
                              "dirty": 1, "unpushed": 1, "github": "acme/app"})
            self.assertIsNone(scan.git_state(str(Path(tmp) / "missing")))

        self.assertEqual(scan.github_slug("https://github.com/spin-dd/taihei-report.git"), "spin-dd/taihei-report")
        self.assertIsNone(scan.github_slug("https://git.disroot.org/usvdh/collect.git"))


    def test_a_pr_merged_after_the_final_message_ends_the_wait_it_reported(self):
        numbers = {name: number for number, name in enumerate(
            ("merged-later", "codex-merged-later", "merged-before", "continues", "other-head"), 7)}

        def stalled(path, said_at, status="blocked_on_others", source="claude"):
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
                stalled("/w/other-head", "2026-10-06T07:25:00.000Z")]
        git = {"branch": "fix-a", "head": "aaa", "dirty": 0, "unpushed": 0, "github": "acme/app"}
        heads = {numbers["other-head"]: "zzz"}

        def pr_lookup(slug, ref):
            return {"number": ref, "state": "MERGED", "merged_at": "2026-10-06T07:35:27Z",
                    "head_oid": heads.get(ref, "aaa")}

        def git_lookup(path):
            return git

        result = {r["path"]: (r["class"], r["reasons"])
                  for r in scan.add_next_step_facts(rows, git_lookup=git_lookup, pr_lookup=each_ref(pr_lookup))}

        self.assertEqual(result["/w/merged-later"], ("finished", ["PR merged after the final message"]))
        self.assertEqual(result["/w/codex-merged-later"], ("finished", ["PR merged after the final message"]))
        self.assertEqual(result["/w/merged-before"][0], "stalled")
        self.assertEqual(result["/w/continues"][0], "stalled")
        self.assertEqual(result["/w/other-head"][0], "stalled")

    def test_a_merged_pr_finishes_a_row_no_agent_ran_in(self):
        # Orca's PR link is often missing or stale; the lookup by branch sees the merge.
        def row(path, reasons, jev=None, link=None):
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

        def git_lookup(path):
            return {"branch": path.rsplit("/", 1)[-1], "head": "aaa", "dirty": 0,
                    "unpushed": 0, "github": "acme/app"}

        def pr_lookup(slug, ref):
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

    def test_open_pr_cleanup_uses_published_head_not_upstream(self):
        names = ("clean", "human-replied", "dirty", "unpushed", "other-head", "unknown-unpushed")
        rows = [{"path": "/w/" + name, "class": "stalled", "reasons": [scan.NO_SESSION_REASON],
                 "linked_pr": None, "claude": None, "codex": []} for name in names]

        def git_lookup(path):
            name = path.rsplit("/", 1)[-1]
            return {"branch": name, "head": "aaa", "dirty": int(name == "dirty"),
                    "unpushed": None if name == "unknown-unpushed" else int(name == "unpushed"),
                    "github": "acme/app"}

        def pr_lookup(slug, ref):
            return {"number": 7, "state": "OPEN", "head_oid": "bbb" if ref == "other-head" else "aaa",
                    "reply_after_last_commit": ref == "human-replied"}

        result = {row["path"].rsplit("/", 1)[-1]: row for row in scan.add_next_step_facts(
            rows, git_lookup=git_lookup, pr_lookup=each_ref(pr_lookup))}

        self.assertEqual({name: (row["class"], row.get("open_pr_cleanup", False)) for name, row in result.items()},
                         {"clean": ("finished", True), "human-replied": ("finished", True),
                          "dirty": ("stalled", False), "unpushed": ("finished", True),
                          "other-head": ("stalled", False), "unknown-unpushed": ("finished", True)})

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

    def test_open_pr_cleanup_preserves_a_historical_transcript_question(self):
        rows = [
            {"path": "/w/history", "class": "waiting", "reasons": ["final message asks"],
             "agents": [], "terminals": [], "linked_pr": None, "claude": {"final_text": "merge?"}},
            {"path": "/w/agent", "class": "stalled", "reasons": [scan.NO_SESSION_REASON],
             "agents": [{"type": "codex"}], "terminals": [], "linked_pr": None},
            {"path": "/w/agent-tab", "class": "stalled", "reasons": [scan.NO_SESSION_REASON],
             "agents": [], "terminals": [{"agent": "codex"}], "linked_pr": None},
        ]

        def git_lookup(path):
            return {"branch": path.rsplit("/", 1)[-1], "head": "aaa", "dirty": 0,
                    "unpushed": 1, "github": "acme/app"}

        def pr_lookup(slug, ref):
            return {"number": 7, "state": "OPEN", "head_oid": "aaa"}

        result = {row["path"].rsplit("/", 1)[-1]: row for row in scan.add_next_step_facts(
            rows, git_lookup=git_lookup, pr_lookup=each_ref(pr_lookup))}

        self.assertEqual(result["history"]["class"], "waiting")
        self.assertTrue(result["history"]["open_pr_cleanup"])
        self.assertNotIn("open_pr_cleanup", result["agent"])
        self.assertNotIn("open_pr_cleanup", result["agent-tab"])

    def test_completed_board_without_agent_still_checks_open_pr_for_cleanup(self):
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
            "final_at": "t1", "read": scan.read_key(rows[0])})

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

    def test_rows_a_reader_classes_get_git_and_pr_facts(self):
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

        def pr_lookup(slug, ref):
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

    def test_a_repository_timeout_does_not_hold_other_repositories_and_a_duplicate_pr_is_read_once(self):
        rows = [{"path": path, "class": "unsure", "linked_pr": {"number": number}}
                for path, number in (("/w/slow", 7), ("/w/fast", 8), ("/w/fast-again", 8))]
        gits = {"/w/slow": {"branch": "a", "head": "aaa", "github": "acme/slow"},
                "/w/fast": {"branch": "b", "head": "aaa", "github": "acme/app"},
                "/w/fast-again": {"branch": "c", "head": "aaa", "github": "acme/app"}}
        fast_started = Event()
        calls = []

        def pr_lookup(slug, refs):
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
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # A host's real Jev key and verdict store must never reach a test run.
        for hermetic in (patch.dict(os.environ, {"TYPESAFE_API_KEY": "", "TRIAGE_INBOX": "",
                                                "TRIAGE_ASK_ANSWERS": str(self.dir / "no-answers.jsonl")}),
                         patch.object(scan, "JEV_KEY_FILE", str(self.dir / "no-key")),
                         patch.object(scan, "INBOX_FILE", str(self.dir / "no-inbox.jsonl")),
                         patch.object(scan, "VERDICTS_FILE", str(self.dir / "verdicts.json")),
                         patch.object(scan, "PR_ACTIVITY_DIR", str(self.dir / "pr-activity"))):
            hermetic.start()
            self.addCleanup(hermetic.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_expand_roots_accepts_absolute_patterns_with_spaces(self):
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

    def test_expand_roots_accepts_literal_current_and_absolute_roots(self):
        self.assertEqual(scan.expand_roots([".", "./", "/"]), [".", ".", "/"])

    def saved_scan(self):
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

    def test_summary_names_each_missing_hook_file_with_its_host(self):
        saved, _ = self.saved_scan()
        rows = json.loads(saved.read_text())
        rows[2]["hook_errors"] = [{"host": "mbp16", "path": "/Users/me/.claude/hooks/git-hook-guard.sh",
                                   "tab": "term_9"}]
        saved.write_text(json.dumps(rows, ensure_ascii=False))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--summary"]), 0)

        self.assertIn("hook errors: 1\n  mbp16 /Users/me/.claude/hooks/git-hook-guard.sh (mbp16::app term_9)",
                      out.getvalue())

    def test_rows_print_one_line_per_saved_row_without_rescanning(self):
        saved, checkout = self.saved_scan()
        with patch.object(scan, "collect_worktrees_and_terminals", side_effect=AssertionError("rescanned")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--rows"]), 0)

        self.assertEqual(out.getvalue().splitlines(), [
            "fix-login | waiting (scan: stalled) | Eotel/app#10 OPEN | tabs 1 | 2026-10-27T01:00:00Z"
            " | PR #10 を merge するか",
            f"dotfiles-73 | unsure | Eotel/chezmoi-dotfiles | tabs 0 | 2026-10-27T02:00:00Z | 設定を反映しました",
            "mbp16::app | waiting | - | tabs 1 | 2026-10-27T03:00:00Z | どちらにしますか",
            "mbp16::fix-login | working | - | tabs 0 | - | ",
        ])

    def test_cards_print_each_done_card_and_tab_on_its_own_line_named_by_the_worktree(self) -> None:
        saved, _ = self.saved_scan()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--cards"]), 0)

        self.assertEqual(out.getvalue().splitlines(), [
            "fix-login | waiting (scan: stalled) | card term_1 claude | draft - | composer unknown"
            " | PR #10 を出しました。 merge してよいですか | screen: ✻ Baked for 3m ⏎ ❯",
            "fix-login | waiting (scan: stalled) | closable term_2",
            "fix-login | waiting (scan: stalled) | meaningful term_1",
            f"fix-login | waiting (scan: stalled) | close: python3 {SCRIPTS / 'close_tabs.py'} --from {saved} term_2",
            "mbp16::app | waiting | card - codex interrupted | draft unknown | composer unknown | - | screen: -",
        ])

    def test_cards_show_the_start_of_the_draft_beside_whether_the_screen_composer_is_empty(self) -> None:
        saved, _ = self.saved_scan()
        rows = json.loads(saved.read_text())
        suggestion = "The next step isn't obvious, so I'll leave this one blank. " + "x" * 20
        rows[0]["done_cards"] = [{"handle": "suggestion", "draft": True, "draft_text": suggestion, "composer_empty": True},
                                 {"handle": "saved-before", "draft": True}]
        saved.write_text(json.dumps(rows))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--cards"]), 0)

        self.assertIn("draft " + suggestion[:60] + "… | composer empty", out.getvalue().splitlines()[0])
        self.assertIn("draft yes | composer unknown", out.getvalue().splitlines()[1])

    def test_waiting_worktrees_and_remote_shells_missing_output_get_screens(self) -> None:
        waiting = worktree("/w/waiting", "waiting")
        working = worktree("/w/working", "working")
        remote = {**worktree("/remote/working", "working"), "_environment": "macbook-pro-16"}
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

    def test_row_prints_one_row_with_its_newest_final_message_and_plain_strings(self):
        saved, checkout = self.saved_scan()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(scan.main(["--from", str(saved), "--row", "dotfiles-73"]), 0)
        row = json.loads(out.getvalue())
        self.assertEqual((row["path"], row["github"], row["class"], row["comment"]),
                         (str(checkout), "Eotel/chezmoi-dotfiles", "unsure", "作業中"))
        self.assertEqual(row["final"], {"source": "codex", "at": "2026-10-27T02:00:00Z",
                                        "text": "設定を反映しました"})
        self.assertEqual(row["last_user"], "設定を反映して")

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

    def test_scan_includes_paired_mac_and_keeps_local_rows_if_it_is_unreachable(self):
        local = {"worktrees": [worktree("/local/w", "done")],
                 "hostScope": {"omittedHostSelectors": [
                     {"hostId": "remote-1", "selector": "--environment macbook-pro-16"}]}}
        remote = {"worktrees": [worktree("/remote/enhancements-9-mbp16", "done", agent="codex")]}
        environments = [{"id": "remote-1", "name": "macbook-pro-16",
                         "preferredEndpointId": "ws-1", "endpoints": [
                             {"id": "ws-1", "endpoint": "ws://remote.example:6768"}]}]
        calls = []

        def payload(orca, args, json_file=None, timeout=None):
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

    def test_screens_are_read_from_the_server_that_issued_each_handle(self):
        calls = []

        def orca(cmd, *args, **kwargs):
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

    def test_removed_host_id_is_named_when_no_environment_selector_exists(self):
        local = {"worktrees": [], "hostScope": {"omittedHostSelectors": [
            {"hostId": "runtime:deleted-id", "selector": None}]}}
        with patch.object(scan, "orca_payload", return_value=local), \
                patch.object(scan, "orca_result", side_effect=[[], []]), \
                redirect_stderr(io.StringIO()) as err:
            scan.collect_worktrees_and_terminals("orca")
        self.assertIn("runtime:deleted-id", err.getvalue())
        self.assertIn("not in orca environment list", err.getvalue())

    def test_remote_orca_final_message_is_classified_without_a_local_transcript(self):
        remote = {**worktree("/remote/w", "done", agent="codex"), "_environment": "macbook-pro-16"}
        remote["agents"][0].update({"lastAssistantMessage": "PR を出しました。レビュー待ちです。",
                                     "updatedAt": 1792567646152})
        term = {**terminal("/remote/w", "codex"), "_environment": "macbook-pro-16"}
        rows = scan.build_inventory([remote], [term], str(self.dir / "claude"), {}, "要判断",
                                    judge=lambda message: verdict(0.1, "blocked_on_others"))
        self.assertEqual(rows[0]["class"], "stalled")
        self.assertEqual(rows[0]["jev"]["source"], "orca")
        self.assertEqual(rows[0]["environment"], "macbook-pro-16")

    def test_reads_orca_json_files_and_skips_old_rollouts(self):
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


    def test_each_row_carries_when_the_scan_started_for_the_tool_that_closes_its_tabs(self):
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

    def test_writes_the_review_queue_when_asked(self):
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

    def test_summary_lists_review_rows_and_finished_worktree_tabs(self):
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

    def test_reads_the_orchestration_mailbox_from_json_files(self):
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

    def test_follows_every_page_of_runs_and_survives_an_orca_without_a_mailbox(self):
        inbox = self.dir / "inbox.json"
        inbox.write_text(json.dumps({"result": {"messages": [
            {"run_id": run, "to_handle": "run:" + run, "type": "question", "read": 0,
             "created_at": "2026-10-07T03:09:00Z", "subject": "Question"} for run in ("run_1", "run_2")]}}))
        pages = {(): {"runs": [{"id": "run_1", "coordinator_handle": "term_1"}], "nextCursor": "c2"},
                 ("--cursor", "c2"): {"runs": [{"id": "run_2", "coordinator_handle": "term_2"}], "nextCursor": None}}

        def run_list(argv, **kwargs):
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

    def test_shell_idle_minutes_sets_when_a_quiet_shell_becomes_closable(self):
        ps = self.dir / "ps.json"
        ps.write_text(json.dumps({"result": {"worktrees": [worktree("/w/s", None)]}}))
        shell = {**terminal("/w/s", None, "term_shell", preview=PROMPT),
                 "lastOutputAt": (time.time() - 10 * 60) * 1000}
        terms = self.dir / "terms.json"
        terms.write_text(json.dumps({"result": {"terminals": [shell]}}))

        def closable(*extra):
            out = io.StringIO()
            with redirect_stdout(out):
                scan.main(["--ps-json", str(ps), "--terminals-json", str(terms),
                           "--claude-projects", str(self.dir / "claude"),
                           "--codex-sessions", str(self.dir / "none"), *extra])
            return json.loads(out.getvalue())[0]["closable"]

        self.assertEqual(closable(), [])
        self.assertEqual(closable("--shell-idle-minutes", "5"), ["term_shell"])

    def scan_merged_worktree(self, *extra):
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

    def test_without_a_jev_key_idle_final_messages_come_back_unjudged(self):
        result, err = self.scan_merged_worktree()

        self.assertEqual(result, ("unsure", ["Jev could not judge: read the final message"],
                                  {"source": "claude", "asks": None, "status": None, "confidence": None}))
        self.assertIn("TYPESAFE_API_KEY", err)

    def test_judges_with_the_key_file(self):
        key = self.dir / "api_key"
        key.write_text("key-2\n")
        sent = []

        def urlopen(request, timeout):
            sent.append(request.get_header("Authorization"))
            return jev_answer(0.7)

        with patch.object(scan.urllib.request, "urlopen", urlopen):
            result, _ = self.scan_merged_worktree("--jev-key-file", str(key))

        self.assertEqual(result, ("waiting", ["final message asks (Jev 0.70)"],
                                  {"source": "claude", **verdict(0.7)}))
        self.assertEqual(sent, ["Bearer key-2"])

    def test_the_environment_key_wins_over_the_key_file(self):
        key = self.dir / "api_key"
        key.write_text("from-file\n")
        garbled = self.dir / "garbled"
        garbled.write_bytes(b"\xff\xfe")

        self.assertEqual(scan.jev_key({"TYPESAFE_API_KEY": "from-env"}, key), "from-env")
        self.assertEqual(scan.jev_key({}, key), "from-file")
        self.assertIsNone(scan.jev_key({}, self.dir / "missing"))
        self.assertIsNone(scan.jev_key({}, garbled))


def git(cwd, *args):
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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, **kwargs):
        return rmcheck.check(self.wt, self.main, base="main", compose_projects=[], **kwargs)

    def test_clean_worktree_contained_in_base_is_ok(self):
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

    def test_running_process_using_the_worktree_blocks_removal(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "print('ready', flush=True); import time; time.sleep(30)",
             str(self.wt.resolve())],
            stdout=subprocess.PIPE, text=True,
        )
        try:
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

    def test_process_running_from_worktree_blocks_without_path_in_command(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "print('ready', flush=True); import time; time.sleep(30)"],
            cwd=self.wt, stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            code, result = self.run_main()

            self.assertEqual(code, 1)
            self.assertEqual([item["pid"] for item in result["processes"]], [process.pid])
            self.assertEqual(result["processes"][0]["cwd"], str(self.wt.resolve()))
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_check_does_not_count_itself_or_its_shell(self):
        binary = Path(self.tmp.name) / "bin/orca"
        binary.parent.mkdir()
        binary.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"ok":false,"error":{"code":"selector_not_found"}}\'\nexit 1\n')
        binary.chmod(0o755)
        command = (f'{sys.executable} {rmcheck.__file__} --worktree {self.wt} '
                   '--base main --no-docker; :')
        completed = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True,
                                   env={**os.environ, "PATH": f"{binary.parent}:{os.environ.get('PATH', '')}"})

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["processes"], [])

    def test_process_in_a_sibling_with_the_same_name_prefix_does_not_block(self):
        sibling = self.wt.with_name(self.wt.name + "-other")
        sibling.mkdir()
        process = subprocess.Popen(
            [sys.executable, "-c", "print('ready', flush=True); import time; time.sleep(30)",
             str(sibling.resolve() / "server.js")],
            cwd=sibling, stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            code, result = self.run_main()

            self.assertEqual((code, result["processes"]), (0, []))
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_the_main_checkout_is_never_removable(self):
        result = rmcheck.check(self.main, self.main, base="main", compose_projects=[])

        self.assertEqual((result["ok"], result["blockers"]),
                         (False, ["this is the repository's main checkout: close its tabs and keep it"]))

    def test_uncommitted_changes_block_removal(self):
        (self.wt / "app.txt").write_text("v2\n")
        (self.wt / "new.txt").write_text("draft\n")

        result = self.check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["blockers"], ["2 uncommitted changes"])

    def test_a_commit_whose_patch_reached_the_base_does_not_block(self):
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

    def test_a_merge_commit_s_own_changes_still_block(self):
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

    def test_only_regenerable_output_left_is_ok(self):
        (self.main / ".gitignore").write_text(".env\nnode_modules/\nuploads\ne2e/screenshots/\n")
        git(self.main, "commit", "-q", "-am", "ignore screenshots")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.wt / "e2e" / "screenshots").mkdir(parents=True)
        (self.wt / "e2e" / "screenshots" / "受付 画面.png").write_text("x")

        result = self.check()

        self.assertTrue(result["ok"])
        self.assertEqual((result["review"], result["regenerable"]), ([], ["e2e/screenshots/"]))

    def test_generated_sources_and_test_output_do_not_hold_up_removal(self):
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

    def test_untracked_python_bytecode_is_regenerable_not_uncommitted(self):
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

    def test_tool_output_the_repository_ignores_does_not_hold_up_removal(self):
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

    def test_the_apm_installation_identity_does_not_hold_up_removal(self):
        # APM 0.35 writes these into each checkout it installs to, ignoring them with their own .gitignore.
        (self.wt / ".apm-local").mkdir()
        (self.wt / ".apm-local" / ".gitignore").write_text("*\n")
        (self.wt / ".apm-local" / "installation").write_text("0123456789abcdef0123456789abcdef\n")

        result = self.check()

        self.assertTrue(result["ok"])
        self.assertEqual((result["review"], sorted(result["regenerable"])),
                         ([], [".apm-local/.gitignore", ".apm-local/installation"]))

    def test_unmerged_commit_blocks_unless_it_is_the_pr_head(self):
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        head = git(self.wt, "rev-parse", "HEAD")

        blocked = self.check()
        squashed = self.check(pr={"number": 7, "state": "MERGED", "headRefOid": head})
        reopened = self.check(pr={"number": 7, "state": "OPEN", "headRefOid": head})

        self.assertEqual(blocked["blockers"], ["HEAD %s is not in main" % head[:8]])
        self.assertTrue(squashed["ok"])
        self.assertTrue(reopened["ok"])

    def test_a_merged_pr_of_the_branch_at_head_counts_without_pr(self):
        git(self.main, "remote", "add", "origin", "git@github.com:acme/app.git")
        (self.wt / "app.txt").write_text("squashed\n")
        git(self.wt, "commit", "-q", "-am", "change")
        head = git(self.wt, "rev-parse", "HEAD")
        real_run = subprocess.run
        lookups = []

        def gh_lists(prs):
            def fake_run(cmd, *args, **kwargs):
                if cmd[0] == "gh":
                    lookups.append(cmd)
                    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(prs), stderr="")
                return real_run(cmd, *args, **kwargs)
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

    def test_archive_scratch_moves_records_only_when_they_alone_keep_the_worktree(self):
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
        moved = sorted(path.name for path in archive.iterdir())
        self.assertRegex(" ".join(moved), r"^wt-investigate-\d{8} wt-yomiyasu-\d{8}$")
        self.assertEqual((archive / moved[1] / "inputs" / "draft.md").read_text(), "原文\n")
        self.assertEqual(sorted(Path(item["to"]).name for item in result["archived"]), moved)

    def test_open_pr_at_clean_checkout_head_is_removable(self):
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

    def test_an_ignored_path_that_holds_nothing_needs_no_review(self):
        (self.main / "uploads").write_text("rows from the main checkout\n")
        (self.wt / "uploads").write_text("")
        (self.wt / "node_modules").mkdir()
        (self.wt / ".env").mkdir()
        (self.wt / ".env" / "empty").write_text("")

        self.assertEqual(self.check()["review"], [])

    def test_ignored_files_need_review_unless_cache_or_same_as_main(self):
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

    def test_an_env_is_regenerable_when_it_moves_only_ports_from_its_example(self):
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

    def test_an_env_with_a_multiline_value_stays_in_review_without_key_names(self):
        (self.main / ".env.example").write_text('PORT=8000\nSIGNING_KEY="-----BEGIN KEY-----\nexample\n-----END KEY-----"\n')
        git(self.main, "add", ".env.example")
        git(self.main, "commit", "-q", "-m", "env example")
        git(self.wt, "merge", "-q", "--ff-only", "main")
        (self.wt / ".env").write_text('PORT=8001\nSIGNING_KEY="-----BEGIN KEY-----\nc2VjcmV0PQ==\n-----END KEY-----"\n')

        result = self.check()

        self.assertEqual((result["review"], result["regenerable"]), ([".env (not in main checkout)"], []))
        self.assertNotIn("c2VjcmV0", json.dumps(result))

    def test_reports_compose_projects_defined_in_the_worktree(self):
        projects = [
            {"Name": "app-wt", "Status": "running(2)", "ConfigFiles": str(self.wt / "compose.yaml")},
            {"Name": "app-main", "Status": "running(1)", "ConfigFiles": str(self.main / "compose.yaml")},
        ]

        result = rmcheck.check(self.wt, self.main, base="main", compose_projects=projects)

        self.assertTrue(result["ok"])
        self.assertEqual(result["containers"], [{"project": "app-wt", "status": "running(2)"}])

    def test_a_dangling_symlink_is_reported_instead_of_crashing(self):
        (self.main / "uploads").mkdir()
        (self.main / "uploads" / "a.png").write_text("png")
        (self.wt / "uploads").symlink_to(Path(self.tmp.name) / "gone")

        result = self.check()

        self.assertEqual(result["review"], ["uploads (differs from main checkout)"])

    def test_same_tree_compares_raw_symlink_targets(self):
        left = self.wt / "link"
        right = self.main / "link"
        left.symlink_to("file/")
        right.symlink_to("file")

        self.assertFalse(rmcheck.same_tree(left, right))

    def test_compose_placeholder_entries_never_match_the_worktree(self):
        projects = [{"Name": "elsewhere", "Status": "running(1)", "ConfigFiles": "/other/compose.yaml,-"}]
        cwd = Path.cwd()
        os.chdir(self.wt)
        try:
            result = rmcheck.check(self.wt, self.main, base="main", compose_projects=projects)
        finally:
            os.chdir(cwd)

        self.assertEqual(result["containers"], [])

    def test_processes_in_orca_tabs_name_their_tab_and_idle_shells_do_not_block(self):
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
                patch.object(rmcheck, "process_handles", return_value={32: "term_busy"}):
            result = self.check()
        with patch.object(rmcheck, "process_table", return_value={pid: table[pid] for pid in (1, 11, 20, 21)}), \
                patch.object(rmcheck, "process_cwds", return_value={21: wt}), \
                patch.object(rmcheck, "process_handles", return_value={}):
            idle_only = self.check()

        tabs = {item["pid"]: (item["in_orca_tab"], item["idle_tab"], item["tab"]) for item in result["processes"]}
        self.assertEqual(tabs, {21: (True, True, None), 32: (True, False, "term_busy"), 40: (False, False, None)})
        self.assertEqual(len(result["blockers"]), 2)
        self.assertIn(f"python3 {SCRIPTS / 'close_tabs.py'} --from SCAN_JSON term_busy", result["blockers"][1])
        self.assertIn("PID 40 ", result["blockers"][0])
        self.assertEqual((idle_only["ok"], idle_only["blockers"]), (True, []))

    def test_a_failed_pr_lookup_names_the_command_for_a_mac_where_gh_works(self):
        git(self.main, "remote", "add", "origin", "git@github.com:acme/app.git")
        head = git(self.wt, "rev-parse", "HEAD")
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "gh":
                raise subprocess.CalledProcessError(1, cmd, stderr="HTTP 401: Bad credentials")
            return real_run(cmd, *args, **kwargs)

        with patch.object(rmcheck.subprocess, "run", side_effect=fake_run):
            code, result = self.run_main("--pr", "7")

        self.assertEqual(code, 1)
        self.assertIn("merged_pr_head.py --repo acme/app --pr 7 --head " + head, " ".join(result["notes"]))

    def test_merged_pr_head_prints_rm_check_arguments_only_for_a_merged_pr(self):
        pr_head, head = "a" * 40, "b" * 40

        def gh(state):
            def run(cmd, *args, **kwargs):
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

    def test_nested_worktrees_on_a_remote_are_detached_and_others_block(self):
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

    def test_a_failed_pr_lookup_blocks_with_a_reason(self):
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "gh":
                raise FileNotFoundError("gh")
            return real_run(cmd, *args, **kwargs)

        with patch.object(rmcheck.subprocess, "run", side_effect=fake_run):
            code, result = self.run_main("--pr", "7")

        self.assertEqual(code, 1)
        self.assertEqual(result["blockers"], ["PR #7 lookup failed: gh"])
        self.assertIn("--pr-head", " ".join(result["notes"]))

    def test_command_accepts_a_merged_pr_head_without_fetched_base(self):
        (self.wt / "app.txt").write_text("merged change\n")
        git(self.wt, "commit", "-q", "-am", "merged change")
        head = git(self.wt, "rev-parse", "HEAD")

        code, result = self.run_main("--pr-head", head, "--fetch-failed")

        self.assertEqual((code, result["ok"], result["blockers"]), (0, True, []))
        self.assertIn("git fetch failed", " ".join(result["notes"]))

    def test_command_accepts_an_ancestor_of_the_merged_pr_head(self):
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

    def test_command_blocks_unpushed_commit_after_the_merged_pr_head(self):
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

    def test_a_commit_after_pr_head_blocks_even_if_its_patch_is_in_base(self):
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

    def test_command_uses_compare_when_the_pr_head_object_is_missing(self):
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

    def test_stale_origin_fetch_is_reported_from_shared_git_state(self):
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
        git(self.main, "remote", "set-url", "origin", str(bare))
        git(self.wt, "fetch", "-q", "origin", "main")
        _, fresh = self.run_main_without_base()

        self.assertEqual(stale["base"], "origin/main")
        self.assertIn("origin の参照は 2020-01-02T00:00:00Z の fetch のまま", " ".join(stale["notes"]))
        self.assertIn("--pr-head", " ".join(stale["notes"]))
        self.assertNotIn("origin の参照は", " ".join(fresh.get("notes", [])))

    def run_main(self, *argv: str, notes: FakeWorktreeNotes | None = None) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(self.wt), "--base", "main", "--no-docker", *argv],
                                worktree_notes=notes or FakeWorktreeNotes())
        return code, json.loads(out.getvalue())

    def run_main_without_base(self, *argv, pr=None):
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(pr), stderr="")
            return real_run(cmd, *args, **kwargs)

        out = io.StringIO()
        with patch.object(rmcheck.subprocess, "run", side_effect=fake_run), redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(self.wt), "--no-docker", *argv], worktree_notes=FakeWorktreeNotes())
        return code, json.loads(out.getvalue())

    def remote_default_and_working_base(self):
        """origin/HEAD -> origin/main at the first commit; origin/develop holds the worktree's commit."""
        first = git(self.main, "rev-parse", "main")
        git(self.main, "update-ref", "refs/remotes/origin/main", first)
        git(self.main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        head = git(self.wt, "rev-parse", "HEAD")
        git(self.main, "update-ref", "refs/remotes/origin/develop", head)
        return head

    def test_command_checks_against_the_base_branch_of_the_pr(self):
        # A repository whose default branch is not where the work merges (cxc merges into feat/nature).
        head = self.remote_default_and_working_base()
        pr = {"number": 7, "state": "OPEN", "headRefOid": head, "baseRefName": "develop"}

        default_code, default = self.run_main_without_base()
        pr_code, by_pr = self.run_main_without_base("--pr", "7", pr=pr)

        self.assertEqual((default_code, default["base"]), (1, "origin/HEAD"))
        self.assertEqual((pr_code, by_pr["base"], by_pr["blockers"]), (0, "origin/develop", []))

    def test_a_pr_base_branch_missing_locally_is_named_in_the_notes(self):
        head = self.remote_default_and_working_base()
        pr = {"number": 7, "state": "OPEN", "headRefOid": head, "baseRefName": "release"}

        code, result = self.run_main_without_base("--pr", "7", pr=pr)

        self.assertEqual((code, result["base"]), (0, "origin/HEAD"))
        self.assertEqual(result["notes"], ["PR #7 base origin/release is not fetched: fetch it or pass --base"])

    def test_command_checks_against_the_remote_branch_the_worktree_started_from(self):
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

    def test_a_branch_started_from_its_own_remote_branch_is_not_its_base(self):
        # `git worktree add -b topic <path> origin/topic`: pushed work is on the remote, not merged.
        first = git(self.main, "rev-parse", "main")
        for ref in ("refs/remotes/origin/main", "refs/remotes/origin/topic"):
            git(self.main, "update-ref", ref, first)
        git(self.main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.wt = Path(self.tmp.name) / "topic"
        git(self.main, "worktree", "add", "-q", "-b", "topic", str(self.wt), "refs/remotes/origin/topic")
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "work in progress")
        head = git(self.wt, "rev-parse", "HEAD")
        git(self.main, "update-ref", "refs/remotes/origin/topic", head)

        code, result = self.run_main_without_base()

        self.assertEqual((code, result["base"], result["blockers"]),
                         (1, "origin/HEAD", ["HEAD %s is not in origin/HEAD" % head[:8]]))

    def test_command_finds_the_main_checkout_and_fails_when_blocked(self):
        (self.main / ".env").write_text("A=1\n")
        (self.wt / ".env").write_text("A=1\n")
        clean_code, clean = self.run_main()
        (self.wt / "app.txt").write_text("v2\n")
        dirty_code, dirty = self.run_main()

        self.assertEqual((clean_code, clean["ok"], clean["main"]), (0, True, str(self.main.resolve())))
        self.assertEqual((dirty_code, dirty["blockers"]), (1, ["1 uncommitted changes"]))

    def test_command_without_a_base_ref_says_so(self):
        (self.wt / "app.txt").write_text("v2\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(self.wt), "--no-docker"], worktree_notes=FakeWorktreeNotes())

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["blockers"],
                         ["1 uncommitted changes",
                          "no base ref found (origin/HEAD, origin/main, origin/master); pass --base"])

    def test_command_rejects_a_path_that_is_not_a_git_worktree(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        out = io.StringIO()
        with redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(plain), "--no-docker"])

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["blockers"], ["not a git worktree"])


NOTHING_OWED = {"needs_owner": [], "closed_issues": []}


def gh_issue(url, number, closing_prs=()):
    return {"number": number, "title": f"issue {number}", "url": url + str(number), "assignees": [],
            "closedByPullRequestsReferences": [{"number": pr} for pr in closing_prs]}


class HourlyStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "hourly.json"
        # A host's real verdict store must never reach a test run.
        hermetic = patch.object(hourly_state.scan_sessions, "VERDICTS_FILE", str(self.root / "no-verdicts.json"))
        hermetic.start()
        self.addCleanup(hermetic.stop)

    def source(self, name, content):
        path = self.root / name
        path.write_text(content)
        return str(path)

    def call(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = hourly_state.main(["--state-file", str(self.state), *args])
        return code, json.loads(out.getvalue())

    def test_pending_lists_only_open_items_each_with_its_id(self):
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

    def test_left_row_only_stays_left_for_the_saved_final_message(self):
        old = self.source("old.txt", "Work complete; leave it.\n")
        new = self.source("new.txt", "New work needed.\n")
        self.assertEqual(self.call("leave", "--row", "/w/a", "--final-message-file", old)[0], 0)
        self.assertEqual(self.call("left-now", "--row", "/w/a", "--final-message-file", old),
                         (0, {"left": True}))
        self.assertEqual(self.call("left-now", "--row", "/w/a", "--final-message-file", new),
                         (1, {"left": False}))

    def test_question_and_handoff_survive_a_new_session(self):
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

    def test_question_records_its_tab_for_later_cleanup(self):
        message = self.source("message.txt", "Ready.\n")
        question = self.source("question.txt", "Proceed?\n")
        with patch.dict(os.environ, {"ORCA_PANE_KEY": "tab:leaf"}):
            _, asked = self.call("asked", "--row", "/w/a", "--decision", "merge:PR#10",
                                 "--final-message-file", message, "--question-file", question)
        self.assertEqual(asked["owner_pane"], "tab:leaf")

    def test_same_question_for_a_later_pr_is_a_new_decision(self):
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

    def test_malformed_state_is_not_overwritten(self):
        self.state.write_text("broken")
        message = self.source("message.txt", "hello")
        with redirect_stderr(io.StringIO()):
            code = hourly_state.main(["--state-file", str(self.state), "leave", "--row", "/w/a",
                                      "--final-message-file", message])
        self.assertEqual(code, 1)
        self.assertEqual(self.state.read_text(), "broken")

    def test_request_stays_open_after_worker_done_until_symptom_is_verified(self):
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

    def test_open_issues_of_listed_repos_without_a_checkout_become_requests_needing_owner(self):
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

    def test_saved_scan_names_live_owners_and_links_issues_without_reading_orca(self):
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

    def test_a_saved_scan_takes_the_verdicts_classed_after_it_was_saved(self):
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

    def test_recorded_issue_no_longer_open_is_set_apart_as_a_closure_candidate(self):
        (self.root / "issue-repos.json").write_text(json.dumps({"repos": ["acme/app", "acme/lib"]}))
        app, lib = "https://github.com/acme/app/issues/", "https://github.com/acme/lib/issues/"
        opened = {"acme/app": [gh_issue(app, 1), gh_issue(app, 2)], "acme/lib": [gh_issue(lib, 3)]}
        with patch.object(hourly_state, "gh_open_issues", side_effect=opened.get), \
                patch.object(hourly_state, "orca_worktrees", return_value=[]):
            self.call("requests-needing-owner")

        def later(repo):
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

    def test_issue_list_is_read_once_more_after_a_failure(self):
        (self.root / "issue-repos.json").write_text(json.dumps({"repos": ["acme/app", "acme/lib"]}))
        app, lib = "https://github.com/acme/app/issues/", "https://github.com/acme/lib/issues/"
        outcomes = {"acme/app": [subprocess.CalledProcessError(1, "gh"), [gh_issue(app, 1)]],
                    "acme/lib": [subprocess.TimeoutExpired("gh", 60), ValueError("bad JSON"), [gh_issue(lib, 2)]]}

        def gh(repo):
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

    def test_unreachable_paired_mac_is_given_up_on_quickly(self):
        (self.root / "issue-repos.json").write_text(json.dumps({"repos": ["acme/app"]}))
        url = "https://github.com/acme/app/issues/"
        local = {"worktrees": [{"path": "/w/app", "repo": "app", "linkedIssue": 1, "linkedItems": [],
                                "linkedPR": {"number": 4, "url": "https://github.com/acme/app/pull/4"}}],
                 "hostScope": {"omittedHostSelectors": [
                     {"hostId": "remote-1", "selector": "--environment office-mac-mini"}]}}
        replies = {("worktree", "ps"): local, ("terminal", "list"): {"terminals": []},
                   ("environment", "list"): {"environments": []}}
        remote_waits = []

        def run(cmd, **kwargs):
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 0, json.dumps([gh_issue(url, 1), gh_issue(url, 2)]), "")
            if "--environment" in cmd:
                remote_waits.append(kwargs["timeout"])
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": replies[tuple(cmd[1:3])]}), "")

        with patch.object(hourly_state.subprocess, "run", side_effect=run), \
                redirect_stderr(io.StringIO()) as err:
            code, report = self.call("requests-needing-owner", "--orca", "orca", "--live-owner", "/w/app")

        self.assertEqual(code, 0)
        self.assertEqual([request["source"]["url"] for request in report["needs_owner"]], [url + "2"])
        self.assertIn("office-mac-mini", err.getvalue())
        self.assertTrue(remote_waits)
        self.assertLessEqual(max(remote_waits), 15)

    def test_request_commands_accept_existing_state_without_requests(self):
        self.state.write_text(json.dumps({"version": 1, "left": {}, "questions": {}, "handoffs": {}}))
        self.assertEqual(self.call("requests-needing-owner"), (0, NOTHING_OWED))
        self.assertIn("requests", self.call("show")[1])
        original = self.source("original.txt", "担当を立てて直してほしい")
        symptom = self.source("symptom.txt", "direnv が止まる")
        _, request = self.call("request-open", "--original-file", original, "--symptom-file", symptom)
        self.assertIsNone(request["owner"])
        self.assertEqual(self.call("requests-needing-owner")[1]["needs_owner"][0]["id"], request["id"])

    def test_same_path_on_another_mac_does_not_hide_an_unowned_request(self):
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

    def test_request_owner_requires_scan_row_identity(self):
        original = self.source("original.txt", "動作が遅い")
        symptom = self.source("symptom.txt", "起動が止まる")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = hourly_state.main(["--state-file", str(self.state), "request-open",
                                      "--original-file", original, "--symptom-file", symptom,
                                      "--owner", "skills/shared-lane"])
        self.assertEqual(code, 1)
        self.assertFalse(self.state.exists())


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ps = Path(self.tmp.name) / "ps.json"
        self.runs = Path(self.tmp.name) / "runs.json"

    def run_precheck(self, worktrees, runs=(), workspace="/w/triage", truncated=False):
        self.ps.write_text(json.dumps({"result": {"worktrees": worktrees, "truncated": truncated}}))
        self.runs.write_text(json.dumps({"result": {"runs": runs}}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = precheck.main(["--ps-json", str(self.ps), "--runs-json", str(self.runs),
                                  "--workspace", workspace])
        return code, out.getvalue()

    def test_a_working_turn_holds_the_next_run(self):
        for turn in ("working", "done"):
            with self.subTest(turn=turn):
                row = worktree("/w/triage", "working", turn=turn)
                row["agents"][0]["paneKey"] = "scheduled-pane"
                code, out = self.run_precheck([row], [{"status": "dispatched", "trigger": "scheduled",
                                                      "terminalPaneKey": "scheduled-pane",
                                                      "workspaceId": "repo::/w/triage"}])
                self.assertEqual(code, 1)
                self.assertIn("skip: a scheduled turn in /w/triage is working", out)

    def test_completed_scheduled_run_with_working_human_followup_allows_next_run(self):
        row = worktree("/w/triage", "working")
        row["agents"][0]["paneKey"] = "old-pane"
        row["agents"][0]["mainAgent"] = {"state": "working"}
        runs = [{"status": "completed", "trigger": "scheduled", "terminalPaneKey": "old-pane",
                 "workspaceId": "repo::/w/triage"}]
        code, out = self.run_precheck([row], runs)
        self.assertEqual(code, 0)
        self.assertIn("run: start a fresh session", out)

    def test_background_job_from_completed_run_still_blocks(self):
        row = worktree("/w/triage", "working", turn="done")
        row["agents"][0]["paneKey"] = "old-pane"
        runs = [{"status": "completed", "trigger": "scheduled", "terminalPaneKey": "old-pane",
                 "workspaceId": "repo::/w/triage"}]
        self.assertEqual(self.run_precheck([row], runs)[0], 1)

    def test_working_agent_without_pane_identity_is_unverifiable(self):
        row = worktree("/w/triage", "working")
        code, out = self.run_precheck([row], [{"status": "completed", "trigger": "scheduled",
                                              "terminalPaneKey": "old-pane",
                                              "workspaceId": "repo::/w/triage"}])
        self.assertEqual(code, 2)
        self.assertIn("could not identify", out)

    def test_waiting_blocked_and_done_turns_allow_a_fresh_session(self):
        for state in ("waiting", "blocked", "done"):
            with self.subTest(state=state):
                code, out = self.run_precheck([worktree("/w/triage", state),
                                               worktree("/w/lane", "working")])
                self.assertEqual(code, 0)
                self.assertIn("run: start a fresh session", out)

    def test_matches_a_symlinked_workspace(self):
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(real)
        code, _ = self.run_precheck([worktree(str(link), "waiting")], workspace=str(real))
        self.assertEqual(code, 0)

    def test_requires_a_known_workspace(self):
        code, out = self.run_precheck([worktree("/w/lane", "done")])
        self.assertEqual(code, 2)
        self.assertIn("not an Orca worktree", out)
        code, out = self.run_precheck([worktree("/w/lane", "done")], truncated=True)
        self.assertEqual(code, 2)
        self.assertIn("before cutting the list short", out)

    def test_failure_to_read_orca_skips_the_run(self):
        refusing = Path(self.tmp.name) / "refusing-orca"
        refusing.write_text("#!/bin/sh\necho 'runtime not reachable' >&2\nexit 3\n")
        refusing.chmod(0o755)
        out = io.StringIO()
        with redirect_stdout(out):
            code = precheck.main(["--orca", str(refusing), "--workspace", "/w/triage"])
        self.assertEqual(code, 2)
        self.assertIn("runtime not reachable", out.getvalue())


class HourlyTabCleanupTests(unittest.TestCase):
    def test_transcript_lookup_finds_unique_scheduled_start_without_usage(self):
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

    def test_only_completed_idle_older_scheduled_tab_is_a_candidate(self):
        runs = [{"status": "dispatched", "terminalPaneKey": "current"},
                {"status": "completed", "terminalPaneKey": "old"}]
        terms = [{"worktreeId": "repo::/w/triage", "tabId": "old", "leafId": "leaf",
                  "handle": "old-handle", "connected": True, "orphaned": False}]
        runs[1]["terminalPaneKey"] = "old:leaf"
        agents = [{"paneKey": "old:leaf", "state": "done", "mainAgent": {"state": "done"}}]
        self.assertEqual([term["handle"] for _, term in cleanup_hourly.prior_tabs(
            runs, terms, agents, "/w/triage", "current")], ["old-handle"])
        agents[0]["state"] = "working"  # a background job still owns the old tab
        self.assertEqual(list(cleanup_hourly.prior_tabs(runs, terms, agents, "/w/triage", "current")), [])
        agents[0]["state"] = "done"
        runs[1]["status"] = "dispatched"
        self.assertEqual(list(cleanup_hourly.prior_tabs(runs, terms, agents, "/w/triage", "current")), [])

    def test_pending_shared_question_keeps_previous_tabs(self):
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

    def test_rendered_screen_and_draft_must_be_safe_to_close(self):
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


def triage_automation(automation_id, workspace, prompt="/orca-session-triage hourly"):
    return {"id": automation_id, "prompt": prompt, "workspaceId": "repo1::" + workspace}


def automation_run(status, started):
    return {"status": status, "startedAt": int(datetime.fromisoformat(started).timestamp() * 1000)}


def tool_result(content, result):
    """A transcript entry holding a tool's result, as Claude Code writes it."""
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": content}]}, "toolUseResult": result}


class HourlyRunsTests(unittest.TestCase):
    def test_lists_each_scheduled_run_with_its_precheck_reason_and_transcript(self):
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

        def local(ms):
            return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")
        self.assertEqual(out.getvalue().splitlines(), [
            f"{local(1793084400000)} | completed | started {local(1793084415827)} | - | pane tab:leaf | {transcript}",
            f"{local(1793080800000)} | skipped_precheck | started {local(1793080828333)}"
            " | skip: a scheduled turn is working | pane - | -",
        ])


class InboxWatchServiceTests(unittest.TestCase):
    def test_plist_runs_the_existing_watch_with_the_existing_offset(self):
        with tempfile.TemporaryDirectory() as root:
            payload = inbox_service.plistlib.loads(inbox_service.plist_bytes(
                root, python="/test/bin/python3", orca="/app/bin/orca"))
            self.assertEqual(payload["ProgramArguments"], [
                "/test/bin/python3",
                f"{root}/.cache/orca-briefs/bin/watch-inbox",
                f"{root}/.cache/orca-briefs/offsets/close-done-triage.offset"])
            self.assertTrue(payload["KeepAlive"])
            self.assertIn("/app/bin", payload["EnvironmentVariables"]["PATH"])

    def test_activation_refuses_a_second_reader_of_the_offset(self):
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

    def test_status_requires_launchd_pid_to_own_the_offset(self):
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

    def test_alert_service_uses_a_separate_cursor_and_notifier(self):
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

    def test_alert_activation_starts_after_existing_inbox_records(self):
        with tempfile.TemporaryDirectory() as root:
            _, inbox, cursor, _ = inbox_service.alert_paths(root)
            write_jsonl(inbox, [{"kind": "ask", "worktree": "old", "text": "already handled"}])
            with patch.object(inbox_service, "alert_status", side_effect=[(False, "stopped"), (True, "running")]), \
                    patch.object(inbox_service.subprocess, "run") as launchctl:
                inbox_service.activate_alerts(root)
            self.assertEqual(int(cursor.read_text()), inbox.stat().st_size)
            self.assertEqual(launchctl.call_args.args[0][:2], ["launchctl", "bootstrap"])


class InboxReadTests(unittest.TestCase):
    def test_a_filtered_read_gives_no_offset_to_acknowledge(self):
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

    def test_questions_come_four_at_a_time_ready_for_ask_user_question(self):
        def question(n, **extra):
            return {"question": f"Q{n} をどうしますか？", "header": f"判断{n}", "multiSelect": False,
                    "options": [{"label": "残す", "description": "今の帳票のまま"},
                                {"label": "直す", "description": "移した帳票に合わせる"}],
                    "recommended": "直す", **extra}

        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            write_jsonl(inbox, [
                {"id": "r1", "ts": "t1", "kind": "ask", "host": "h", "repo": "report", "worktree": "decisions",
                 "handle": "term_a", "text": "判断 4 件", "questions": [
                     question(1, issue="spin-dd/taihei-report#239"), question(2), question(3), question(4)]},
                {"id": "r2", "ts": "t2", "kind": "ask", "host": "h", "worktree": "plain", "text": "文章だけの質問"},
                {"id": "r3", "ts": "t3", "kind": "ask", "host": "h", "repo": "report", "worktree": "decisions",
                 "text": "判断 2 件", "questions": [question(5), question(6)]},
            ])
            environment = {"TRIAGE_INBOX": str(inbox), "TRIAGE_READ_OFFSET": str(Path(root) / "read.offset"),
                           "TRIAGE_ASK_ANSWERS": str(Path(root) / "answers.jsonl")}

            def run(*argv):
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
            "question": "Q1 をどうしますか？（spin-dd/taihei-report#239）", "header": "判断1", "multiSelect": False,
            "options": [{"label": "直す（推奨）", "description": "移した帳票に合わせる"},
                        {"label": "残す", "description": "今の帳票のまま"}]})
        self.assertEqual((first["items"][0]["source"], first["items"][0]["handle"]), ("report/decisions", "term_a"))
        self.assertEqual((first["remaining"], rest["remaining"], rest["next_after"]), (1, 0, None))
        self.assertEqual([item["id"] for item in rest["items"]], ["r3:2"])
        self.assertEqual([item["id"] for item in after_answered["items"]], ["r3:2"])

    def test_open_asks_lists_unanswered_asks_oldest_first_until_answered(self):
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

            def run(*argv):
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
                             [("early", "r/b", [1, 2]), ("late", "a", []),
                              ("c8f79c0f-1111-4222-8333-444455556666", "c", [])])
            self.assertEqual([(ask["id"], ask["open_questions"]) for ask in partly],
                             [("early", [2]), ("c8f79c0f-1111-4222-8333-444455556666", [])])
            self.assertEqual((by_prefix[0], recorded[-1]), (0, "c8f79c0f-1111-4222-8333-444455556666"))
            self.assertEqual(run("open-asks")[1], "")
            self.assertEqual(unknown[0], 1)
            self.assertIn("done1", unknown[2])

    def test_watch_shows_every_ask_and_done_and_reminds_of_asks_left_unanswered(self):
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

            def watch():
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

    def test_summary_prints_one_short_line_per_entry(self):
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

    def test_summary_names_each_entry_and_id_reads_only_those_in_full(self):
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

    def test_id_reads_an_8_character_prefix_and_acknowledged_lines_without_moving_the_cursor(self):
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

            def run(*argv):
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

    def test_an_ask_s_questions_show_in_full_reads_and_in_summaries(self):
        # 2026-10-28: read --id of epm-11432-review-fix's ask d57cdf88 showed only its one-line text.
        asked = [{"question": "どちらで直しますか？", "header": "方針", "multiSelect": False, "recommended": "A",
                  "issue": "spin-dd/taihei-epm-server#11432",
                  "options": [{"label": "A", "description": "画面で止める"}, {"label": "B", "description": "API で止める"}]},
                 {"question": "いつ出しますか？", "header": "時期", "multiSelect": False, "recommended": "今日",
                  "options": [{"label": "今日", "description": "devel へ"}, {"label": "明日", "description": "本番と同時"}]}]
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            write_jsonl(inbox, [{"id": "d57cdf88-1111-4222-8333-444455556666", "ts": "t1", "kind": "ask", "host": "h",
                                 "worktree": "epm-11432-review-fix", "text": "伺い直します", "questions": asked}])

            def run(*argv):
                with patch.dict(os.environ, {"TRIAGE_INBOX": str(inbox),
                                             "TRIAGE_READ_OFFSET": str(Path(root) / "read.offset")}), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    self.assertEqual(inbox_read.main(["read", *argv]), 0)
                return out.getvalue()

            full, summary = run("--id", "d57cdf88"), run("--summary")

        self.assertIn("伺い直します\nQ1 id=d57cdf88-1111-4222-8333-444455556666:1 [方針] どちらで直しますか？（推奨: A）\n"
                      "  - A: 画面で止める\n  - B: API で止める\n  関わる issue: spin-dd/taihei-epm-server#11432\n"
                      "Q2 id=d57cdf88-1111-4222-8333-444455556666:2 [時期]", full)
        self.assertIn("伺い直します  [質問 2 件: 方針・時期]", summary.splitlines()[0])

    def test_brief_keeps_asks_and_done_full_and_shortens_only_info(self):
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

    def test_live_tab_of_a_run_marked_dispatch_failed_keeps_the_default_cursor(self):
        with tempfile.TemporaryDirectory() as root:
            inbox = Path(root) / "inbox.jsonl"
            cursor = Path(root) / "default.offset"
            write_jsonl(inbox, [{"kind": "ask", "text": "new question"}])
            cursor.write_text("0")
            runs = [{"status": "dispatch_failed", "terminalPaneKey": "tab15:leaf"},
                    {"status": "completed", "terminalPaneKey": "tab14:leaf"}]
            terminals = [{"tabId": "tab15", "leafId": "leaf", "connected": True, "orphaned": True},
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

    def test_closed_latest_run_does_not_return_cursor_ownership_to_older_tab(self):
        runs = [{"status": "completed", "terminalPaneKey": None},
                {"status": "completed", "terminalPaneKey": "old-pane"}]
        self.assertIsNone(hourly_runtime.latest_pane(runs))

    def test_only_latest_scheduled_pane_advances_default_cursor(self):
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
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(inbox_read.main(["ack", str(inbox.stat().st_size)]), 1)
                self.assertIn("latest scheduled", error.getvalue())
                self.assertEqual(cursor.read_text(), "0")
                os.environ["ORCA_PANE_KEY"] = "new-pane"
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(inbox_read.main(["ack", str(inbox.stat().st_size)]), 0)
                self.assertEqual(int(cursor.read_text()), inbox.stat().st_size)

    def test_late_remote_report_is_read_even_with_an_old_timestamp(self):
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
    def test_alerts_only_new_complete_asks_and_advances_its_own_cursor(self):
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

    def test_notification_uses_the_first_line_as_arguments(self):
        item = {"kind": "ask", "worktree": "sample-lane", "text": "first line\nprivate second line"}
        with patch.object(inbox_alert.subprocess, "run") as run, redirect_stdout(io.StringIO()):
            inbox_alert.notify(item, NoTab(), inbox_alert.AppleScript())
        args = run.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/osascript")
        self.assertIn("sample-lane", args[-2])
        self.assertEqual(args[-1], "first line")
        self.assertTrue(run.call_args.kwargs["check"])

    def test_pressing_the_notification_runs_the_senders_click_action(self):
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

    def test_orca_switches_to_the_sender_tab_on_the_mac_that_sent_the_ask(self):
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

    def test_a_slow_paired_server_still_switches_to_the_reported_tab(self):
        answer = fake_orca({}, {"laptop": "Laptop-16"})
        calls = []

        def run(args, **kwargs):
            calls.append(tuple(args[1:3]))
            if calls[-1] == ("terminal", "list"):
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return answer(args, **kwargs)

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
    def __init__(self, action=None):
        self.result = action

    def action(self, item):
        return self.result


def fake_orca(terminals, machines):
    """Answer the Orca CLI calls the alert makes, keyed by the --environment they target."""
    def run(args, **kwargs):
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


def published(url, live="arming"):
    return tool_result(f"Published page.html at {url} (Version 1)\n\nLive subscription: {live} in the background",
                       {"url": url, "liveSubscription": live})


def unwatched(url):
    return tool_result(f"Stopped watching {url}; republishes of it will no longer be reported in this session.",
                       {"unwatch": {"url": url, "was_watching": True}})


def watch_ended(url):
    """Claude Code's notice that a watch ended on its own, as the connection kept failing."""
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": (
        "<task-notification>\n<task-type>artifact-watch-lifecycle</task-type>\n"
        f"<event>Watch on {url} ended — the live connection kept failing and reconnecting has stopped.</event>\n"
        "</task-notification>")}}


class StopCheckTests(unittest.TestCase):
    """The Stop hook keeps a triage turn open while its review queue is not classed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "s1"
        self.dir.mkdir()
        (self.root / "inbox.jsonl").write_text("")
        (self.root / "read.offset").write_text("0")

    def tearDown(self):
        self.tmp.cleanup()

    def queue(self, *paths, at="2026-10-07T10:00:00+00:00"):
        (self.dir / "review-queue.json").write_text(json.dumps(
            {"generated_at": at, "rows": [{"path": p} for p in paths]}))

    def result(self, *paths, at="2026-10-07T10:00:00+00:00"):
        (self.dir / "review-result.json").write_text(json.dumps(
            {"queue_generated_at": at, "rows": [{"path": p, "class": "blocked"} for p in paths]}))

    def check(self, active=False, cwd=None, orca="false", transcript=None, watch_running=True):
        hook = {"session_id": "s1", "stop_hook_active": active}
        if cwd:
            hook["cwd"] = cwd
        if transcript:
            hook["transcript_path"] = str(transcript)
        paths = {"TRIAGE_INBOX": os.environ.get("TRIAGE_INBOX", str(self.root / "inbox.jsonl")),
                 "TRIAGE_READ_OFFSET": os.environ.get("TRIAGE_READ_OFFSET", str(self.root / "read.offset"))}
        with patch.dict(os.environ, paths), patch.object(stopcheck, "inbox_service_running", return_value=watch_running):
            return stopcheck.decide(hook, self.root, orca=orca)

    def fake_orca(self, automations, runs):
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

    def test_lets_the_turn_end_without_a_queue_or_with_an_empty_one(self):
        self.assertIsNone(self.check())
        self.queue()
        self.assertIsNone(self.check())

    def test_blocks_while_queued_rows_are_not_classed(self):
        self.queue("/w/a", "/w/b")
        blocked = self.check()
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("/w/a", blocked["reason"])
        self.assertIn("orca-row-classify", blocked["reason"])

        self.result("/w/a")
        self.assertIn("/w/b", self.check()["reason"])
        self.assertNotIn("/w/a", self.check()["reason"])

    def test_a_result_for_an_older_queue_does_not_count(self):
        self.result("/w/a", at="2026-10-07T09:00:00+00:00")
        self.queue("/w/a")
        self.assertEqual(self.check()["decision"], "block")

    def test_lets_the_turn_end_once_every_row_is_classed(self):
        self.queue("/w/a", "/w/b")
        self.result("/w/a", "/w/b")
        self.assertIsNone(self.check())

    def test_unread_ask_blocks_even_on_repeated_stop_until_ack(self):
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
                self.assertEqual(result["decision"], "block")
                self.assertIn("sample-lane", result["reason"])
                self.assertNotIn("partial", result["reason"])
            os.environ["ORCA_PANE_KEY"] = "old-pane"
            self.assertIsNone(self.check(active=True, cwd="/w/triage", orca=orca))
            os.environ["ORCA_PANE_KEY"] = "new-pane"
            cursor.write_text(str(inbox.stat().st_size - len('{"kind":"ask","worktree":"partial"')))
            self.assertIsNone(self.check(active=True, cwd="/w/triage", orca=orca))

    def test_an_ask_the_scheduled_tab_sent_itself_is_not_unread(self):
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
            self.assertEqual(self.check(active=True, cwd="/w/triage", orca=orca)["decision"], "block")

    def test_blocks_once_then_lets_a_repeated_stop_through(self):
        self.queue("/w/a")
        self.assertEqual(self.check()["decision"], "block")
        self.assertIsNone(self.check(active=True))

    def test_a_verdict_without_a_known_class_does_not_count(self):
        self.queue("/w/a")
        (self.dir / "review-result.json").write_text(json.dumps(
            {"queue_generated_at": "2026-10-07T10:00:00+00:00", "rows": [{"path": "/w/a", "class": "maybe"}]}))
        self.assertEqual(self.check()["decision"], "block")

    def test_warns_when_the_external_inbox_service_is_stopped(self):
        self.queue()
        orca = self.fake_orca([triage_automation("a1", "/w/triage")], {"a1": []})
        reason = self.check(cwd="/w/triage", orca=orca, watch_running=False)["reason"]
        self.assertIn("watch-inbox", reason)
        self.assertIn("~/.cache/orca-briefs/offsets/close-done-triage.offset", reason)
        self.assertIsNone(self.check(cwd="/w/triage", orca=orca, watch_running=True))

    def test_monitor_in_another_tab_does_not_replace_external_service(self):
        with patch.object(stopcheck, "triage_automations", return_value=[{"id": "a1"}]), \
                patch.object(stopcheck, "inbox_service_running", return_value=False):
            reason = stopcheck.missing_inbox_watch("/w/triage", "orca")
        self.assertIn("external triage inbox service", reason)

    def test_external_service_status_controls_stop_notice(self):
        with patch.object(stopcheck.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "running")) as run:
            self.assertTrue(stopcheck.inbox_service_running())
        self.assertEqual(run.call_args.args[0][-1], "status")
        with patch.object(stopcheck.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1, "stopped")):
            self.assertFalse(stopcheck.inbox_service_running())

    def test_blocks_while_an_artifact_this_session_published_is_still_watched(self):
        transcript = self.root / "session.jsonl"
        write_jsonl(transcript, [published("https://claude.ai/artifact/A"),
                                 published("https://claude.ai/artifact/B", live="flag_off")])

        blocked = self.check(transcript=transcript)

        self.assertEqual(blocked["decision"], "block")
        self.assertIn("https://claude.ai/artifact/A", blocked["reason"])
        self.assertNotIn("artifact/B", blocked["reason"])
        self.assertIn("ArtifactComments", blocked["reason"])
        self.assertIsNone(self.check(active=True, transcript=transcript))

        def append(entry):
            with transcript.open("a") as f:
                f.write(json.dumps(entry) + "\n")

        append(user("a later turn that never stops the watch"))
        self.assertEqual(self.check(transcript=transcript)["decision"], "block")
        append(unwatched("https://claude.ai/artifact/A"))
        self.assertIsNone(self.check(transcript=transcript))

    def test_a_watch_that_ended_on_its_own_lets_the_turn_end(self):
        transcript = self.root / "session.jsonl"
        write_jsonl(transcript, [published("https://claude.ai/artifact/A"), watch_ended("https://claude.ai/artifact/A")])

        self.assertIsNone(self.check(transcript=transcript))

    def test_a_damaged_queue_or_result_lets_the_turn_end(self):
        (self.dir / "review-queue.json").write_text(json.dumps({"generated_at": "g", "rows": [{"nopath": 1}]}))
        self.assertIsNone(self.check())
        (self.dir / "review-queue.json").write_text("[]")
        self.assertIsNone(self.check())



class VerdictStoreTests(unittest.TestCase):
    """A careful read is kept only while what it read still holds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root / "verdicts.json"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def row(path, final_at="t1", pr=None, dirty=0, cls="stalled", codex_at=None):
        return {"path": path, "class": cls, "jev": {"asks": 0.1}, "pr": pr, "git": {"dirty": dirty},
                "claude": {"final_at": final_at} if final_at else None,
                "codex": [{"last_assistant_at": codex_at}] if codex_at else []}

    def test_a_verdict_lapses_when_the_pr_or_the_tree_changed_or_nothing_was_read(self):
        open_pr = {"state": "OPEN", "number": 7, "head_oid": "a"}
        read = self.row("/w/a", pr=open_pr)
        verdicts = {"/w/a": {"read": scan.read_key(read), "class": "blocked"},
                    "/w/none": {"read": scan.read_key(self.row("/w/none", final_at=None)), "class": "blocked"}}

        self.assertEqual(scan.review_queue([read], verdicts), [])
        for changed in (self.row("/w/a", pr={**open_pr, "state": "MERGED"}),
                        self.row("/w/a", pr={**open_pr, "head_oid": "b"}),
                        self.row("/w/a", pr=open_pr, dirty=2),
                        self.row("/w/a", pr=open_pr, final_at="t2")):
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

    def test_a_new_human_review_returns_a_row_to_the_review_queue(self):
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

    def test_remote_and_local_rows_with_the_same_path_keep_separate_verdicts(self):
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

    def test_a_new_pr_comment_returns_a_row_to_the_review_queue(self):
        old = self.row("/w/review-lane", pr={"state": "OPEN", "number": 42,
                                             "latest_human_activity": None})
        verdicts = {old["path"]: {"read": scan.read_key(old), "class": "blocked"}}
        updated = self.row(old["path"], pr={"state": "OPEN", "number": 42,
                                           "latest_human_activity": {"author": "reviewer",
                                                                     "kind": "comment",
                                                                     "at": "2026-10-21T02:00:00Z"}})
        self.assertEqual(scan.review_queue([old], verdicts), [])
        self.assertEqual([row["path"] for row in scan.review_queue([updated], verdicts)], [old["path"]])

    def test_the_newer_of_the_claude_and_codex_messages_is_what_was_read(self):
        self.assertEqual(scan.row_final_at(self.row("/w", final_at="2026-10-07T01:00:00Z",
                                                     codex_at="2026-10-07T02:00:00Z")), "2026-10-07T02:00:00Z")
        self.assertEqual(scan.row_final_at(self.row("/w", final_at="2026-10-07T03:00:00Z",
                                                     codex_at="2026-10-07T02:00:00Z")), "2026-10-07T03:00:00Z")

    def write_session(self, name, queue_at, rows, result_at, verdicts):
        session = self.root / name
        session.mkdir()
        (session / "review-queue.json").write_text(json.dumps({"generated_at": queue_at, "rows": rows}))
        (session / "review-result.json").write_text(json.dumps({"queue_generated_at": result_at, "rows": verdicts}))

    def test_every_session_s_classed_queue_feeds_the_store_and_bad_verdicts_do_not(self):
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

    def test_an_older_session_s_read_does_not_replace_a_newer_one_in_the_store(self):
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

    def test_a_damaged_store_is_kept_aside_not_overwritten(self):
        self.store.write_text("[1, 2")
        self.write_session("s1", "g1", [{"path": "/w/a", "read": "ka"}], "g1", [{"path": "/w/a", "class": "blocked"}])

        verdicts = scan.absorb_verdicts(self.store)

        self.assertEqual(verdicts, {"/w/a": {"class": "blocked", "read": "ka", "queued_at": "g1"}})
        self.assertEqual((self.root / "verdicts.json.bad").read_text(), "[1, 2")

if __name__ == "__main__":
    unittest.main()
