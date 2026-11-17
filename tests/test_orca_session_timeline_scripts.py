"""Offline behavior tests for the timeline that merges the lead's and the lanes' records."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

NO_BOARD_COMPONENT = tempfile.TemporaryDirectory(prefix="triage-tests-bin-")
os.environ["ORCA_BRIEFS_BIN"] = NO_BOARD_COMPONENT.name


SCRIPT = Path(__file__).resolve().parents[1] / "orca-session-triage" / "scripts" / "timeline.py"
# orca is outside this repository: answer `orca terminal list --json` with the terminals in FAKE_ORCA_TERMINALS.
FAKE_ORCA = """#!{python}
import json, os, sys

if sys.argv[1:3] != ["terminal", "list"]:
    sys.exit("unexpected orca call: " + " ".join(sys.argv[1:]))
print(json.dumps({{"ok": True, "result": {{"terminals": json.loads(os.environ["FAKE_ORCA_TERMINALS"])}}}}))
"""
LEAD = "term_lead-0000-0000-0000-000000000001"
LANE = "term_lane-0000-0000-0000-000000000002"
OTHER = "term_other-000-0000-0000-000000000003"


class TimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.workspaces = self.home / "orca" / "workspaces"
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        (bin_dir / "orca").write_text(FAKE_ORCA.format(python=sys.executable), encoding="utf-8")
        (bin_dir / "orca").chmod(0o755)
        terminals = [{"handle": LEAD, "worktreePath": str(self.workspaces / "orca" / "triage-lead")}]
        unset = ("TRIAGE_INBOX", "TRIAGE_ASK_ANSWERS", "TRIAGE_LEAD_DECISIONS", "ORCA_BOARD_EVENTS",
                 "ORCA_BRIEFS_ROOT", "ADOPTION_TRIAGE_EVENTS", "ADOPTION_TRIAGE_IMPORTS", "ORCA_WORKSPACES")
        self.env = {**{key: value for key, value in os.environ.items() if key not in unset},
                    "HOME": str(self.home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_ORCA_TERMINALS": json.dumps(terminals)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def append(self, relative: str, *records: dict) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.writelines(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        return path

    def send(self, ts: str, to: str, text: str) -> dict:
        return {"id": f"send-{ts}-{to[-4:]}", "ts": ts, "type": "send", "source": "send", "host": "mac",
                "from": LEAD, "to": to, "lane": to, "worktree": "scratchpad", "text": text,
                "step_add": [], "environment": None, "delivery": ["input_accepted"]}

    def report(self, ts: str, handle: str, worktree: str, kind: str, text: str, report_id: str = "") -> dict:
        return {"id": report_id or f"report-{ts}", "host": "mac", "ts": ts, "worktree": worktree,
                "repo": "app", "handle": handle, "kind": kind, "text": text}

    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_a_lane_post_reads_between_the_lead_sends_around_it(self) -> None:
        self.append(".local/share/orca-board/events.jsonl",
                    self.send("2026-11-15T16:35:17+0900", LANE, "答えです。#101 に投稿してください。"),
                    self.send("2026-11-15T16:36:44+0900", LANE, "取り消しです。#101 への投稿はしないでください。"))
        self.append(".cache/orca-briefs/triage-inbox.jsonl",
                    self.report("2026-11-15T16:51:43+0900", LANE, "fix-checklist", "done",
                                "#101 は取り消しの連絡の前に投稿済みでした。"))
        lane = self.workspaces / "app" / "fix-checklist"
        call = {"type": "McpToolCall", "server": "codex_apps", "status": "completed", "appName": "GitHub",
                "arguments": {"repo_full_name": "example-org/app", "pr_number": 101}}
        self.append(".codex/sessions/2026/11/15/rollout-2026-11-15T14-46-32-lane.jsonl",
                    {"timestamp": "2026-11-15T05:46:38.654Z", "type": "session_meta",
                     "payload": {"id": "lane", "cwd": str(lane)}},
                    {"timestamp": "2026-11-15T07:35:22.521Z", "type": "event_msg", "payload": {
                        "type": "item_completed", "item": {**call, "id": "exec-read", "tool": "github.fetch_issue",
                                                           "readOnlyHint": True}}},
                    {"timestamp": "2026-11-15T07:35:31.174Z", "type": "event_msg", "payload": {
                        "type": "item_completed", "item": {**call, "id": "exec-post", "tool": "github.add_comment_to_issue",
                                                           "readOnlyHint": False}}})

        lines = self.run_tool("--since", "2026-11-15T16:30").stdout.splitlines()

        self.assertEqual(len(lines), 4, lines)
        self.assertIn("16:35:17  triage-lead → fix-checklist  送った  答えです。", lines[0])
        self.assertIn("16:35:31  fix-checklist  投稿した  GitHub add_comment_to_issue example-org/app#101",
                      lines[1])
        self.assertIn("16:36:44  triage-lead → fix-checklist  送った  取り消しです。", lines[2])
        self.assertIn("16:51:43  fix-checklist  完了が届いた", lines[3])

    def test_a_lane_filter_keeps_every_instruction_to_that_lane_in_order(self) -> None:
        self.append(".cache/orca-briefs/triage-inbox.jsonl",
                    self.report("2026-11-15T14:46:03+0900", OTHER, "fix-orders", "info", "頼まれた事実を調べました。"),
                    self.report("2026-11-15T18:29:08+0900", OTHER, "fix-orders", "done", "PR #102 を開きました。"))
        self.append(".local/share/orca-board/events.jsonl",
                    self.send("2026-11-15T14:52:40+0900", OTHER, "事実をありがとうございます。"),
                    self.send("2026-11-15T18:24:35+0900", OTHER, "merge の伺いは明朝でかまいません。"),
                    self.send("2026-11-15T18:25:07+0900", LANE, "別の担当への至急の依頼です。"),
                    self.send("2026-11-15T18:33:06+0900", OTHER, "【至急・方針の変更】今晩の処理の前に merge して。"),
                    self.send("2026-11-15T18:39:28+0900", OTHER, "方針の再変更: #102 の急ぎは止めてください。"))

        rows = json.loads(self.run_tool("--since", "2026-11-15T18:00", "--lane", "app/fix-orders",
                                        "--json").stdout)

        self.assertEqual([(row["kind"], row["text"][:12]) for row in rows], [
            ("送った", "merge の伺いは明朝"), ("完了が届いた", "PR #102 を開きま"),
            ("送った", "【至急・方針の変更】今晩"), ("送った", "方針の再変更: #102")])
        self.assertEqual({row["to"] for row in rows if row["kind"] == "送った"}, {"fix-orders"})
        # The lane's tab is gone from Orca; its handle still names it through the records.
        self.assertEqual(json.loads(self.run_tool("--since", "2026-11-15T18:00", "--lane", OTHER, "--json").stdout), rows)

    def test_only_completed_writes_from_a_claude_transcript_count(self) -> None:
        lane = self.workspaces / "skills" / "adopt-x"
        project = self.home / ".claude" / "projects" / "".join(c if c.isalnum() else "-" for c in str(lane))
        elsewhere = self.home / ".claude" / "projects" / "-elsewhere"

        def call(ts: str, use_id: str, command: str, background: bool = False) -> dict:
            return {"type": "assistant", "timestamp": ts, "cwd": str(lane), "message": {"content": [
                {"type": "tool_use", "id": use_id, "name": "Bash",
                 "input": {"command": command, "run_in_background": background}}]}}

        def result(ts: str, use_id: str, failed: bool, out: str = "out") -> dict:
            return {"type": "user", "timestamp": ts, "cwd": str(lane), "message": {"content": [
                {"type": "tool_result", "tool_use_id": use_id, "is_error": failed,
                 "content": [{"type": "text", "text": out}]}]}}

        self.append(str(project.relative_to(self.home) / "session.jsonl"),
                    call("2026-11-15T09:00:00Z", "view", "gh pr view 12 -R Eotel/skills"),
                    result("2026-11-15T09:00:01Z", "view", False),
                    call("2026-11-15T09:00:30Z", "help", "gh pr merge --help"),
                    result("2026-11-15T09:00:31Z", "help", False),
                    call("2026-11-15T09:01:00Z", "early", "gh pr merge 12 -R Eotel/skills --squash"),
                    result("2026-11-15T09:01:02Z", "early", True),
                    call("2026-11-15T09:05:00Z", "merge", "cd /tmp && gh pr merge 12 -R Eotel/skills --squash"),
                    result("2026-11-15T09:05:04Z", "merge", False),
                    call("2026-11-15T09:06:00Z", "body", "cat > body.md <<'EOF'\ngh pr merge 12 --squash\nEOF"),
                    result("2026-11-15T09:06:01Z", "body", False),
                    call("2026-11-15T09:07:00Z", "pair", 'gh pr comment 13 --body "ok && go" && gh pr merge 13 --auto'),
                    result("2026-11-15T09:07:03Z", "pair", False),
                    call("2026-11-15T09:08:00Z", "later", "gh pr merge 14 --squash", background=True),
                    result("2026-11-15T09:08:00Z", "later", False),
                    call("2026-11-15T09:09:00Z", "thread", "gh api graphql -f query='query { threads }' | jq . ; "
                         "gh api graphql -f query='mutation { resolveReviewThread }'"),
                    result("2026-11-15T09:09:01Z", "thread", False),
                    call("2026-11-15T09:10:00Z", "preview", "gh pr create --dry-run --title x"),
                    result("2026-11-15T09:10:01Z", "preview", False),
                    call("2026-11-15T09:11:00Z", "hidden", "gh pr merge 15 --squash 2>&1 | tail -1"),
                    result("2026-11-15T09:11:02Z", "hidden", False, "GraphQL: Head branch was modified."),
                    call("2026-11-15T09:12:00Z", "read", "gh api graphql -X POST -f query='query { viewer { login } }'"),
                    result("2026-11-15T09:12:01Z", "read", False))
        self.append(str(elsewhere.relative_to(self.home) / "other.jsonl"),
                    call("2026-11-15T09:13:00Z", "other", "gh pr comment 30 --body x"),
                    result("2026-11-15T09:13:01Z", "other", False))

        rows = json.loads(self.run_tool("--since", "2026-11-15T17:00", "--json").stdout)

        self.assertEqual([(row["at"], row["lane"], row["kind"]) for row in rows], [
            ("2026-11-15T18:05:04+09:00", "adopt-x", "merge した"),
            ("2026-11-15T18:07:03+09:00", "adopt-x", "投稿した"),
            ("2026-11-15T18:07:03+09:00", "adopt-x", "自動 merge を予約した"),
            ("2026-11-15T18:09:01+09:00", "adopt-x", "書いた"),
            ("2026-11-15T18:11:02+09:00", "adopt-x", "merge した（失敗の表示あり）")])

    def test_holds_answers_decisions_starts_and_adoptions_join_the_timeline(self) -> None:
        self.append(".cache/orca-briefs/triage-inbox.jsonl",
                    self.report("2026-11-14T10:00:00+0900", OTHER, "fix-orders", "info", "前日の経過"),
                    {**self.report("2026-11-15T15:09:25+0900", LANE, "fix-checklist", "ask", "伺い直します。",
                                   "43cc0f6c-1111-2222-3333-444444444444"),
                     "questions": [{"header": "資料", "question": "どの版の資料を使いますか？", "recommended": "最新版",
                                    "issue": "example-org/app#103"}]})
        self.append(".cache/orca-briefs/ask-answers.jsonl",
                    {"id": "43cc0f6c-1111-2222-3333-444444444444", "ts": "2026-11-15T15:20:00+09:00", "how": "send",
                     "sender": {"worktree": str(self.workspaces / "orca" / "triage-lead")}, "note": "最新版は #100 です。"})
        self.append(".cache/orca-session-triage/lead-decisions.jsonl",
                    {"id": "d1", "decision": "remove:local::/x/orca/workspaces/app/old-lane", "answer": "消す",
                     "decided_at": "2026-11-15T06:21:00+00:00", "value": None})
        self.append(".local/share/orca-board/events.jsonl",
                    {"id": "start-1", "ts": "2026-11-15T15:25:00+0900", "type": "start", "source": "spawn-lane",
                     "from": LEAD, "to": "term_new", "worktree": str(self.workspaces / "skills" / "adopt-lead-timeline"),
                     "task": "adopt-lead-timeline", "brief": "/x/adopt-lead-timeline.md"})
        self.append(".local/share/adoption-triage/events.jsonl",
                    {"id": "p1", "ts": "2026-11-15T15:00:00+09:00", "type": "proposal", "proposal_id": "a1",
                     "proposal": "時系列にまとめる CLI", "source": {"worktree": "orca/triage-lead"}},
                    {"id": "e1", "ts": "2026-11-15T15:26:00+09:00", "type": "execution", "proposal_id": "a1",
                     "outcome": "handed_off", "actor": "adopt-lead-timeline"})
        root = self.home / ".cache" / "orca-briefs"
        with sqlite3.connect(root / "holds.sqlite") as db:
            db.execute("""CREATE TABLE holds (id TEXT PRIMARY KEY, handle TEXT NOT NULL, environment TEXT,
                condition_kind TEXT NOT NULL, condition_value TEXT NOT NULL, release_by TEXT NOT NULL,
                may_continue TEXT NOT NULL, instruction TEXT NOT NULL, created_at REAL NOT NULL,
                alert_after REAL NOT NULL, status TEXT NOT NULL, signalled_at REAL, warned_at REAL,
                released_at REAL, sending_at REAL, failure TEXT)""")
            db.execute("INSERT INTO holds VALUES ('h1', ?, NULL, 'signal', ?, '指揮', '', 'まだ書かないでください。',"
                       " 1794719400, 1794720300, 'released', 1794730804, NULL, 1794730834, NULL, NULL)", (OTHER, LEAD))
        (root / "undelivered").mkdir()
        (root / "undelivered" / "u1.json").write_text(json.dumps(
            {"id": "u1", "at": 1794726000, "target": "fix-orders", "handle": OTHER, "environment": None,
             "argv": [], "brief": None, "alerted": False}), encoding="utf-8")
        (root / "undelivered" / "u1.txt").write_text("画面が読めず届いていない本文", encoding="utf-8")

        rows = json.loads(self.run_tool("--since", "2026-11-15T14:00", "--until", "2026-11-15T23:59", "--json").stdout)

        self.assertEqual([(row["kind"], row["lane"], row["to"]) for row in rows], [
            ("保留を置いた", "fix-orders", None),
            ("案が出た", "triage-lead", None),
            ("質問が届いた", "fix-checklist", None),
            ("答えた", "triage-lead", "fix-checklist"),
            ("指揮が決めた", "指揮", "old-lane"),
            ("起動した", "triage-lead", "adopt-lead-timeline"),
            ("案を渡した", "adopt-lead-timeline", None),
            ("未配信", "fix-orders", None),
            ("保留を解いた", "fix-orders", None),
        ])
        asked = next(row["text"] for row in rows if row["kind"] == "質問が届いた")
        self.assertIn("どの版の資料を使いますか？", asked)
        self.assertIn("example-org/app#103", asked)


if __name__ == "__main__":
    unittest.main()
