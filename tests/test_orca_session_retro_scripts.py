"""Offline behavior tests for hourly triage retrospective capture and decisions."""

from __future__ import annotations

import json
import io
import plistlib
from contextlib import redirect_stdout
from datetime import datetime
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
from typing import Any
import unittest
import uuid
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "orca-session-triage" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import retro  # noqa: E402
import retro_service  # noqa: E402
import stop_check  # noqa: E402


def transcript(path: Path, cwd: str = "/tmp/hourly-triage") -> None:
    rows = [
        {"type": "user", "timestamp": "2026-10-26T01:00:00Z", "cwd": cwd, "message": {
            "content": "<command-message>orca-session-triage</command-message><command-args>hourly 日本語で</command-args>"}},
        {"type": "assistant", "timestamp": "2026-10-26T01:10:00Z", "message": {
            "model": "claude-opus-5-5", "content": [{"type": "text", "text": "最初は容量不足で止まった。"}]}},
        {"type": "assistant", "timestamp": "2026-10-26T01:11:00Z", "message": {
            "model": "claude-opus-5-5", "content": [{"type": "text", "text": "一行を送ると再開し、完了を確認した。"}]}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))


def sample_candidate() -> dict[str, object]:
    return {"title": "容量不足から再開", "first_failure": "容量不足で止まった",
            "final_success": "一行を送って再開した", "note": "`Selected model is at capacity` の Codex は一行を送ると再開した。",
            "target_skill": "orca-session-triage", "category": "fact",
            "evidence_quotes": ["最初は容量不足で止まった", "一行を送ると再開し、完了を確認した"]}


class FakeLedger:
    def __init__(self):
        self.items = {}

    def list(self):
        return list(self.items.values())

    def writer_enabled(self):
        return True

    def propose(self, body):
        self.items[body["proposal_id"]] = {**body, "status": "pending", "decisions": [],
                                          "executions": [], "implemented_at": None,
                                          "reverted_at": None, "adopted_at": None, "revoked_at": None}

    def decide(self, body):
        item = self.items[body["proposal_id"]]
        item["decisions"].append({**body, "id": str(uuid.uuid4()), "ts": retro.now()})
        item["status"] = {"adopt": "adopted", "reject": "rejected", "hold": "held",
                          "revoke": "revoked"}[body["decision"]]
        item[item["status"] + "_at"] = retro.now()

    def record(self, body):
        item = self.items[body["proposal_id"]]
        decision = next((row for row in item["decisions"] if row["id"] == body["decision_id"]), None)
        if decision is None:
            raise ValueError("execution needs its decision ID")
        if body["outcome"] == "applied" and decision["decision"] != "adopt":
            raise ValueError("application needs adoption")
        if body["outcome"] == "reverted" and decision["decision"] != "revoke":
            raise ValueError("reversal needs revocation")
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

    def test_claude_uses_selected_orca_profile_without_api_credentials(self) -> None:
        selected = self.home / "Library/Application Support/orca/claude-profiles/selected-host"
        selected.parent.mkdir(parents=True)
        selected.write_text(str(self.home / "orca-profile/a"))
        success = subprocess.CompletedProcess(
            ["claude", "-p"], 0,
            stdout=json.dumps({"structured_output": {"ok": True}}), stderr="")
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "paid-key",
                                              "ANTHROPIC_AUTH_TOKEN": "paid-token"}, clear=True):
                with mock.patch.object(retro.subprocess, "run", return_value=success) as run:
                    self.assertEqual(retro.run_claude("prompt", {"type": "object"}), {"ok": True})
                    env = run.call_args.kwargs["env"]
                    self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.home / "orca-profile/a"))
                    self.assertEqual(env["USER"], pwd.getpwuid(os.getuid()).pw_name)
                    self.assertNotIn("ANTHROPIC_API_KEY", env)
                    self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

                    selected.write_text(str(self.home / "orca-profile/b"))
                    retro.run_claude("prompt", {"type": "object"}, resume=retro.ForkTarget(
                        self.sid, self.home, "sonnet", 0))
                    self.assertEqual(run.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"],
                                     str(self.home / "orca-profile/b"))

                    selected.unlink()
                    retro.run_claude("prompt", {"type": "object"})
                    self.assertNotIn("CLAUDE_CONFIG_DIR", run.call_args.kwargs["env"])

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
        self.assertEqual(proposal["executions"][0]["decision_id"], proposal["decisions"][0]["id"])
        self.assertEqual(len(proposal["evidence"]), 4)
        facts = retro.facts_path(self.target)
        self.assertEqual(facts.read_text().count(retro.MARKER), 1)
        self.assertIn(retro.ROUTER_SECTION, self.target.read_text())
        self.target.write_text(self.target.read_text() + "\nOther author's line\n")
        ledger.decide({"proposal_id": proposal["proposal_id"], "decision": "revoke", "actor": "human"})
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger)
        self.assertIn("Other author's line", self.target.read_text())
        self.assertNotIn(retro.ROUTER_SECTION, self.target.read_text())
        self.assertFalse(facts.exists())
        self.assertTrue(ledger.list()[0]["reverted_at"])
        self.assertEqual(ledger.list()[0]["executions"][-1]["decision_id"],
                         ledger.list()[0]["decisions"][-1]["id"])
        self.assertEqual(data["sessions"][self.sid]["status"], "done")

    def test_real_adoption_ledger_records_support_application_and_reversal(self):
        config = self.home / "hosts.json"
        config.write_text(json.dumps({"name": "board", "decision_writer": True, "hosts": []}))
        ledger = retro.LedgerClient(
            script=ROOT / "adoption-triage/scripts/ledger.py",
            events=self.home / "events.jsonl", imports=self.home / "imports",
            decision_config=config)
        proposal_id = str(uuid.uuid4())
        ledger.propose({"proposal_id": proposal_id,
                        "source": {"session": self.sid, "worktree": str(self.root)},
                        "fact": "Tool failed, then succeeded.",
                        "proposal": "Tool.py returns JSON.",
                        "reason": "The failure and success were observed.",
                        "recommendation": "adopt", "priority": "normal",
                        "evidence": ["first failure", "later success"],
                        "target": "triage-retro:skill:orca-session-triage:fact"})
        ledger.add_evidence({"proposal_id": proposal_id,
                             "source": {"session": str(uuid.uuid4()), "worktree": str(self.root)},
                             "evidence": ["another run succeeded"]})
        ledger.decide({"proposal_id": proposal_id, "decision": "adopt", "actor": "triage-retro"})
        data = retro.load(self.state)
        self.assertEqual(retro.sync_decisions(ledger, data, self.root, self.state), 1)
        adopted = ledger.list()[0]
        self.assertEqual(len(adopted["evidence"]), 3)
        self.assertTrue(adopted["implemented_at"])
        self.assertEqual(adopted["executions"][-1]["decision_id"], adopted["decisions"][-1]["id"])
        self.assertIn(proposal_id, retro.facts_path(self.target).read_text())

        ledger.decide({"proposal_id": proposal_id, "decision": "revoke", "actor": "human"})
        self.assertEqual(retro.sync_decisions(ledger, data, self.root, self.state), 1)
        revoked = ledger.list()[0]
        self.assertTrue(revoked["reverted_at"])
        self.assertEqual(revoked["executions"][-1]["decision_id"], revoked["decisions"][-1]["id"])
        self.assertFalse(retro.facts_path(self.target).exists())
        self.assertNotIn(retro.ROUTER_SECTION, self.target.read_text())

    def test_policy_change_waits_for_a_button(self):
        proposal = sample_candidate()
        proposal["category"] = "rule"
        proposal["note"] = "PR の head の祖先なら作業場を削除してよい。"
        proposal["review"] = {"supported": True, "duplicate": False, "low_risk": False}
        eligible, reason = retro.eligible_auto(proposal, {"target_versions": {}}, [], self.root)
        self.assertFalse(eligible)
        self.assertEqual(reason, "要確認")

    def test_multiple_adoptions_share_one_reference_and_restore_router(self):
        ledger = FakeLedger()
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for index, proposal_id in enumerate(ids):
            ledger.propose({"proposal_id": proposal_id, "proposal": f"Tool {index}.py returns JSON.",
                            "target": "triage-retro:skill:orca-session-triage:fact"})
            ledger.decide({"proposal_id": proposal_id, "decision": "adopt", "actor": "triage-retro"})
        data = retro.load(self.state)
        self.assertEqual(retro.sync_decisions(ledger, data, self.root), 3)
        facts = retro.facts_path(self.target)
        self.assertEqual(facts.read_text().count(retro.MARKER), 3)
        self.assertEqual(self.target.read_text().count(retro.ROUTER_SECTION), 1)
        for proposal_id in ids:
            ledger.decide({"proposal_id": proposal_id, "decision": "revoke", "actor": "human"})
        self.assertEqual(retro.sync_decisions(ledger, data, self.root), 3)
        self.assertFalse(facts.exists())
        self.assertNotIn(retro.ROUTER_SECTION, self.target.read_text())

    def test_adoption_receipt_timeout_recovers_without_a_second_write(self):
        ledger = FakeLedger()
        proposal_id = str(uuid.uuid4())
        ledger.propose({"proposal_id": proposal_id, "proposal": "Tool.py returns JSON.",
                        "target": "triage-retro:skill:orca-session-triage:fact"})
        ledger.decide({"proposal_id": proposal_id, "decision": "adopt", "actor": "triage-retro"})
        original_record = ledger.record
        ledger.record = mock.Mock(side_effect=subprocess.TimeoutExpired("ledger record", 30))
        with self.assertRaises(subprocess.TimeoutExpired):
            retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger)
        self.assertIn(proposal_id, retro.load(self.state)["applied_actions"])
        facts = retro.facts_path(self.target)
        self.assertEqual(facts.read_text().count(retro.MARKER), 1)
        ledger.record = original_record
        retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger)
        self.assertTrue(ledger.items[proposal_id]["implemented_at"])
        self.assertEqual(facts.read_text().count(retro.MARKER), 1)
        self.assertNotIn(proposal_id, retro.load(self.state)["applied_actions"])

    def test_revocation_removes_a_note_without_an_applied_receipt(self):
        ledger = FakeLedger()
        proposal_id = str(uuid.uuid4())
        ledger.propose({"proposal_id": proposal_id, "proposal": "Tool.py returns JSON.",
                        "target": "triage-retro:skill:orca-session-triage:fact"})
        ledger.decide({"proposal_id": proposal_id, "decision": "adopt", "actor": "triage-retro"})
        original_record = ledger.record
        ledger.record = mock.Mock(side_effect=subprocess.TimeoutExpired("ledger record", 30))
        with self.assertRaises(subprocess.TimeoutExpired):
            retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger)
        ledger.record = original_record
        data = retro.load(self.state)
        data["applied_actions"].clear()  # Simulate losing the receipt checkpoint after the file write.
        data["target_versions"].clear()
        retro.save(data, self.state)
        ledger.decide({"proposal_id": proposal_id, "decision": "revoke", "actor": "human"})
        retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger)
        self.assertTrue(ledger.items[proposal_id]["reverted_at"])
        self.assertFalse(retro.facts_path(self.target).exists())
        self.assertNotIn(retro.ROUTER_SECTION, self.target.read_text())

    def test_batch_stops_at_per_skill_note_limit(self):
        data = retro.load(self.state)
        for index in range(2):
            retro.adopt({"id": str(uuid.uuid4()), "note": f"Existing tool {index}.py works.",
                         "target_skill": "orca-session-triage"}, self.root)
        retro.remember_versions(data, self.target, retro.facts_path(self.target))
        retro.save(data, self.state)
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
        evidence = retro.extract_events(self.transcript)[1:3]
        candidates = [{"note": f"New tool {index}.py returns JSON.", "target_skill": "orca-session-triage",
                       "category": "fact", "first_failure": "read failed", "final_success": "read succeeded",
                       "review": {"supported": True, "duplicate": False, "low_risk": True},
                       "evidence": evidence} for index in range(3)]
        ledger = FakeLedger()
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.object(retro, "build_proposals", return_value=candidates):
                self.assertIn("3 proposals", retro.process_once(self.state, self.root,
                                                                  ignore_load=True, ledger=ledger))
        self.assertEqual([item["status"] for item in ledger.list()].count("adopted"), 1)
        self.assertEqual([item["status"] for item in ledger.list()].count("pending"), 2)
        self.assertEqual(retro.facts_path(self.target).read_text().count(retro.MARKER), 3)

    def test_reference_content_is_available_to_overlap_review(self):
        reference = self.target.parent / "references" / "hourly.md"
        reference.parent.mkdir(parents=True)
        note = "scan_sessions.py の scan.json は行の list"
        reference.write_text(f"# Hourly\n\n{note}。\n")
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            result = retro.nearby_knowledge(note, self.root, "orca-session-triage")
        self.assertIn(str(reference), result)
        self.assertIn(note, result)

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

    def test_deleted_reference_is_not_recreated_automatically(self):
        facts = retro.facts_path(self.target)
        facts.parent.mkdir(parents=True)
        facts.write_text(retro.FACTS_HEADER)
        subprocess.run(["git", "-C", str(self.root), "add", str(facts.relative_to(self.root))], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null",
                        "commit", "-qm", "tracked reference"], check=True)
        facts.unlink()
        proposal = sample_candidate()
        proposal["review"] = {"supported": True, "duplicate": False, "low_risk": True}
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            eligible, reason = retro.eligible_auto(proposal, {"target_versions": {}}, [], self.root)
        self.assertFalse(eligible)
        self.assertEqual(reason, "書き込み先に別の変更がある")

    def test_worker_deleted_tracked_reference_allows_a_later_adoption(self):
        ledger = FakeLedger()
        proposal_id = str(uuid.uuid4())
        ledger.propose({"proposal_id": proposal_id, "proposal": "First_tool.py returns JSON.",
                        "target": "triage-retro:skill:orca-session-triage:fact"})
        ledger.decide({"proposal_id": proposal_id, "decision": "adopt", "actor": "triage-retro"})
        data = retro.load(self.state)
        self.assertEqual(retro.sync_decisions(ledger, data, self.root), 1)
        facts = retro.facts_path(self.target)
        subprocess.run(["git", "-C", str(self.root), "add", "orca-session-triage/SKILL.md",
                        str(facts.relative_to(self.root))], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null",
                        "commit", "-qm", "tracked adoption"], check=True)
        ledger.decide({"proposal_id": proposal_id, "decision": "revoke", "actor": "human"})
        self.assertEqual(retro.sync_decisions(ledger, data, self.root), 1)
        self.assertFalse(facts.exists())
        self.assertIn(str(facts.resolve()), data["target_versions"])
        self.assertIsNone(data["target_versions"][str(facts.resolve())])
        proposal = sample_candidate()
        proposal["note"] = "Second_tool.py returns JSON."
        proposal["review"] = {"supported": True, "duplicate": False, "low_risk": True}
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            eligible, reason = retro.eligible_auto(proposal, data, ledger.list(), self.root)
        self.assertTrue(eligible, reason)

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

    def test_disabled_decision_writer_defers_before_model_call(self):
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
        ledger = FakeLedger()
        ledger.writer_enabled = lambda: False
        with mock.patch.object(retro, "build_proposals") as model:
            self.assertIn("deferred: adoption ledger", retro.process_once(
                self.state, self.root, ignore_load=True, ledger=ledger))
            model.assert_not_called()

    def test_decision_writer_needs_a_stable_name(self):
        config = self.home / "hosts.json"
        ledger = retro.LedgerClient(decision_config=config)
        config.write_text(json.dumps({"decision_writer": True}))
        self.assertFalse(ledger.writer_enabled())
        config.write_text(json.dumps({"decision_writer": True, "name": "board"}))
        self.assertTrue(ledger.writer_enabled())

    def test_failed_session_is_visible_and_can_be_retried(self):
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
            data = retro.load(self.state)
            retro.ingest(data, self.state)
            data["sessions"][self.sid].update({"status": "failed", "attempts": 3,
                                                 "error": "claude was unavailable"})
            retro.save(data, self.state)
            output = io.StringIO()
            with mock.patch.object(retro, "LedgerClient", return_value=FakeLedger()):
                with redirect_stdout(output):
                    self.assertEqual(retro.main(["status", "--state", str(self.state)]), 0)
                self.assertEqual(json.loads(output.getvalue())["failed_session_ids"], [self.sid])
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(retro.main(["retry", "--state", str(self.state),
                                                 "--session-id", self.sid]), 0)
        session = retro.load(self.state)["sessions"][self.sid]
        self.assertEqual((session["status"], session["attempts"]), ("pending", 0))
        self.assertNotIn("error", session)


    def test_finished_session_is_reviewed_in_a_forked_copy(self) -> None:
        transcript(self.transcript, cwd=str(self.home))
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
        calls = []
        real_run = subprocess.run

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[0] != "claude":
                return real_run(cmd, *args, **kwargs)
            calls.append((cmd, kwargs.get("cwd")))
            output = ({"proposals": [sample_candidate()]} if "--resume" in cmd else
                      {"decisions": [{"index": 0, "supported": True, "duplicate": False,
                                      "low_risk": False, "reason": "shown"}]})
            return subprocess.CompletedProcess(cmd, 0, json.dumps(
                {"is_error": False, "structured_output": output}), "")
        ledger = FakeLedger()
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.object(retro.subprocess, "run", side_effect=fake_run):
                self.assertIn("1 proposals", retro.process_once(
                    self.state, self.root, ignore_load=True, ledger=ledger))
        fork, cwd = calls[0]
        self.assertEqual(fork[fork.index("--resume") + 1], self.sid)
        self.assertIn("--fork-session", fork)
        self.assertIn("--no-session-persistence", fork)
        self.assertEqual(cwd, self.home)
        self.assertNotIn("--resume", calls[1][0])
        evidence = ledger.list()[0]["evidence"]
        self.assertEqual([line.split()[2] for line in evidence], ["E00002", "E00003"])

    def test_claude_failure_keeps_the_json_reason(self) -> None:
        failed = subprocess.CompletedProcess(["claude"], 1, json.dumps(
            {"is_error": True, "subtype": "success",
             "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}), "")
        with mock.patch.object(retro.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as raised:
                retro.run_claude("prompt", retro.REVIEW_SCHEMA)
        self.assertIn("is_error=True", str(raised.exception))
        self.assertIn("OAuth session expired", str(raised.exception))

    def test_repeated_failure_notifies_once(self) -> None:
        log = self.home / "notify.log"
        notifier = self.home / "triage-notify"
        notifier.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
        notifier.chmod(0o755)
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
        error = RuntimeError("claude -p exited 1: is_error=True result=Failed to authenticate")
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.object(retro, "NOTIFY", notifier):
                with mock.patch.object(retro, "build_proposals", side_effect=error):
                    for _ in range(3):
                        retro.process_once(self.state, self.root, ignore_load=True, ledger=FakeLedger())
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertIn("Failed to authenticate", log.read_text())
        self.assertIn("Failed to authenticate", retro.load(self.state)["sessions"][self.sid]["error"])

    def test_live_tab_submission_is_recorded_once(self) -> None:
        review = {"decisions": [{"index": 0, "supported": True, "duplicate": False,
                                 "low_risk": False, "reason": "shown"}]}
        ledger = FakeLedger()
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.object(retro, "run_claude", return_value=review):
                self.assertIn("1 proposals", retro.submit(
                    {"proposals": [sample_candidate()]}, self.sid, self.state, self.root, ledger=ledger))
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
            with mock.patch.object(retro, "build_proposals") as fork:
                self.assertIn("empty", retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger))
                fork.assert_not_called()
        self.assertEqual(len(ledger.items), 1)

    def test_adoption_comment_rewrites_the_note_before_it_is_written(self) -> None:
        ledger = FakeLedger()
        proposal_id = str(uuid.uuid4())
        ledger.propose({"proposal_id": proposal_id, "proposal": "Tool.py returns JSON.",
                        "fact": "Tool.py failed, then returned JSON.",
                        "target": "triage-retro:skill:orca-session-triage:fact"})
        ledger.decide({"proposal_id": proposal_id, "decision": "adopt", "actor": "Eotel",
                       "reason": "コマンドの形で書いて"})
        rewritten = "`python3 tool.py --json` は JSON を返す。"
        data = retro.load(self.state)
        with mock.patch.object(retro, "run_claude", return_value={"note": rewritten}) as claude:
            self.assertEqual(retro.sync_decisions(ledger, data, self.root, self.state), 1)
        self.assertIn("コマンドの形で書いて", claude.call_args.args[0])
        facts = retro.facts_path(self.target)
        self.assertIn(rewritten, facts.read_text())
        self.assertNotIn("Tool.py returns JSON.", facts.read_text())
        self.assertIn(rewritten, ledger.items[proposal_id]["executions"][-1]["result"])
        ledger.decide({"proposal_id": proposal_id, "decision": "revoke", "actor": "Eotel"})
        self.assertEqual(retro.sync_decisions(ledger, data, self.root, self.state), 1)
        self.assertFalse(facts.exists())

    def test_rejection_comment_reaches_the_next_retrospective(self) -> None:
        ledger = FakeLedger()
        proposal_id = str(uuid.uuid4())
        ledger.propose({"proposal_id": proposal_id, "proposal": "作業場は毎回作り直す。",
                        "target": "triage-retro:skill:orca-session-triage:procedure"})
        ledger.decide({"proposal_id": proposal_id, "decision": "reject", "actor": "Eotel",
                       "reason": "作り直すと担当の履歴が消える"})
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            retro.enqueue({"session_id": self.sid, "transcript_path": str(self.transcript)}, self.state)
        prompts = []

        def fork(prompt: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            prompts.append(prompt)
            return {"proposals": []}
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.object(retro, "fork_target", return_value=retro.ForkTarget(
                    self.sid, self.home, "claude-opus-5-5", 0.0)):
                with mock.patch.object(retro, "run_claude", side_effect=fork):
                    retro.process_once(self.state, self.root, ignore_load=True, ledger=ledger)
        self.assertIn("作業場は毎回作り直す。", prompts[0])
        self.assertIn("作り直すと担当の履歴が消える", prompts[0])

    def test_deployed_skill_copy_is_written_at_its_apm_source(self) -> None:
        checkouts = self.home / "ghq/github.com"
        source = checkouts / "team/skills/skills/shared/SKILL.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Shared\n")
        copy = checkouts / "example/app/.claude/skills/shared/SKILL.md"
        copy.parent.mkdir(parents=True)
        copy.write_text("# Shared\n")
        lock = ("dependencies:\n- repo_url: team/skills\n  host: github.com\n"
                "  virtual_path: skills/shared\n  deployed_files:\n  - {path}\n  - {path}/SKILL.md\n"
                "deployments:\n- kind: project-relative\n  value: {path}\n")
        (copy.parents[3] / "apm.lock.yaml").write_text(lock.format(path=".claude/skills/shared"))
        (self.home / ".apm").mkdir()
        (self.home / ".apm/apm.lock.yaml").write_text(lock.format(path=".agents/skills/shared"))
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            self.assertEqual(retro.skill_target("example/app/shared", self.root), source)
            self.assertEqual(retro.skill_target("team/skills/shared", self.root), source)

    def test_adoption_the_worker_cannot_write_notifies_once(self) -> None:
        log = self.home / "notify.log"
        notifier = self.home / "triage-notify"
        notifier.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
        notifier.chmod(0o755)
        ledger = FakeLedger()
        proposal_id = str(uuid.uuid4())
        ledger.propose({"proposal_id": proposal_id, "proposal": "Tool.py returns JSON.",
                        "target": "triage-retro:skill:example/app/missing:fact"})
        ledger.decide({"proposal_id": proposal_id, "decision": "adopt", "actor": "Eotel"})
        data = retro.load(self.state)
        with mock.patch.object(retro.Path, "home", return_value=self.home):
            with mock.patch.object(retro, "NOTIFY", notifier):
                for _ in range(4):
                    retro.sync_decisions(ledger, data, self.root, self.state)
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertIn(proposal_id, log.read_text())
        self.assertIn("example/app/missing", log.read_text())
        self.assertIn("target skill is missing", log.read_text())

    def test_service_does_not_embed_the_claude_profile(self) -> None:
        with mock.patch.dict(retro_service.os.environ, {"CLAUDE_CONFIG_DIR": "/profiles/a/home"}):
            plist = plistlib.loads(retro_service.plist_bytes(
                self.home, "/usr/bin/python3", "/usr/local/bin/claude", SCRIPTS / "retro.py"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", plist["EnvironmentVariables"])



if __name__ == "__main__":
    unittest.main()
