"""Offline behavior tests for the hourly handoff that adds what changed after the scan."""

import importlib.util
import json
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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "send", "--inbox", str(self.inbox),
                               "--decisions-file", str(self.decisions), "--handed-off-file", str(self.handed_off),
                               "--claude-projects", str(self.projects),
                               "--notify", str(self.notify), "--body-file", str(self.body), *args],
                              capture_output=True, text=True)

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

    def test_records_each_key_it_hands_off_for_tools_that_must_wait_for_the_answer(self) -> None:
        self.inbox.write_text("")

        result = self.run_tool("--since", "0", "--decision", "asana:complete:example/app#12",
                               "--decision", "merge:Eotel/skills#80")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item.decision for item in lead_decisions.handed_off(self.handed_off)],
                         ["asana:complete:example/app#12", "merge:Eotel/skills#80"])


if __name__ == "__main__":
    unittest.main()
