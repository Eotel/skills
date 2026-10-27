"""Observable ledger behavior through its command line."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "adoption-triage/scripts/ledger.py"


class AdoptionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = Path(self.temp.name) / "events.jsonl"
        self.imports = Path(self.temp.name) / "imports"
        self.writer_config = Path(self.temp.name) / "writer.json"
        self.writer_config.write_text('{"decision_writer": true, "name": "board"}')

    def run_ledger(self, command, body=None):
        return subprocess.run([sys.executable, str(SCRIPT), "--events", str(self.events),
                               "--imports", str(self.imports), "--decision-config", str(self.writer_config),
                               *command],
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
                adopted_id = json.loads(result.stdout)["id"]
                result = self.run_ledger(["record"], {"proposal_id": proposal_id,
                    "decision_id": adopted_id,
                    "outcome": "applied", "actor": "night worker", "result": "commit abc",
                    "undo": "revert commit abc"})
                self.assertEqual(result.returncode, 0, result.stderr)
            if decision == "revoke":
                revoked_id = json.loads(result.stdout)["id"]
        result = self.run_ledger(["record"], {"proposal_id": proposal_id,
            "decision_id": revoked_id,
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

    def test_only_configured_writer_can_decide(self):
        proposal_id = self.proposal()
        self.writer_config.write_text('{"decision_writer": false}')
        result = self.run_ledger(["decide"], {"proposal_id": proposal_id,
            "decision": "adopt", "actor": "other Mac"})
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
                    "decision_id": json.loads(result.stdout)["id"],
                    "outcome": "applied", "actor": "worker", "result": "commit abc"})
                self.assertEqual(applied.returncode, 0, applied.stderr)
        item = json.loads(self.run_ledger(["list", "--json"]).stdout)["items"][0]
        self.assertEqual(item["status"], "adopted")
        self.assertIsNone(item["implemented_at"])
        self.assertIsNone(item["revoked_at"])

    def test_failed_execution_does_not_describe_a_later_decision(self):
        proposal_id = self.proposal()
        adopted = self.run_ledger(["decide"], {"proposal_id": proposal_id,
            "decision": "adopt", "actor": "Eotel"})
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        failed = self.run_ledger(["record"], {"proposal_id": proposal_id,
            "decision_id": json.loads(adopted.stdout)["id"],
            "outcome": "failed", "actor": "worker", "result": "適用に失敗"})
        self.assertEqual(failed.returncode, 0, failed.stderr)
        item = json.loads(self.run_ledger(["list", "--json"]).stdout)["items"][0]
        self.assertEqual(item["current_execution"]["result"], "適用に失敗")
        revoked = self.run_ledger(["decide"], {"proposal_id": proposal_id,
            "decision": "revoke", "actor": "Eotel"})
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        item = json.loads(self.run_ledger(["list", "--json"]).stdout)["items"][0]
        self.assertEqual(item["status"], "revoked")
        self.assertIsNone(item["current_execution"])
        reversal_failed = self.run_ledger(["record"], {"proposal_id": proposal_id,
            "decision_id": json.loads(revoked.stdout)["id"],
            "outcome": "failed", "actor": "worker", "result": "取り消しに失敗"})
        self.assertEqual(reversal_failed.returncode, 0, reversal_failed.stderr)
        item = json.loads(self.run_ledger(["list", "--json"]).stdout)["items"][0]
        self.assertEqual(item["current_execution"]["result"], "取り消しに失敗")

    def test_late_reversal_stays_with_its_decision(self):
        proposal_id = self.proposal()
        adopted = self.run_ledger(["decide"], {"proposal_id": proposal_id,
            "decision": "adopt", "actor": "Eotel"})
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        revoked = self.run_ledger(["decide"], {"proposal_id": proposal_id,
            "decision": "revoke", "actor": "Eotel"})
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        readopted = self.run_ledger(["decide"], {"proposal_id": proposal_id,
            "decision": "adopt", "actor": "Eotel"})
        self.assertEqual(readopted.returncode, 0, readopted.stderr)
        missing_id = self.run_ledger(["record"], {"proposal_id": proposal_id,
            "outcome": "failed", "actor": "worker", "result": "失敗"})
        self.assertNotEqual(missing_id.returncode, 0)
        revoked_id = json.loads(revoked.stdout)["id"]
        for outcome, result in (("reverted", "旧変更を取り消した"), ("failed", "旧取り消しの報告に失敗")):
            recorded = self.run_ledger(["record"], {"proposal_id": proposal_id,
                "decision_id": revoked_id, "outcome": outcome, "actor": "worker", "result": result})
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
        item = json.loads(self.run_ledger(["list", "--json"]).stdout)["items"][0]
        self.assertEqual(item["status"], "adopted")
        self.assertIsNone(item["current_execution"])
        self.assertIsNone(item["reverted_at"])
        self.assertEqual([event["decision_id"] for event in item["executions"]], [revoked_id, revoked_id])

    def test_clock_ahead_decision_precedes_remote_execution(self):
        proposal_id = self.proposal()
        decision = self.run_ledger(["decide"], {"proposal_id": proposal_id,
            "decision": "adopt", "actor": "Eotel"})
        self.assertEqual(decision.returncode, 0, decision.stderr)
        applied = self.run_ledger(["record"], {"proposal_id": proposal_id,
            "decision_id": json.loads(decision.stdout)["id"],
            "outcome": "applied", "actor": "worker", "result": "commit abc"})
        self.assertEqual(applied.returncode, 0, applied.stderr)
        rows = [json.loads(line) for line in self.events.read_text().splitlines()]
        rows[1]["ts"] = "2026-10-26T23:00:00+09:00"
        self.events.write_text("".join(json.dumps(row) + "\n" for row in rows[:2]))
        self.imports.mkdir()
        (self.imports / "remote.jsonl").write_text(json.dumps(rows[2]) + "\n")
        view = self.run_ledger(["list", "--json"])
        self.assertEqual(view.returncode, 0, view.stderr)
        self.assertEqual(json.loads(view.stdout)["items"][0]["status"], "adopted")
        self.assertEqual(len(json.loads(view.stdout)["items"][0]["executions"]), 1)

    def consolidation(self, relation: str, targets: list[str]) -> subprocess.CompletedProcess[str]:
        return self.run_ledger(["propose"], {
            "source": {"session": "adoption-consolidate", "worktree": "adoption-consolidate"},
            "fact": "同じ話題の案が別々に届いた", "proposal": "検査の option にまとめる",
            "reason": "道具で守らせる", "recommendation": "adopt", "priority": "normal",
            "evidence": ["ledger.py:1"], relation: targets})

    def test_consolidation_links_merged_and_retired_proposals(self) -> None:
        first, second, stale = self.proposal(), self.proposal(), self.proposal()
        merged = self.consolidation("merges", [first, second])
        self.assertEqual(merged.returncode, 0, merged.stderr)
        retired = self.consolidation("retires", [stale])
        self.assertEqual(retired.returncode, 0, retired.stderr)
        items = {item["proposal_id"]: item for item in
                 json.loads(self.run_ledger(["list", "--json"]).stdout)["items"]}
        merged_id = json.loads(merged.stdout)["proposal_id"]
        retired_id = json.loads(retired.stdout)["proposal_id"]
        self.assertEqual(items[merged_id]["merges"], [first, second])
        self.assertEqual(items[retired_id]["retires"], [stale])
        self.assertEqual(items[first]["merged_into"], [merged_id])
        self.assertEqual(items[second]["merged_into"], [merged_id])
        self.assertEqual(items[stale]["retired_by"], [retired_id])
        self.assertEqual(items[stale]["merged_into"], [])

    def test_consolidation_needs_open_targets_not_already_consolidated(self) -> None:
        target, closed = self.proposal(), self.proposal()
        self.run_ledger(["decide"], {"proposal_id": closed, "decision": "reject", "actor": "Eotel"})
        first = self.consolidation("merges", [target])
        self.assertEqual(first.returncode, 0, first.stderr)
        first_id = json.loads(first.stdout)["proposal_id"]
        candidates = json.loads(self.run_ledger(["list", "--json", "--candidates"]).stdout)["items"]
        self.assertEqual([item["proposal_id"] for item in candidates], [first_id])
        lines = len(self.events.read_text().splitlines())
        for relation, targets in (("retires", [target]), ("merges", [closed]),
                                  ("merges", [str(uuid.uuid4())])):
            with self.subTest(relation=relation, targets=targets):
                result = self.consolidation(relation, targets)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.events.read_text().splitlines()), lines)
        self.run_ledger(["decide"], {"proposal_id": first_id, "decision": "reject", "actor": "Eotel"})
        candidates = json.loads(self.run_ledger(["list", "--json", "--candidates"]).stdout)["items"]
        self.assertEqual([item["proposal_id"] for item in candidates], [target])
        again = self.consolidation("retires", [target])
        self.assertEqual(again.returncode, 0, again.stderr)
        lines = len(self.events.read_text().splitlines())
        for decision in ("adopt", "hold"):
            with self.subTest(reactivated=decision):
                result = self.run_ledger(["decide"], {"proposal_id": first_id, "decision": decision,
                                                      "actor": "Eotel"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.events.read_text().splitlines()), lines)

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
        supported = self.run_ledger(["support"], {"proposal_id": proposal_id,
            "source": {"session": "remote", "worktree": "preview"},
            "evidence": ["別の記録"]})
        self.assertEqual(supported.returncode, 0, supported.stderr)
        remote = Path(self.temp.name) / "remote.jsonl"
        rows = [json.loads(line) for line in self.events.read_text().splitlines()]
        for row in rows:
            row["host"] = "renamed-os-host"
        remote.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
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
        remote.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in reversed(rows)))
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([json.loads(line)["type"] for line in (self.imports / "other-mac.jsonl").read_text().splitlines()],
                         ["proposal", "evidence"])
        decision = {**rows[1], "type": "decision", "decision": "adopt", "actor": "other Mac"}
        remote.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [rows[0], decision]))
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        remote.write_text("")
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(proposal_id, (self.imports / "other-mac.jsonl").read_text())

    def test_remote_implementer_reads_writer_decision_before_recording(self):
        root = Path(self.temp.name)
        writer = root / "writer"
        remote = root / "remote"
        writer.mkdir()
        remote.mkdir()
        writer_config = writer / "hosts.json"
        remote_config = remote / "hosts.json"
        writer_config.write_text(json.dumps({"decision_writer": True, "name": "board", "hosts": [{
            "name": "remote", "ssh_target": "remote", "remote_path": "/remote/events.jsonl"}]}))
        remote_config.write_text(json.dumps({"trusted_writer": "board", "hosts": []}))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        scp = bin_dir / "scp"
        scp.write_text(f"#!{sys.executable}\nimport json, os, shutil, sys\n"
                       "files = json.loads(os.environ['FAKE_FILES'])\n"
                       "shutil.copyfile(files[sys.argv[-2].split(':', 1)[0]], sys.argv[-1])\n")
        scp.chmod(0o755)
        ssh = bin_dir / "ssh"
        ssh.write_text(f"#!{sys.executable}\nimport os, subprocess, sys\n"
                       "command = [sys.executable, os.environ['FAKE_SCRIPT'], '--events', os.environ['FAKE_REMOTE_EVENTS'], "
                       "'--imports', os.environ['FAKE_REMOTE_IMPORTS'], 'receive', '--name', 'board', "
                       "'--config', os.environ['FAKE_REMOTE_CONFIG']]\n"
                       "result = subprocess.run(command, input=sys.stdin.buffer.read(), capture_output=True)\n"
                       "sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr)\n"
                       "sys.exit(result.returncode)\n")
        ssh.chmod(0o755)
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
               "FAKE_FILES": json.dumps({"remote": str(remote / "events.jsonl"),
                                          "writer": str(writer / "events.jsonl")}),
               "FAKE_SCRIPT": str(SCRIPT), "FAKE_REMOTE_EVENTS": str(remote / "events.jsonl"),
               "FAKE_REMOTE_IMPORTS": str(remote / "imports"), "FAKE_REMOTE_CONFIG": str(remote_config)}

        def run(directory, config, command, body=None):
            return subprocess.run([sys.executable, str(SCRIPT), "--events", str(directory / "events.jsonl"),
                                   "--imports", str(directory / "imports"), "--decision-config", str(config),
                                   *command], input=json.dumps(body) if body is not None else None,
                                  env=env, text=True, capture_output=True)

        proposed = run(remote, remote_config, ["propose"], {
            "source": {"session": "remote", "worktree": "remote"}, "fact": "遠隔で失敗した",
            "proposal": "手順を直す", "reason": "使い方に関わる", "recommendation": "adopt",
            "priority": "normal"})
        self.assertEqual(proposed.returncode, 0, proposed.stderr)
        proposal_id = json.loads(proposed.stdout)["proposal_id"]
        collected = run(writer, writer_config, ["collect", "--config", str(writer_config)])
        self.assertEqual(collected.returncode, 0, collected.stderr)
        proposed_row = json.loads((remote / "events.jsonl").read_text().strip())
        other_evidence = {"id": str(uuid.uuid4()), "ts": proposed_row["ts"], "host": "other Mac",
                          "type": "evidence", "proposal_id": proposal_id, "after_id": proposed_row["id"],
                          "source": {"session": "other", "worktree": "other"}, "evidence": ["別の Mac でも再発"]}
        (writer / "imports" / "other.jsonl").write_text(json.dumps(other_evidence, ensure_ascii=False) + "\n")
        adopted = run(writer, writer_config, ["decide"], {"proposal_id": proposal_id,
            "decision": "adopt", "actor": "Eotel"})
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        adopted_id = json.loads(adopted.stdout)["id"]
        before_sync = run(remote, remote_config, ["record"], {"proposal_id": proposal_id,
            "decision_id": adopted_id,
            "outcome": "applied", "actor": "remote worker", "result": "commit abc"})
        self.assertNotEqual(before_sync.returncode, 0)
        synchronized = run(writer, writer_config, ["collect", "--config", str(writer_config)])
        self.assertEqual(synchronized.returncode, 0, synchronized.stderr)
        applied = run(remote, remote_config, ["record"], {"proposal_id": proposal_id,
            "decision_id": adopted_id,
            "outcome": "applied", "actor": "remote worker", "result": "commit abc"})
        self.assertEqual(applied.returncode, 0, applied.stderr)
        collected = run(writer, writer_config, ["collect", "--config", str(writer_config)])
        self.assertEqual(collected.returncode, 0, collected.stderr)
        view = run(writer, writer_config, ["list", "--json"])
        self.assertEqual(view.returncode, 0, view.stderr)
        item = json.loads(view.stdout)["items"][0]
        self.assertEqual(item["status"], "adopted")
        self.assertEqual(item["executions"][0]["result"], "commit abc")
        revoked = run(writer, writer_config, ["decide"], {"proposal_id": proposal_id,
            "decision": "revoke", "actor": "Eotel"})
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        late_result = run(remote, remote_config, ["record"], {"proposal_id": proposal_id,
            "decision_id": adopted_id,
            "outcome": "applied", "actor": "remote worker", "result": "late commit"})
        self.assertEqual(late_result.returncode, 0, late_result.stderr)
        collected = run(writer, writer_config, ["collect", "--config", str(writer_config)])
        self.assertEqual(collected.returncode, 0, collected.stderr)
        view = run(writer, writer_config, ["list", "--json"])
        self.assertEqual(view.returncode, 0, view.stderr)
        item = json.loads(view.stdout)["items"][0]
        self.assertEqual(item["status"], "revoked")
        self.assertIsNone(item["current_execution"])
        self.assertEqual(item["executions"][-1]["result"], "late commit")
        self.assertEqual(item["executions"][-1]["decision_id"], adopted_id)

    def test_collect_does_not_treat_read_failure_as_missing(self):
        root = Path(self.temp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, code in (("scp", 7), ("ssh", 1)):
            command = bin_dir / name
            command.write_text(f"#!{sys.executable}\nimport sys\nsys.exit({code})\n")
            command.chmod(0o755)
        config = root / "hosts.json"
        config.write_text(json.dumps({"hosts": [{"name": "remote", "ssh_target": "remote",
            "remote_path": "/remote/events.jsonl"}]}))
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
        command = [sys.executable, str(SCRIPT), "--events", str(self.events),
            "--imports", str(self.imports), "collect", "--config", str(config)]
        failed = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("scp exit 7", failed.stderr)
        self.assertIn("remote check exit 1", failed.stderr)
        self.assertFalse((self.imports / "remote.jsonl").exists())
        missing = bin_dir / "ssh"
        missing.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(42)\n")
        empty = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual((self.imports / "remote.jsonl").read_bytes(), b"")

    def test_writer_snapshot_may_reorder_but_cannot_drop_history(self):
        config = Path(self.temp.name) / "remote-hosts.json"
        config.write_text(json.dumps({"trusted_writer": "board", "hosts": []}))
        rows = [{"id": str(uuid.uuid4()), "ts": "2026-10-26T19:00:00+09:00", "host": "writer",
                 "type": "proposal", "proposal_id": str(uuid.uuid4()),
                 "source": {"session": "writer", "worktree": "writer"}, "fact": "再発",
                 "proposal": f"案 {index}", "reason": "判断待ち", "recommendation": "adopt",
                 "priority": "normal", "evidence": []} for index in range(3)]

        def receive(snapshot):
            return subprocess.run([sys.executable, str(SCRIPT), "--events", str(self.events),
                "--imports", str(self.imports), "receive", "--name", "board", "--config", str(config)],
                input="".join(json.dumps(row) + "\n" for row in snapshot), text=True, capture_output=True)

        self.assertEqual(receive(rows[:2]).returncode, 0)
        reordered = receive([rows[1], rows[0], rows[2]])
        self.assertEqual(reordered.returncode, 0, reordered.stderr)
        view = self.run_ledger(["list", "--json"])
        self.assertEqual(len(json.loads(view.stdout)["items"]), 3)
        dropped = receive([rows[1], rows[2]])
        self.assertNotEqual(dropped.returncode, 0)
        self.assertEqual(len((self.imports / "board.jsonl").read_text().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
