"""Observable ledger behavior through its command line."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "adoption-triage/scripts/ledger.py"


class AdoptionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = Path(self.temp.name) / "events.jsonl"
        self.imports = Path(self.temp.name) / "imports"

    def run_ledger(self, command, body=None):
        return subprocess.run([sys.executable, str(SCRIPT), "--events", str(self.events),
                               "--imports", str(self.imports), *command],
                              input=json.dumps(body, ensure_ascii=False) if body is not None else None,
                              text=True, capture_output=True)

    def proposal(self):
        result = self.run_ledger(["propose"], {
            "source": {"session": "triage", "worktree": "triage"},
            "fact": "同じ失敗が続いた", "proposal": "設定を直す", "reason": "使い方に関わる",
            "recommendation": "adopt", "priority": "high", "evidence": ["2026-10-26 17:00"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["proposal_id"]

    def test_decisions_are_appended_and_dates_survive_revoke(self):
        proposal_id = self.proposal()
        for decision in ("hold", "adopt", "revoke"):
            result = self.run_ledger(["decide"], {"proposal_id": proposal_id,
                                     "decision": decision, "actor": "Eotel"})
            self.assertEqual(result.returncode, 0, result.stderr)
            if decision == "adopt":
                result = self.run_ledger(["record"], {"proposal_id": proposal_id,
                    "outcome": "applied", "actor": "night worker", "result": "commit abc",
                    "undo": "revert commit abc"})
                self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_ledger(["record"], {"proposal_id": proposal_id,
            "outcome": "reverted", "actor": "night worker", "result": "reverted commit abc"})
        self.assertEqual(result.returncode, 0, result.stderr)
        view = self.run_ledger(["list", "--json"])
        self.assertEqual(view.returncode, 0, view.stderr)
        item = json.loads(view.stdout)["items"][0]
        self.assertEqual(item["status"], "revoked")
        self.assertIsNotNone(item["adopted_at"])
        self.assertIsNotNone(item["revoked_at"])
        self.assertIsNotNone(item["implemented_at"])
        self.assertIsNotNone(item["reverted_at"])
        self.assertEqual(len(item["decisions"]), 3)
        self.assertEqual(len(item["executions"]), 2)
        self.assertEqual(len(self.events.read_text().splitlines()), 6)

    def test_invalid_transition_does_not_append(self):
        proposal_id = self.proposal()
        result = self.run_ledger(["decide"], {"proposal_id": proposal_id,
                                 "decision": "revoke", "actor": "Eotel"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.events.read_text().splitlines()), 1)

    def test_support_adds_evidence_without_creating_another_proposal(self):
        proposal_id = self.proposal()
        result = self.run_ledger(["support"], {"proposal_id": proposal_id,
            "source": {"session": "next triage", "worktree": "triage"},
            "evidence": ["2026-10-26 18:10 に再発"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        view = self.run_ledger(["list", "--json"])
        items = json.loads(view.stdout)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["evidence"], ["2026-10-26 17:00", "2026-10-26 18:10 に再発"])
        self.assertEqual(items[0]["supporting_events"][0]["source"]["session"], "next triage")

    def test_readoption_needs_a_new_execution(self):
        proposal_id = self.proposal()
        for decision in ("adopt", "revoke", "adopt"):
            result = self.run_ledger(["decide"], {"proposal_id": proposal_id,
                                     "decision": decision, "actor": "Eotel"})
            self.assertEqual(result.returncode, 0, result.stderr)
            if decision == "adopt" and len(self.events.read_text().splitlines()) == 2:
                applied = self.run_ledger(["record"], {"proposal_id": proposal_id,
                    "outcome": "applied", "actor": "worker", "result": "commit abc"})
                self.assertEqual(applied.returncode, 0, applied.stderr)
        item = json.loads(self.run_ledger(["list", "--json"]).stdout)["items"][0]
        self.assertEqual(item["status"], "adopted")
        self.assertIsNone(item["implemented_at"])
        self.assertIsNone(item["revoked_at"])

    def test_remote_clock_ahead_keeps_decision_and_execution_in_order(self):
        proposal_id = self.proposal()
        self.imports.mkdir()
        row = json.loads(self.events.read_text().strip())
        row["ts"] = "2026-10-26T23:00:00+09:00"
        (self.imports / "remote.jsonl").write_text(json.dumps(row) + "\n")
        self.events.unlink()
        decision = self.run_ledger(["decide"], {"proposal_id": proposal_id,
            "decision": "adopt", "actor": "Eotel"})
        self.assertEqual(decision.returncode, 0, decision.stderr)
        applied = self.run_ledger(["record"], {"proposal_id": proposal_id,
            "outcome": "applied", "actor": "worker", "result": "commit abc"})
        self.assertEqual(applied.returncode, 0, applied.stderr)
        view = self.run_ledger(["list", "--json"])
        self.assertEqual(view.returncode, 0, view.stderr)
        self.assertEqual(json.loads(view.stdout)["items"][0]["status"], "adopted")
        self.assertEqual(len(json.loads(view.stdout)["items"][0]["executions"]), 1)

    def test_empty_proposal_id_does_not_append(self):
        result = self.run_ledger(["propose"], {
            "proposal_id": "", "source": {"session": "triage", "worktree": "triage"},
            "fact": "失敗", "proposal": "設定を直す", "reason": "繰り返す",
            "recommendation": "adopt", "priority": "high"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.events.exists())

    def test_imports_are_read_without_changing_local_log(self):
        self.imports.mkdir()
        remote = self.imports / "other-mac.jsonl"
        result = self.run_ledger(["propose"], {
            "source": {"session": "remote", "worktree": "triage"},
            "fact": "遠回り", "proposal": "手順を足す", "reason": "影響の確認が必要",
            "recommendation": "adopt", "priority": "normal", "evidence": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        remote.write_bytes(self.events.read_bytes())
        self.events.unlink()
        view = self.run_ledger(["list", "--json"])
        self.assertEqual(len(json.loads(view.stdout)["items"]), 1)
        self.assertFalse(self.events.exists())

    def test_collect_keeps_remote_history_and_rejects_truncation(self):
        proposal_id = self.proposal()
        remote = Path(self.temp.name) / "remote.jsonl"
        rows = [json.loads(line) for line in self.events.read_text().splitlines()]
        rows[0]["host"] = "renamed-os-host"
        remote.write_text(json.dumps(rows[0], ensure_ascii=False) + "\n")
        self.events.unlink()
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        scp = bin_dir / "scp"
        scp.write_text(f"#!{sys.executable}\nimport os, shutil, sys\nshutil.copyfile(os.environ['FAKE_REMOTE'], sys.argv[-1])\n")
        scp.chmod(0o755)
        config = Path(self.temp.name) / "hosts.json"
        config.write_text(json.dumps({"hosts": [{"name": "other-mac", "ssh_target": "remote",
                                                   "remote_path": "/remote/events.jsonl"}]}))
        command = [sys.executable, str(SCRIPT), "--events", str(self.events),
                   "--imports", str(self.imports), "collect", "--config", str(config)]
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
               "FAKE_REMOTE": str(remote)}
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        view = self.run_ledger(["list", "--json"])
        self.assertEqual(json.loads(view.stdout)["items"][0]["proposal_id"], proposal_id)
        remote.write_text("")
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(proposal_id, (self.imports / "other-mac.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
