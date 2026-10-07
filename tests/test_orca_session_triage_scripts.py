"""Offline behavior tests for the orca-session-triage command line tools."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "orca-session-triage" / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = load_script("scan_sessions")
rmcheck = load_script("rm_check")
precheck = load_script("precheck")
stopcheck = load_script("stop_check")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def text(value):
    return {"type": "text", "text": value}


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
        with open(path, "a") as handle:
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
        with open(broken, "a") as handle:
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

    def test_each_agent_orca_marks_done_is_a_done_card_with_the_tab_that_closes_it(self):
        rows = self.inventory(
            [{**worktree("/w/d"), "agents": [
                done_agent("codex", "tab1:leaf1", said="PR #5 is open for review.", asked="fix #4",
                           minutes_ago=120),
                {"agentType": "claude", "state": "working", "paneKey": "tab2:leaf2"}]}],
            [in_pane(terminal("/w/d", "codex", "term_codex"), "tab1:leaf1"),
             in_pane(terminal("/w/d", "claude", "term_claude"), "tab2:leaf2")])

        self.assertEqual(rows[0]["done_cards"], [
            {"agent": "codex", "handle": "term_codex", "done_at": "2027-01-15T06:00:00Z",
             "interrupted": False, "prompt": "fix #4", "last_message": "PR #5 is open for review."}])

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
            "merge_state": "CLEAN", "merged_at": None,
            "checks": {"SUCCESS": 2, "IN_PROGRESS": 1, "FAILURE": 1}})

    def test_a_failed_or_missing_gh_degrades_to_no_pr(self):
        with patch.object(scan.subprocess, "run", side_effect=FileNotFoundError("gh")):
            self.assertIsNone(scan.gh_pr("acme/app", 7))
        with patch.object(scan.subprocess, "run", side_effect=scan.subprocess.TimeoutExpired("gh", 30)):
            self.assertIsNone(scan.gh_pr("acme/app", 7))
        failed = scan.subprocess.CompletedProcess([], 1, stdout="", stderr="no pull requests found")
        with patch.object(scan.subprocess, "run", return_value=failed):
            self.assertIsNone(scan.gh_pr("acme/app", "fix-a"))


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
        def stalled(path, said_at, status="blocked_on_others", source="claude"):
            transcripts = ({"claude": {"final_text": "PR を出しました。レビュー待ちです。", "final_at": said_at},
                            "codex": []} if source == "claude" else
                           {"claude": None, "codex": [{"last_assistant": "PR を出しました。",
                                                       "last_assistant_at": said_at}]})
            return {"path": path, "class": "stalled", "linked_pr": {"number": 7, "state": "open"},
                    "reasons": ["final message waits on someone else (Jev 0.90)"],
                    "jev": {"source": source, "asks": 0.1, "status": status, "confidence": 0.9}, **transcripts}

        rows = [stalled("/w/merged-later", "2026-10-06T07:25:00.000Z"),
                stalled("/w/codex-merged-later", "2026-10-06T07:25:00.000Z", source="codex"),
                stalled("/w/merged-before", "2026-10-06T07:40:00.000Z"),
                stalled("/w/continues", "2026-10-06T07:25:00.000Z", status="in_progress"),
                stalled("/w/other-head", "2026-10-06T07:25:00.000Z")]
        git = {"branch": "fix-a", "head": "aaa", "dirty": 0, "unpushed": 0, "github": "acme/app"}
        heads = {"/w/other-head": "zzz"}
        current = {}

        def pr_lookup(slug, ref):
            return {"number": ref, "state": "MERGED", "merged_at": "2026-10-06T07:35:27Z",
                    "head_oid": heads.get(current["path"], "aaa")}

        def git_lookup(path):
            current["path"] = path
            return git

        result = {r["path"]: (r["class"], r["reasons"])
                  for r in scan.add_next_step_facts(rows, git_lookup=git_lookup, pr_lookup=pr_lookup)}

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
        heads = {"/w/no-session-other-head": "zzz"}
        states = {"/w/no-session-open": "OPEN"}
        current = {}

        def git_lookup(path):
            current["path"] = path
            return {"branch": "fix-a", "head": "aaa", "dirty": 0, "unpushed": 0, "github": "acme/app"}

        def pr_lookup(slug, ref):
            path = current["path"]
            return {"number": 7, "state": states.get(path, "MERGED"), "merged_at": "2026-10-06T07:35:27Z",
                    "head_oid": heads.get(path, "aaa")}

        result = {r["path"]: (r["class"], r["reasons"])
                  for r in scan.add_next_step_facts(rows, git_lookup=git_lookup, pr_lookup=pr_lookup)}

        self.assertEqual(result["/w/no-session"], ("finished", ["PR merged"]))
        # Jev's "done" is a fast read: a merged row it judged waits for the careful one.
        self.assertEqual(result["/w/done"][0], "stalled")
        self.assertEqual(result["/w/continues"][0], "stalled")
        self.assertEqual(result["/w/no-session-other-head"][0], "stalled")
        self.assertEqual(result["/w/no-session-open"][0], "stalled")

    def test_the_review_queue_holds_every_row_a_reader_must_class(self):
        # Jev reads fast and shallow: every row it judged from a final message gets
        # one careful read, and the verdict is kept until that message changes.
        def row(path, cls, pr=None, jev=True, final_at="t1", **extra):
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
            "path": "/w/unsure", "repo": "app", "class": "unsure", "reasons": ["r"], "comment": "c",
            "jev": {"asks": 0.5}, "pr": None, "git": {"dirty": 0},
            "claude": {"last_user": "u", "final_text": "f", "final_at": "t1", "transcript": "/x.jsonl"},
            "codex": {"last_assistant": "new", "last_assistant_at": "t1", "rollout": "/r.jsonl"},
            "final_at": "t1", "read": scan.read_key(rows[0])})

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

        result = scan.add_next_step_facts(rows, git_lookup=gits.get, pr_lookup=pr_lookup)

        self.assertEqual(asked, [("acme/app", 7), ("acme/app", "fix-b"), ("acme/app", 8)])
        self.assertEqual([(r.get("git"), r.get("pr")) for r in result], [
            (gits["/w/linked"], {"number": 7, "head_oid": "aaa", "head_matches": True}),
            (gits["/w/branch"], {"number": "fix-b", "head_oid": "aaa", "head_matches": False}),
            (gits["/w/forgejo"], None),
            (gits["/w/asks"], {"number": 8, "head_oid": "aaa", "head_matches": True}), (None, None)])
        self.assertNotIn("git", rows[0])


class ScanCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # A host's real Jev key and verdict store must never reach a test run.
        for hermetic in (patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}),
                         patch.object(scan, "JEV_KEY_FILE", str(self.dir / "no-key")),
                         patch.object(scan, "VERDICTS_FILE", str(self.dir / "verdicts.json"))):
            hermetic.start()
            self.addCleanup(hermetic.stop)

    def tearDown(self):
        self.tmp.cleanup()

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
        self.assertEqual(self.check()["blockers"], [])

        (self.wt / "other.txt").write_text("only here\n")
        git(self.wt, "add", "other.txt")
        git(self.wt, "commit", "-q", "-m", "unmerged")
        self.assertEqual(len(self.check()["blockers"]), 1)

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

    def test_unmerged_commit_blocks_unless_it_is_the_merged_pr_head(self):
        (self.wt / "app.txt").write_text("v2\n")
        git(self.wt, "commit", "-q", "-am", "change")
        head = git(self.wt, "rev-parse", "HEAD")

        blocked = self.check()
        squashed = self.check(pr={"number": 7, "state": "MERGED", "headRefOid": head})
        reopened = self.check(pr={"number": 7, "state": "OPEN", "headRefOid": head})

        self.assertEqual(blocked["blockers"], ["HEAD %s is not in main" % head[:8]])
        self.assertTrue(squashed["ok"])
        self.assertEqual(reopened["blockers"], ["HEAD %s is not in main and PR #7 is OPEN" % head[:8]])

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
        os.symlink(str(Path(self.tmp.name) / "gone"), str(self.wt / "uploads"))

        result = self.check()

        self.assertEqual(result["review"], ["uploads (differs from main checkout)"])

    def test_compose_placeholder_entries_never_match_the_worktree(self):
        projects = [{"Name": "elsewhere", "Status": "running(1)", "ConfigFiles": "/other/compose.yaml,-"}]
        cwd = os.getcwd()
        os.chdir(self.wt)
        try:
            result = rmcheck.check(self.wt, self.main, base="main", compose_projects=projects)
        finally:
            os.chdir(cwd)

        self.assertEqual(result["containers"], [])

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

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = rmcheck.main(["--worktree", str(self.wt), "--base", "main", "--no-docker", *argv])
        return code, json.loads(out.getvalue())

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
            code = rmcheck.main(["--worktree", str(self.wt), "--no-docker"])

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


class PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ps = Path(self.tmp.name) / "ps.json"

    def run_precheck(self, worktrees, workspace="/w/triage"):
        self.ps.write_text(json.dumps({"result": {"worktrees": worktrees}}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = precheck.main(["--ps-json", str(self.ps), "--workspace", workspace])
        return code, out.getvalue()

    def test_skips_while_an_agent_in_the_workspace_works_is_blocked_or_waits(self):
        for state in ("working", "blocked", "waiting"):
            with self.subTest(state=state):
                code, out = self.run_precheck([worktree("/w/triage", state)])

                self.assertEqual(code, 1)
                self.assertIn(f"skip: an agent in /w/triage is {state}", out)

    def test_skip_reason_names_a_background_job_left_after_the_agents_turn(self):
        code, out = self.run_precheck([worktree("/w/triage", "working", turn="done")])

        self.assertEqual(code, 1)
        self.assertIn("skip: an agent in /w/triage is working (a background job after its turn)", out)

    def test_runs_when_the_workspace_agents_are_done_whatever_other_worktrees_do(self):
        code, out = self.run_precheck([worktree("/w/triage", "done"), worktree("/w/lane", "waiting")])

        self.assertEqual(code, 0)
        self.assertIn("run: no agent in /w/triage works or waits", out)

    def test_matches_the_workspace_through_a_symlink(self):
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(real)

        code, out = self.run_precheck([worktree(str(link), "waiting")], workspace=str(real))

        self.assertEqual(code, 1)

    def test_requires_the_workspace_since_orca_runs_a_precheck_in_the_main_checkout(self):
        self.ps.write_text(json.dumps({"result": {"worktrees": [worktree("/w/triage", "waiting")]}}))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
            precheck.main(["--ps-json", str(self.ps)])

        self.assertEqual(exit.exception.code, 2)

    def test_skips_when_orca_does_not_know_the_workspace(self):
        code, out = self.run_precheck([worktree("/w/lane", "done")])

        self.assertEqual(code, 2)
        self.assertIn("/w/triage is not an Orca worktree", out)

    def test_skip_reason_carries_what_orca_said_on_stderr(self):
        refusing = Path(self.tmp.name) / "refusing-orca"
        refusing.write_text("#!/bin/sh\necho 'runtime not reachable' >&2\nexit 3\n")
        refusing.chmod(0o755)
        out = io.StringIO()
        with redirect_stdout(out):
            code = precheck.main(["--orca", str(refusing), "--workspace", "/w/triage"])

        self.assertEqual(code, 2)
        self.assertIn("skip: could not read Orca worktrees: runtime not reachable", out.getvalue())

    def test_says_when_orca_cut_its_list_before_the_workspace(self):
        self.ps.write_text(json.dumps({"result": {"worktrees": [worktree("/w/lane", "done")], "truncated": True}}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = precheck.main(["--ps-json", str(self.ps), "--workspace", "/w/triage"])

        self.assertEqual(code, 2)
        self.assertIn("skip: /w/triage is not among the 1 worktrees Orca listed before cutting the list short",
                      out.getvalue())

    def test_skips_with_the_reason_when_orca_answers_in_another_shape(self):
        for answer in ({"result": {"worktrees": None}}, {"result": {"worktrees": [{"agents": []}]}}, ["x"]):
            with self.subTest(answer=answer):
                self.ps.write_text(json.dumps(answer))
                out = io.StringIO()
                with redirect_stdout(out):
                    code = precheck.main(["--ps-json", str(self.ps), "--workspace", "/w/triage"])

                self.assertEqual(code, 2)
                self.assertIn("skip: could not read Orca worktrees", out.getvalue())

    def test_skips_with_the_reason_when_the_orca_cli_fails(self):
        garbled = Path(self.tmp.name) / "garbled-orca"
        garbled.write_text("#!/bin/sh\necho not json\n")
        garbled.chmod(0o755)
        for orca in (str(Path(self.tmp.name) / "missing-orca"), "false", str(garbled)):
            with self.subTest(orca=orca):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = precheck.main(["--orca", orca, "--workspace", "/w/triage"])

                self.assertEqual(code, 2)
                self.assertIn("skip: could not read Orca worktrees", out.getvalue())


class StopCheckTests(unittest.TestCase):
    """The Stop hook keeps a triage turn open while its review queue is not classed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dir = self.root / "s1"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def queue(self, *paths, at="2026-10-07T10:00:00+00:00"):
        (self.dir / "review-queue.json").write_text(json.dumps(
            {"generated_at": at, "rows": [{"path": p} for p in paths]}))

    def result(self, *paths, at="2026-10-07T10:00:00+00:00"):
        (self.dir / "review-result.json").write_text(json.dumps(
            {"queue_generated_at": at, "rows": [{"path": p, "class": "blocked"} for p in paths]}))

    def check(self, active=False):
        return stopcheck.decide({"session_id": "s1", "stop_hook_active": active}, self.root)

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

    def test_blocks_once_then_lets_a_repeated_stop_through(self):
        self.queue("/w/a")
        self.assertEqual(self.check()["decision"], "block")
        self.assertIsNone(self.check(active=True))

    def test_a_verdict_without_a_known_class_does_not_count(self):
        self.queue("/w/a")
        (self.dir / "review-result.json").write_text(json.dumps(
            {"queue_generated_at": "2026-10-07T10:00:00+00:00", "rows": [{"path": "/w/a", "class": "maybe"}]}))
        self.assertEqual(self.check()["decision"], "block")

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
                                    "/w/a": {"class": "blocked", "read": "ka"},
                                    "/w/c": {"class": "finished", "read": "kc"}})
        self.assertEqual(json.loads(self.store.read_text()), verdicts)

    def test_a_damaged_store_is_kept_aside_not_overwritten(self):
        self.store.write_text("[1, 2")
        self.write_session("s1", "g1", [{"path": "/w/a", "read": "ka"}], "g1", [{"path": "/w/a", "class": "blocked"}])

        verdicts = scan.absorb_verdicts(self.store)

        self.assertEqual(verdicts, {"/w/a": {"class": "blocked", "read": "ka"}})
        self.assertEqual((self.root / "verdicts.json.bad").read_text(), "[1, 2")

if __name__ == "__main__":
    unittest.main()
