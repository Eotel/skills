"""Offline behavior tests for hourly triage retrospective capture and decisions."""

import json
import io
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "orca-session-triage" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import retro  # noqa: E402
import stop_check  # noqa: E402


def transcript(path):
    rows = [
        {"type": "user", "timestamp": "2026-10-26T01:00:00Z", "cwd": "/tmp/hourly-triage", "message": {
            "content": "<command-message>orca-session-triage</command-message><command-args>hourly 日本語で</command-args>"}},
        {"type": "assistant", "timestamp": "2026-10-26T01:10:00Z", "message": {
            "content": [{"type": "text", "text": "最初は容量不足で止まった。"}]}},
        {"type": "assistant", "timestamp": "2026-10-26T01:11:00Z", "message": {
            "content": [{"type": "text", "text": "一行を送ると再開し、完了を確認した。"}]}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))


def sample_candidate():
    return {"title": "容量不足から再開", "first_failure": "容量不足で止まった",
            "final_success": "一行を送って再開した", "note": "`Selected model is at capacity` の Codex は一行を送ると再開した。",
            "target_skill": "orca-session-triage", "category": "fact",
            "evidence_ids": ["E00002", "E00003"]}


class FakeLedger:
    def __init__(self):
        self.items = {}

    def list(self):
        return list(self.items.values())

    def propose(self, body):
        self.items[body["proposal_id"]] = {**body, "status": "pending", "decisions": [],
                                          "executions": [], "implemented_at": None,
                                          "reverted_at": None, "adopted_at": None, "revoked_at": None}

    def decide(self, body):
        item = self.items[body["proposal_id"]]
        item["decisions"].append({**body, "ts": retro.now()})
        item["status"] = {"adopt": "adopted", "revoke": "revoked"}[body["decision"]]
        item["adopted_at" if body["decision"] == "adopt" else "revoked_at"] = retro.now()

    def record(self, body):
        item = self.items[body["proposal_id"]]
        item["executions"].append(body)
        if body["outcome"] == "applied":
            item["implemented_at"] = retro.now()
        if body["outcome"] == "reverted":
            item["reverted_at"] = retro.now()

    def add_evidence(self, body):
        self.items[body["proposal_id"]]["evidence"].extend(body["evidence"])


class RetroTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = self.home / "state"
        self.root = self.home / "skills"
        target = self.root / "orca-session-triage" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Triage\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "orca-session-triage/SKILL.md"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null",
                        "commit", "-qm", "test fixture"], check=True)
        self.target = target
        self.sid = "4d4aa0ed-cff3-416c-9e67-3136c059789b"
        self.transcript = self.home / ".claude/projects/triage" / f"{self.sid}.jsonl"
        transcript(self.transcript)

    def test_stop_capture_is_one_line_only_for_hourly_session(self):
        hook = {"session_id": self.sid, "transcript_path": str(self.transcript)}
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            self.assertTrue(retro.enqueue(hook, self.state))
            self.assertFalse(retro.enqueue(hook, self.state))
            self.assertFalse(retro.enqueue({**hook, "session_id": "other"}, self.state))
        lines = retro.paths(self.state)[0].read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], str(self.transcript.resolve()))
        self.assertFalse(retro.paths(self.state)[1].exists())

    def test_existing_stop_checks_finish_before_capture(self):
        hook = {"session_id": self.sid, "transcript_path": str(self.transcript)}
        with mock.patch.object(stop_check.sys, "stdin", io.StringIO(json.dumps(hook))):
            with mock.patch.object(stop_check, "decide", return_value={"decision": "block", "reason": "ask"}):
                with mock.patch.object(retro, "enqueue") as capture:
                    with mock.patch.object(stop_check.sys, "stdout", io.StringIO()):
                        stop_check.main()
                    capture.assert_not_called()
        with mock.patch.object(stop_check.sys, "stdin", io.StringIO(json.dumps(hook))):
            with mock.patch.object(stop_check, "decide", return_value=None):
                with mock.patch.object(retro, "enqueue") as capture:
                    stop_check.main()
                    capture.assert_called_once_with(hook)

    def test_real_transcript_events_have_timestamped_failure_and_success(self):
        events = retro.extract_events(self.transcript)
        self.assertEqual([e["id"] for e in events], ["E00001", "E00002", "E00003"])
        self.assertEqual(events[2]["at"], "2026-10-26T01:11:00Z")
        self.assertIn("完了を確認", events[2]["text"])

    def test_worker_merges_repeated_lesson_and_undo_changes_only_its_line(self):
        second = "5235d194-3941-4db2-82bb-cf2d7277c123"
        second_transcript = self.transcript.with_name(second + ".jsonl")
        transcript(second_transcript)
        second_transcript.write_text(second_transcript.read_text().replace("T01:", "T02:"))
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
            retro.enqueue({"session_id": second, "transcript_path": str(second_transcript)}, self.state)
        answers = [
            {"proposals": [sample_candidate()]},
            {"decisions": [{"index": 0, "supported": True, "duplicate": False,
                            "low_risk": True, "reason": "failure and success shown"}]},
        ] * 2
        ledger = FakeLedger()
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.object(retro, "run_claude", side_effect=answers):
                self.assertIn("1 proposals", retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger))
                self.assertIn("1 proposals", retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger))
        data = retro.load(self.state)
        self.assertEqual(len(ledger.items), 1)
        proposal = ledger.list()[0]
        self.assertEqual(proposal["status"], "adopted")
        self.assertTrue(proposal["implemented_at"])
        self.assertEqual(len(proposal["evidence"]), 4)
        self.assertEqual(self.target.read_text().count(retro.MARKER), 1)
        self.target.write_text(self.target.read_text() + "\nOther author's line\n")
        ledger.decide({"proposal_id": proposal["proposal_id"], "decision": "revoke", "actor": "human"})
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger)
        self.assertIn("Other author's line", self.target.read_text())
        self.assertNotIn(retro.MARKER, self.target.read_text())
        self.assertTrue(ledger.list()[0]["reverted_at"])
        self.assertEqual(data["sessions"][self.sid]["status"], "done")

    def test_policy_change_waits_for_a_button(self):
        proposal = sample_candidate()
        proposal["category"] = "rule"
        proposal["note"] = "PR の head の祖先なら作業場を削除してよい。"
        proposal["review"] = {"supported": True, "duplicate": False, "low_risk": False}
        eligible, reason = retro.eligible_auto(proposal, {"target_versions": {}}, [], self.root)
        self.assertFalse(eligible)
        self.assertEqual(reason, "要確認")

    def test_repository_local_skill_is_an_adoption_candidate(self):
        project_skill = self.home / "ghq/github.com/example/project/.claude/skills/project-triage/SKILL.md"
        project_skill.parent.mkdir(parents=True)
        project_skill.write_text("# Project triage\n")
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            catalog = retro.skill_catalog(self.root)
        self.assertEqual(catalog["example/project/project-triage"], project_skill)

    def test_nightly_cap_keeps_another_supported_fact_for_review(self):
        proposal = sample_candidate()
        proposal["review"] = {"supported": True, "duplicate": False, "low_risk": True}
        today = retro.now()
        prior = [{"decisions": [{"actor": "triage-retro", "decision": "adopt", "ts": today}]}
                 for _ in range(retro.MAX_NIGHTLY_AUTO)]
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            eligible, reason = retro.eligible_auto(proposal, {"target_versions": {}}, prior, self.root)
        self.assertFalse(eligible)
        self.assertEqual(reason, "今夜の自動採用上限")

    def test_nightly_cap_spans_midnight(self):
        evening = datetime.fromisoformat("2026-10-26T23:30:00+09:00")
        morning = datetime.fromisoformat("2026-10-27T06:00:00+09:00")
        next_evening = datetime.fromisoformat("2026-10-27T23:30:00+09:00")
        self.assertEqual(retro.night_key(evening), retro.night_key(morning))
        self.assertNotEqual(retro.night_key(evening), retro.night_key(next_evening))

    def test_unversioned_destination_waits_for_review(self):
        proposal = sample_candidate()
        proposal["review"] = {"supported": True, "duplicate": False, "low_risk": True}
        (self.root / ".git").rename(self.root / "git-fixture-hidden")
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            eligible, reason = retro.eligible_auto(proposal, {"target_versions": {}}, [], self.root)
        self.assertFalse(eligible)
        self.assertIn("Git repository", reason)

    def test_verified_json_shape_fact_can_be_adopted(self):
        proposal = sample_candidate()
        proposal["note"] = "scan_sessions.py の標準出力は JSON 配列。行ごとに読む。"
        proposal["review"] = {"supported": True, "duplicate": False, "low_risk": True}
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            eligible, reason = retro.eligible_auto(proposal, {"target_versions": {}}, [], self.root)
        self.assertTrue(eligible, reason)

    def test_load_gate_defers_without_paid_model_call(self):
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.object(retro.os, "getloadavg", return_value=(35.0, 30.0, 20.0)):
                with mock.patch.object(retro, "build_proposals") as model:
                    self.assertIn("deferred", retro.process_once(self.state, self.root, ledger=FakeLedger()))
                    model.assert_not_called()
        self.assertEqual(retro.load(self.state)["sessions"][self.sid]["status"], "pending")



if __name__ == "__main__":
    unittest.main()
