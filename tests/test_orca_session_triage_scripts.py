"""Offline behavior tests for the orca-session-triage command line tools."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "orca-session-triage" / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = load_script("scan_sessions")
rmcheck = load_script("rm_check")


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
            assistant(text("Done. Shall I merge it?")),
        ])

        result = scan.read_claude(self.projects, self.worktree)

        self.assertEqual(result["last_user"], "second request")
        self.assertEqual(result["final_text"], "Done. Shall I merge it?")
        self.assertEqual(result["pending_questions"], [])

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
        self.assertEqual(session["pending_requests"], [{
            "asked_at": "2026-10-01T01:06:00Z",
            "questions": [{"title": "SSH host?"}],
            "answered_in_chat_later": True,
        }])


def worktree(path, state=None, agent="claude", comment="", pr=None, main=False):
    return {
        "worktreeId": "repo::" + path, "path": path, "repo": "app", "branch": "refs/heads/x",
        "workspaceStatus": "in-progress", "comment": comment, "linkedPR": pr,
        "isMainWorktree": main,
        "agents": [{"agentType": agent, "state": state}] if state else [],
    }


def terminal(path, agent, handle="term_1", title="t", preview="", quiet_minutes=0, orphaned=False):
    return {"worktreeId": "repo::" + path, "handle": handle, "agentIdentity": agent,
            "title": title, "connected": not orphaned, "orphaned": orphaned, "preview": preview,
            "lastOutputAt": (NOW - quiet_minutes * 60) * 1000}


NOW = 1_800_000_000
PROMPT = "app on main via python\n❯"


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def claude_says(self, path, *rows):
        write_jsonl(scan.claude_project_dir(self.projects, path) / "s.jsonl", list(rows))

    def inventory(self, worktrees, terminals, codex=None):
        return scan.build_inventory(worktrees, terminals, self.projects, codex or {}, "要判断", now=NOW)

    def classes(self, worktrees, terminals, codex=None):
        return {r["path"]: (r["class"], r["reasons"]) for r in self.inventory(worktrees, terminals, codex)}

    def closable(self, worktrees, terminals, codex=None):
        return {r["path"]: r["closable"] for r in self.inventory(worktrees, terminals, codex)}

    def test_a_finished_setup_shell_is_closable_while_the_agent_works(self):
        result = self.closable(
            [worktree("/w/a", "working")],
            [terminal("/w/a", "claude", "term_agent"),
             terminal("/w/a", None, "term_setup", title="Setup", preview=PROMPT, quiet_minutes=90)])

        self.assertEqual(result["/w/a"], ["term_setup"])

    def test_an_idle_worktree_offers_its_agents_but_not_a_busy_or_recent_shell(self):
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

    def test_orphaned_terminal_records_are_ignored(self):
        result = self.inventory(
            [worktree("/w/c", None)],
            [terminal("/w/c", "codex", "term_gone", orphaned=True)])

        self.assertEqual([(r["class"], r["terminals"], r["closable"]) for r in result],
                         [("idle", [], [])])

    def test_waiting_comes_from_transcripts_and_the_decision_marker(self):
        self.claude_says("/w/a", user("go"), assistant(
            {"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": {"questions": []}}))
        self.claude_says("/w/b", user("go"), assistant(text("PR is up.\nShall I merge it?")))
        result = self.classes(
            [worktree("/w/a", "working"), worktree("/w/b", "done"),
             worktree("/w/c", "working", comment="要判断: which base?"),
             worktree("/w/main", main=True)],
            [terminal("/w/a", "claude"), terminal("/w/b", "claude"), terminal("/w/c", "claude")])

        self.assertEqual(result["/w/a"], ("waiting", ["AskUserQuestion pending"]))
        self.assertEqual(result["/w/b"], ("waiting", ["final message asks"]))
        self.assertEqual(result["/w/c"], ("waiting", ["decision marker in comment"]))
        self.assertNotIn("/w/main", result)

    def test_a_codex_final_message_that_asks_is_waiting(self):
        codex = {"/w/q": [{"rollout": "r", "last_user": "ship it", "last_role": "assistant",
                           "last_assistant": "PR #1 is green.\nPR #1 をマージしてよいですか？\n"
                                             "AGENTS.md で確認が必要と定めているため、ここで確認しています。",
                           "pending_requests": []}],
                 "/w/r": [{"rollout": "r", "last_user": "マージしてよいですか？", "last_role": "user",
                           "last_assistant": "Shall I merge?", "pending_requests": []}]}
        result = self.classes(
            [worktree("/w/q", "done", agent="codex", pr={"number": 1, "state": "merged"}),
             worktree("/w/r", "working", agent="codex")],
            [terminal("/w/q", "codex"), terminal("/w/r", "codex")], codex)

        self.assertEqual(result["/w/q"], ("waiting", ["Codex final message asks"]))
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
            codex)

        self.assertEqual(result["/w/d"], ("waiting", ["Codex request pending"]))
        self.assertEqual(result["/w/e"], ("working", []))
        self.assertEqual(result["/w/f"], ("unstarted", ["codex terminal without a recent transcript"]))
        self.assertEqual(result["/w/g"], ("finished", ["PR merged"]))
        self.assertEqual(result["/w/h"], ("idle", []))


class ScanCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

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

    def test_uncommitted_changes_block_removal(self):
        (self.wt / "app.txt").write_text("v2\n")
        (self.wt / "new.txt").write_text("draft\n")

        result = self.check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["blockers"], ["2 uncommitted changes"])

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


if __name__ == "__main__":
    unittest.main()
