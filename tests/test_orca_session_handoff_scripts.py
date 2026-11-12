"""Offline behavior tests for the hourly handoff that adds what changed after the scan."""

import importlib.util
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "orca-session-triage" / "scripts" / "handoff.py"
spec = importlib.util.spec_from_file_location("lead_decisions", SCRIPT.with_name("lead_decisions.py"))
assert spec is not None and spec.loader is not None
lead_decisions = importlib.util.module_from_spec(spec)
# dataclasses resolve string annotations through sys.modules.
sys.modules[spec.name] = lead_decisions
spec.loader.exec_module(lead_decisions)


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Application Support"
        self.root.mkdir()
        self.inbox = self.root / "inbox.jsonl"
        self.decisions = self.root / "lead-decisions.jsonl"
        self.handed_off = self.root / "handed-off.jsonl"
        self.projects = self.root / "projects"
        self.sent = self.root / "sent.json"
        # triage-notify is outside this repository; record what it was given.
        self.notify = self.root / "triage-notify"
        self.notify.write_text(f"#!{sys.executable}\nimport json, sys\n"
                               f"open({str(self.sent)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
        self.notify.chmod(0o755)
        self.body = self.root / "body.txt"
        self.body.write_text("定時の引き継ぎ: A を消してよいか\n")
        self.handoffs = self.root / "handoffs"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_tool(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "send", "--inbox", str(self.inbox),
                               "--decisions-file", str(self.decisions), "--handed-off-file", str(self.handed_off),
                               "--claude-projects", str(self.projects), "--handoffs-dir", str(self.handoffs),
                               "--notify", str(self.notify), "--body-file", str(self.body), *args],
                              capture_output=True, text=True,
                              env={**os.environ, "ORCA_WORKTREE_ID": "repo1::/w/triage", "TRIAGE_HOST": "mac-a",
                                   "ORCA_TERMINAL_HANDLE": "term_me", "ORCA_PANE_KEY": "",
                                   "TRIAGE_ASK_ANSWERS": str(self.root / "ask-answers.jsonl"),
                                   "CLAUDE_CONFIG_DIR": str(self.root), "CLAUDE_PID": "86520", **(env or {})})

    def last(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "last", "--handoffs-dir", str(self.handoffs)],
                              capture_output=True, text=True)

    def notify_appending(self, *before: dict[str, str]) -> None:
        """A triage-notify that appends `before`, then the report it was given, as triage-notify records it."""
        self.notify.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            f"lines = [json.dumps(item) for item in {list(before)!r}]\n"
            "lines.append(json.dumps({'id': 'own', 'host': os.environ['TRIAGE_HOST'], 'kind': sys.argv[2],"
            " 'worktree': os.path.basename(os.environ['ORCA_WORKTREE_ID'].partition('::')[2]),"
            " 'text': sys.argv[-1].strip()}))\n"
            f"open({str(self.inbox)!r}, 'a').write(''.join(line + '\\n' for line in lines))\n")

    def test_adds_reports_that_arrived_after_the_scan_and_the_lead_s_current_state(self) -> None:
        seen = json.dumps({"id": "s1", "ts": "t0", "kind": "ask", "worktree": "a", "text": "走査の前の質問"}) + "\n"
        late = [{"id": "l1", "ts": "t1", "kind": "ask", "repo": "app", "worktree": "b", "text": "後の質問\n二行目"},
                {"id": "l2", "ts": "t2", "kind": "info", "repo": "app", "worktree": "c", "text": "経" * 200}]
        self.inbox.write_text(seen + "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in late))
        lead = self.projects / "-w-triage-lead" / "s.jsonl"
        lead.parent.mkdir(parents=True)
        lead.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [
            {"type": "user", "message": {"role": "user", "content": "三浦さんの答え: merge してよい"}},
            {"type": "assistant", "timestamp": "2026-10-27T03:04:00Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "merge を始めました"}]}}]))

        result = self.run_tool("--since", str(len(seen.encode())), "--decision", "remove:/w/a",
                               "--lead-worktree", "/w/triage-lead")

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads(self.sent.read_text())
        self.assertEqual(sent[:2], ["--kind", "ask"])
        body = sent[-1]
        self.assertTrue(body.startswith("定時の引き継ぎ: A を消してよいか"))
        self.assertIn("走査の開始後に届いた報告: 2 件", body)
        self.assertIn("t1 ask app/b id=l1\n後の質問\n二行目", body)
        self.assertIn("t2 info app/c id=l2  " + "経" * 160 + "…", body)
        self.assertNotIn("走査の前の質問", body)
        self.assertIn("未回答の質問 0 件。最後の発言 2026-10-27T03:04:00Z: merge を始めました", body)
        self.assertEqual(result.stdout.strip(), f"NEXT_OFFSET={self.inbox.stat().st_size}")

    def test_refuses_to_hand_off_a_question_the_lead_already_answered(self) -> None:
        self.inbox.write_text("")
        self.decisions.write_text(json.dumps({"id": "x", "decision": "remove:/w/a", "answer": "消してよい",
                                              "decided_at": "2026-10-27T03:04:00+09:00"}, ensure_ascii=False) + "\n")

        result = self.run_tool("--since", "0", "--decision", "remove:/w/a", "--decision", "merge:PR#9")

        self.assertEqual(result.returncode, 1)
        self.assertIn("remove:/w/a", result.stderr)
        self.assertIn("消してよい", result.stderr)
        self.assertFalse(self.sent.exists())
        self.assertFalse(self.handed_off.exists())

    def test_refuses_while_another_tab_of_this_workspace_reported_after_the_scan(self) -> None:
        # 11/10 09:08: the 07:00 tab handed off 4 asks as open after the 02:00 tab of the same workspace
        # handed off (09:05) and corrected them as answered (09:06).
        seen = json.dumps({"id": "s1", "ts": "2026-11-10T09:00:00+09:00", "kind": "info", "worktree": "a",
                           "text": "前"}) + "\n"
        mine = {"id": "m1", "kind": "info", "host": "mac-a", "worktree": "triage", "handle": "term_me",
                "text": "この回の経過"}
        other = {"id": "o1", "ts": "2026-11-10T09:06:46+09:00", "kind": "info", "host": "mac-a",
                 "worktree": "triage", "handle": "term_02", "text": "02:00 の定時のタブから、09:05 の引き継ぎの訂正です"}
        self.inbox.write_text(seen + json.dumps(mine, ensure_ascii=False) + "\n")
        self.assertEqual(self.run_tool("--since", str(len(seen.encode()))).returncode, 0)
        self.sent.unlink()

        with self.inbox.open("a") as stream:
            stream.write(json.dumps(other, ensure_ascii=False) + "\n")
        result = self.run_tool("--since", str(len(seen.encode())))

        self.assertEqual(result.returncode, 1)
        self.assertIn("id=o1", result.stderr)
        self.assertIn("09:05 の引き継ぎの訂正", result.stderr)
        self.assertFalse(self.sent.exists())
        self.assertEqual(self.run_tool("--since", str(len(seen.encode())), "--force").returncode, 0)
        self.assertTrue(self.sent.exists())

    def test_refuses_while_an_ask_the_body_names_was_answered_after_the_scan(self) -> None:
        ask = {"id": "f439c015-aaaa-bbbb-cccc-000000000001", "ts": "2026-11-09T18:00:00+09:00", "kind": "ask",
               "worktree": "lane", "text": "投稿してよいですか"}
        older = {"id": "87fc0252-aaaa-bbbb-cccc-000000000002", "ts": "2026-11-09T18:10:00+09:00", "kind": "ask",
                 "worktree": "lane2", "text": "merge してよいですか"}
        scanned = {"id": "s1", "ts": "2026-11-10T09:00:00+09:00", "kind": "info", "worktree": "a", "text": "走査の前"}
        seen = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in (ask, older, scanned))
        self.inbox.write_text(seen)
        (self.root / "ask-answers.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in [
            {"id": older["id"], "ts": "2026-11-09T18:20:00+09:00", "how": "send", "note": "merge する"}]))
        self.body.write_text("未回答: ask f439c015（投稿）と ask 87fc0252（merge）\n")
        self.assertEqual(self.run_tool("--since", str(len(seen.encode()))).returncode, 0)
        self.sent.unlink()

        with (self.root / "ask-answers.jsonl").open("a") as stream:
            stream.write(json.dumps({"id": ask["id"], "ts": "2026-11-10T09:02:00+09:00", "how": "send",
                                     "note": "三浦さんの答え: 投稿しない"}, ensure_ascii=False) + "\n")
        result = self.run_tool("--since", str(len(seen.encode())))

        self.assertEqual(result.returncode, 1)
        self.assertIn(ask["id"], result.stderr)
        self.assertIn("投稿しない", result.stderr)
        self.assertNotIn(older["id"], result.stderr)
        self.assertFalse(self.sent.exists())

    def test_seen_takes_reflected_reports_and_answers_off_the_reasons_one_by_one(self) -> None:
        # 11/11 10:09: three reports and an answer the body already held kept it from being sent, and --force
        # skipped every check.
        ask = {"id": "f439c015-aaaa-bbbb-cccc-000000000001", "ts": "2026-11-10T08:00:00+09:00", "kind": "ask",
               "worktree": "lane", "text": "投稿してよいですか"}
        scanned = {"id": "s1", "ts": "2026-11-10T09:00:00+09:00", "kind": "info", "worktree": "a", "text": "走査の前"}
        other = {"id": "o1", "ts": "2026-11-10T09:06:46+09:00", "kind": "info", "host": "mac-a",
                 "worktree": "triage", "handle": "term_02", "text": "02:00 の定時のタブからの訂正"}
        seen = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in (ask, scanned))
        self.inbox.write_text(seen + json.dumps(other, ensure_ascii=False) + "\n")
        (self.root / "ask-answers.jsonl").write_text(json.dumps(
            {"id": ask["id"], "ts": "2026-11-10T09:02:00+09:00", "how": "send", "note": "投稿しない"},
            ensure_ascii=False) + "\n")
        self.body.write_text("ask f439c015 は投稿しないと答えがあった。02:00 のタブの訂正も反映した\n")
        since = str(len(seen.encode()))

        one = self.run_tool("--since", since, "--seen", "o1")
        both = self.run_tool("--since", since, "--seen", "o1", "--seen", "f439c015")

        self.assertEqual(one.returncode, 1)
        self.assertNotIn("id=o1", one.stderr)
        self.assertIn(f"answered {ask['id']}", one.stderr)
        self.assertEqual(both.returncode, 0, both.stderr)
        self.assertTrue(self.sent.exists())

    def test_refuses_while_a_later_scheduled_run_began_or_was_skipped_for_this_one(self) -> None:
        # 11/12: the 10:00 run worked past noon; the 11:00 run was skipped for it and the 12:00 run began unseen.
        self.inbox.write_text("")
        orca = self.root / "orca"
        hour, ten = 3_600_000, int(datetime(2026, 11, 12, 10, 0).timestamp() * 1000)
        runs = [{"status": "dispatched", "scheduledFor": ten + hour, "startedAt": ten + hour + 480_000,
                 "terminalPaneKey": "pane-11"},
                {"status": "dispatched", "scheduledFor": ten, "startedAt": ten + 422_000, "terminalPaneKey": "pane-10"}]
        orca.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            "automations = [{'id': 'a1', 'prompt': '/orca-session-triage hourly', 'workspaceId': 'repo1::/w/triage'}]\n"
            f"runs = {[{**run, 'trigger': 'scheduled', 'workspaceId': 'repo1::/w/triage'} for run in runs]!r}\n"
            "result = {'automations': automations} if sys.argv[1:3] == ['automations', 'list'] else {'runs': runs}\n"
            "print(json.dumps({'result': result}))\n")
        orca.chmod(0o755)
        env = {"ORCA_CLI_COMMAND": str(orca), "ORCA_PANE_KEY": "pane-10"}

        refused = self.run_tool("--since", "0", env=env)
        self.assertFalse(self.sent.exists())
        forced = self.run_tool("--since", "0", "--force", env=env)

        self.assertEqual(refused.returncode, 1)
        self.assertIn("11/12 11:00 の定時の回が始まった（dispatched、11/12 11:08 開始、pane pane-11）", refused.stderr)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertTrue(self.sent.exists())

    def test_search_prints_the_matching_lines_and_keys_of_each_saved_handoff_since_a_time(self) -> None:
        # 11/11 20:14: grep of the saved JSON hit ugrep's limits, and a script read the body under the wrong key.
        self.handoffs.mkdir()
        for name, sent_at, text, keys in (
                ("20261111T060000.000000Z", "2026-11-11T06:00:00+00:00", "muse を消す\n別の行", ["remove:muse"]),
                ("20261111T110000.000000Z", "2026-11-11T11:00:00+00:00", "持ち越し: muse の PR\n関係ない行",
                 ["spawn:plinth::x", "merge:other"]),
                ("20261111T120000.000000Z", "2026-11-11T12:00:00+00:00", "当たらない本文", [])):
            (self.handoffs / f"{name}.json").write_text(json.dumps(
                {"text": text, "kind": "ask", "decisions": keys, "sent_at": sent_at, "next_offset": 0},
                ensure_ascii=False))

        result = subprocess.run([sys.executable, str(SCRIPT), "search", "MUSE", "plinth", "--since",
                                 "2026-11-11T10:00:00+00:00", "--handoffs-dir", str(self.handoffs)],
                                capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"2026-11-11T11:00:00+00:00 ask {self.handoffs / '20261111T110000.000000Z.json'}",
            "  持ち越し: muse の PR", "  決定キー: spawn:plinth::x"])

    def test_send_signs_the_end_with_the_session_name_and_start_time_in_place_of_the_body_signature(self) -> None:
        self.inbox.write_text("")
        (self.root / "sessions").mkdir()
        started = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
        (self.root / "sessions/86520.json").write_text(json.dumps({
            "name": "triage-hourly-0f", "startedAt": int(started.timestamp() * 1000)}))
        self.body.write_text("引き継ぎ本文\n\ntriage-hourly（03:00 の定時）\n")

        result = self.run_tool("--since", "0", env={"TZ": "Asia/Tokyo"})

        self.assertEqual(result.returncode, 0, result.stderr)
        sent = json.loads(self.sent.read_text())[-1]
        self.assertTrue(sent.endswith("\n\ntriage-hourly-0f（03:00 の定時）"), sent)
        self.assertNotIn("triage-hourly（", sent)

    def test_without_the_session_record_the_body_goes_as_written(self) -> None:
        self.inbox.write_text("")
        self.body.write_text("引き継ぎ本文\n\ntriage-hourly（03:00 の定時）\n")

        result = self.run_tool("--since", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("triage-hourly（03:00 の定時）\n\n——", json.loads(self.sent.read_text())[-1])
        self.assertIn("without the run's signature", result.stderr)

    def test_records_each_key_it_hands_off_for_tools_that_must_wait_for_the_answer(self) -> None:
        self.inbox.write_text("")

        result = self.run_tool("--since", "0", "--decision", "asana:complete:example/app#12",
                               "--decision", "merge:Eotel/skills#80")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item.decision for item in lead_decisions.handed_off(self.handed_off)],
                         ["asana:complete:example/app#12", "merge:Eotel/skills#80"])


    def test_keeps_each_handoff_it_sent_and_last_prints_the_newest(self) -> None:
        self.inbox.write_text("")
        self.assertEqual(self.last().returncode, 1)

        self.assertEqual(self.run_tool("--since", "0", "--decision", "merge:Eotel/skills#80").returncode, 0)
        first = json.loads(self.sent.read_text())[-1]
        self.body.write_text("定時の引き継ぎ: B を merge してよいか\n")
        result = self.run_tool("--since", "0", "--decision", "merge:Eotel/orca-board#12+#14",
                               "--decision", "remove:mac::/w/b", "--kind", "info")
        self.assertEqual(result.returncode, 0, result.stderr)
        second = json.loads(self.sent.read_text())[-1]

        saved = sorted(json.loads(path.read_text(encoding="utf-8"))["text"] for path in self.handoffs.glob("*.json"))
        self.assertEqual(saved, sorted([first, second]))
        shown = self.last()
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn(second, shown.stdout)
        self.assertNotIn("A を消してよいか", shown.stdout)
        self.assertIn("merge:Eotel/orca-board#12+#14", shown.stdout)
        self.assertIn("remove:mac::/w/b", shown.stdout)
        self.assertIn("info", shown.stdout)

    def test_next_offset_passes_the_run_s_own_handoff(self) -> None:
        self.inbox.write_text(json.dumps({"id": "s1", "kind": "info", "worktree": "a", "text": "前"}) + "\n")
        self.notify_appending()

        result = self.run_tool("--since", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.inbox.read_text().splitlines()[-1].count('"own"'), 1)
        self.assertEqual(result.stdout.strip(), f"NEXT_OFFSET={self.inbox.stat().st_size}")

    def test_next_offset_stays_before_a_report_that_arrived_while_sending(self) -> None:
        seen = json.dumps({"id": "s1", "kind": "info", "worktree": "a", "text": "前"}) + "\n"
        self.inbox.write_text(seen)
        self.notify_appending({"id": "l1", "host": "mac-a", "kind": "ask", "worktree": "lane", "text": "送信中の質問"})

        result = self.run_tool("--since", "0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"NEXT_OFFSET={len(seen.encode())}")


if __name__ == "__main__":
    unittest.main()
