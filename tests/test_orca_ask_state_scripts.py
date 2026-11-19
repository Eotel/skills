"""Behavior checks for waiting reports, shared answers, and duplicate question guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

NO_BOARD_COMPONENT = tempfile.TemporaryDirectory(prefix="triage-tests-bin-")
os.environ["ORCA_BRIEFS_BIN"] = NO_BOARD_COMPONENT.name



SCRIPTS = Path(__file__).resolve().parents[1] / "orca-session-triage/scripts"


class AskStateTests(unittest.TestCase):
    ASK = "a1234567-1234-4567-8888-123456789abc"
    AT = "2026-11-14T09:01:00+00:00"
    KEY = f"answer:worker::{ASK}"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "hourly-state.json"
        self.log = self.root / "lead.jsonl"
        self.handed = self.root / "handed.jsonl"
        self.inbox = self.root / "inbox.jsonl"
        self.answers = self.root / "answers.jsonl"
        self.claims = self.root / "claims.jsonl"
        self.env = {**os.environ, "TRIAGE_INBOX": str(self.inbox), "TRIAGE_ASK_ANSWERS": str(self.answers),
                    "TRIAGE_ASK_CLAIMS": str(self.claims), "TRIAGE_HOURLY_STATE": str(self.state),
                    "TRIAGE_STATE_DIR": str(self.root), "TRIAGE_LEAD_DECISIONS": str(self.log),
                    "TRIAGE_HANDED_OFF": str(self.handed), "ORCA_SESSION_TRIAGE_SCRIPTS": str(SCRIPTS),
                    "ORCA_WORKTREE_PATH": "/w/hourly", "ORCA_WORKTREE_ID": "repo::/w/hourly",
                    "ORCA_PANE_KEY": "", "CLAUDE_CODE_SESSION_ID": "hourly-session"}
        self.env.pop("CODEX_THREAD_ID", None)
        self.question = {"question": "公開しますか？", "header": "公開", "multiSelect": False,
                         "options": [{"label": "公開する", "description": "公開する"},
                                     {"label": "待つ", "description": "待つ"}]}
        self.write_lines(self.inbox, [{"id": self.ASK, "ts": "2026-11-14T09:00:00Z", "kind": "ask",
                                      "worktree": "worker", "text": "公開しますか？", "questions": [self.question]}])

    def write_lines(self, path: Path, items: list[dict]) -> None:
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items))

    def run_tool(self, script: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], env=self.env,
                              input=stdin, text=True, capture_output=True, timeout=30)

    def ask(self, *, key: str | None = None, covers: list[str] | None = None) -> dict:
        item = {"row": "/w/worker", "decision": key or "publish", "final_message": "公開しますか？",
                "question": "公開しますか？", "header": "公開", "asked_at": self.AT,
                "covers": covers if covers is not None else [self.KEY]}
        result = self.run_tool("hourly_state.py", "asked-many", "--no-comment", stdin=json.dumps(item))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)[0]

    def python(self, code: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([sys.executable, "-c", code], cwd=SCRIPTS, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def relay(self, text: str, at: str) -> None:
        self.python(f"import answer_sync; answer_sync.record([{self.ASK!r}], {text!r}, 'send', at={at!r})")

    def test_unicode_line_separator_survives_batch_relay_and_decision_readers(self) -> None:
        asked = self.ask()
        answer = "前\u2028後"
        result = self.run_tool("hourly_state.py", "answered-many", stdin=json.dumps(
            {"id": asked["id"], "answer": answer, "answered_at": self.AT}, ensure_ascii=False))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.relay("中継しました", self.AT)
        decided = self.run_tool("lead_decisions.py", "--decisions-file", str(self.log), "show")
        self.assertEqual(decided.returncode, 0, decided.stderr)
        self.assertEqual(json.loads(decided.stdout)[0]["answer"], answer)
        probe = self.python("import answer_evidence, inbox_read; "
                            "print(len(list(answer_evidence.delivered(inbox_read.answers_path()))))")
        self.assertEqual(probe.stdout.strip(), "1")

    def test_part_correction_changes_the_automatic_whole_key_and_reader_wording(self) -> None:
        self.write_lines(self.inbox, [{"id": self.ASK, "kind": "ask", "questions": [self.question, self.question]}])
        self.ask()
        first = self.ask(key=f"{self.KEY}:1", covers=[f"{self.KEY}:1"])
        self.relay("両方公開する", self.AT)
        corrected = self.run_tool("hourly_state.py", "answered-many", stdin=json.dumps(
            {"id": first["id"], "answer": "第一案は中止", "replace_reason": "本人が訂正した", "answered_at": self.AT}))
        self.assertEqual(corrected.returncode, 0, corrected.stderr)
        decided = self.run_tool("lead_decisions.py", "--decisions-file", str(self.log), "show")
        self.assertEqual(decided.returncode, 0, decided.stderr)
        keys = {item["decision"]: item["answer"] for item in json.loads(decided.stdout)}
        self.assertEqual(keys[self.KEY], "第一案は中止\n両方公開する")
        probe = self.python("import inbox_read, lead_decisions, scan_sessions, json; "
                            "ask = inbox_read.inbox_entries(inbox_read.paths()[0])[0]; "
                            "path = inbox_read.answers_path(); "
                            "print(json.dumps([scan_sessions.ask_answer(ask, scan_sessions.ask_answers(path, include_sources=True)), "
                            "lead_decisions.ask_answer(ask['id'], lead_decisions.ask_answers(path), {ask['id']: ask}), "
                            "inbox_read.delivered_records(path)]))")
        scanned, handoff, delivered = json.loads(probe.stdout)
        self.assertIn("第一案は中止", scanned["note"])
        self.assertEqual(handoff["answers"][0]["note"], "第一案は中止")
        self.assertEqual(delivered[self.ASK]["note"], "両方公開する")

    def test_legacy_automatic_decision_needs_delivery_provenance(self) -> None:
        question = self.ask()
        self.relay("公開する", self.AT)
        old = json.loads(self.log.read_text())
        old.pop("from_ask")
        self.write_lines(self.log, [old])
        correction = self.run_tool("hourly_state.py", "answered-many", stdin=json.dumps(
            {"id": question["id"], "answer": "中止する", "replace_reason": "本人が訂正した", "answered_at": self.AT}))
        self.assertEqual(correction.returncode, 0, correction.stderr)
        updated = [json.loads(line) for line in self.log.read_text().split("\n") if line]
        self.assertEqual([(item["answer"], item.get("from_ask")) for item in updated],
                         [("公開する", None), ("中止する", True)])

    def test_returning_to_an_earlier_answer_creates_a_new_delivery(self) -> None:
        question = self.ask()
        self.relay("公開する", self.AT)
        for answer, when in (("中止する", "2026-11-15T09:01:00+00:00"), ("公開する", "2026-11-16T09:01:00+00:00")):
            result = self.run_tool("hourly_state.py", "answered-many", stdin=json.dumps(
                {"id": question["id"], "answer": answer, "replace_reason": "本人が訂正した", "answered_at": when}))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.relay(answer, when)
        journal = [json.loads(line) for line in self.answers.read_text().split("\n") if line]
        self.assertEqual([item["note"] for item in journal if item["how"] == "send"], ["公開する", "中止する", "公開する"])

    def test_parallel_explicit_decision_wins_over_an_automatic_correction(self) -> None:
        asked = self.ask()
        self.relay("公開する", self.AT)
        answer = self.root / "manual.txt"
        answer.write_text("来週まで保留")
        explicit = [sys.executable, str(SCRIPTS / "lead_decisions.py"), "--decisions-file", str(self.log),
                    "record", "--decision", self.KEY, "--answer-file", str(answer)]
        correction = {"id": asked["id"], "answer": "中止する", "replace_reason": "本人が訂正した"}
        self.python(f"""
import io
import subprocess
import sys
from unittest.mock import patch

import hourly_state
import lead_decisions

records = lead_decisions.records
writer = None

def overlapping(path):
    global writer
    snapshot = list(records(path))
    if writer is None:
        writer = subprocess.Popen({explicit!r}, stdout=subprocess.DEVNULL)
        try:
            writer.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    return iter(snapshot)

with patch.object(lead_decisions, "records", overlapping), patch.object(sys, "stdin", io.StringIO({json.dumps(correction)!r})):
    assert hourly_state.main(["answered-many"]) == 0
assert writer is not None and writer.wait(timeout=10) == 0
""")
        entries = [json.loads(line) for line in self.log.read_text().split("\n") if line]
        self.assertEqual(entries[-1]["answer"], "来週まで保留")
        self.assertIs(entries[-1]["from_ask"], False)

    def test_new_explicit_inbox_decision_validates_its_clock(self) -> None:
        self.ask()
        now = datetime.now(timezone(timedelta(hours=9)))
        stale = (now - timedelta(hours=2)).strftime("%H:%M")
        result = self.run_tool("inbox_read.py", "answered", self.ASK, "--note", "定時で回答",
                               "--decision", f"{self.KEY}={stale} に回答")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("minutes from answered_at", result.stderr)
        self.assertFalse(self.answers.exists())
        self.assertFalse(self.log.exists())

    def test_waiting_report_and_needs_user_are_visible_without_misclassifying_another_wait(self) -> None:
        rows = [{"path": "/w/worker", "repo": "skills", "class": "stalled",
                 "verdict": {"class": "blocked", "waits_on": "reviewer の報告。三浦さんへは連絡済み",
                             "needs_user": "", "next_step": "報告を読む"},
                 "claude": {"final_text": "待機中", "final_at": "2026-11-14T09:00:00Z"}}]
        scan = self.root / "scan.json"
        scan.write_text(json.dumps(rows))
        self.inbox.write_text("")
        args = ["stale-asks", "--from", str(scan), "--verdicts", str(self.root / "no-verdicts"),
                "--decisions-file", str(self.log), "--handed-off-file", str(self.handed),
                "--handoffs-dir", str(self.root / "handoffs"), "--claude-projects", str(self.root / "projects")]
        result = self.run_tool("hourly_state.py", *args)
        self.assertEqual(json.loads(result.stdout), [], result.stderr)
        self.write_lines(self.inbox, [
            {"id": "old", "ts": "2026-11-14T08:59:00Z", "kind": "done", "worktree": "reviewer"},
            {"id": "wrong", "ts": self.AT, "kind": "info", "worktree": "reviewer-extra"},
            {"id": "fresh", "ts": self.AT, "kind": "info", "worktree": "reviewer", "text": "確認済み"}])
        result = self.run_tool("hourly_state.py", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)[0]
        self.assertEqual([item["id"] for item in row["waiting_reports"]], ["fresh"])
        self.assertEqual(row["next_step"], "報告を読む")
        summary = self.run_tool("hourly_state.py", *args, "--summary")
        self.assertIn("待ち先から届いた", summary.stdout)
        self.assertIn("fresh", summary.stdout)
        rows[0]["verdict"].update(needs_user="許可", waits_on="三浦さん")
        scan.write_text(json.dumps(rows))
        self.inbox.write_text("")
        result = self.run_tool("hourly_state.py", *args)
        self.assertEqual(json.loads(result.stdout)[0]["waits_on"], "三浦さん")

    def test_delivered_answer_updates_covering_question_and_all_keys_at_the_same_time(self) -> None:
        question = self.ask()
        self.write_lines(self.handed, [{"decision": self.KEY, "handed_off_at": self.AT, "ask_id": self.ASK}])
        # Call the public delivery boundary with isolated stores, as send does after acceptance.
        command = ("import answer_sync; answer_sync.record(" + repr([self.ASK]) +
                   ", '公開する', 'send', at=" + repr(self.AT) + ")")
        result = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.state.read_text())["questions"][question["id"]]
        recorded = json.loads(self.answers.read_text())
        decision = json.loads(self.log.read_text())
        self.assertEqual((state["answer"], recorded["note"], decision["answer"]), ("公開する",) * 3)
        self.assertEqual((state["answered_at"], recorded["ts"], decision["decided_at"]), (self.AT,) * 3)

    def test_explicit_answers_close_only_bound_parts_and_can_be_relayed_from_the_same_session(self) -> None:
        self.env["ORCA_TERMINAL_HANDLE"] = "term_fixture"
        self.write_lines(self.inbox, [{"id": self.ASK, "kind": "ask", "questions": [self.question, self.question]}])
        part = f"{self.ASK}:1"
        first = self.ask(key=f"answer:worker::{part}", covers=[f"answer:worker::{part}"])
        second = self.ask(key=f"answer:worker::{self.ASK}:2", covers=[f"answer:worker::{self.ASK}:2"])
        answer = self.root / "answer.txt"
        answer.write_text("公開する（23:00 の定時のタブで回答）")
        recorded = self.run_tool("hourly_state.py", "answered", "--id", first["id"],
                                 "--answer-file", str(answer), "--answered-at", self.AT)
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(json.loads(self.answers.read_text())["id"], part)

        command = f"import answer_sync; answer_sync.record([{part!r}], '公開する', 'send', at={self.AT!r})"
        other = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS,
                               env={**self.env, "CLAUDE_CODE_SESSION_ID": "another-session"},
                               capture_output=True, text=True, timeout=30)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("answer already recorded", other.stderr)
        relay = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS,
                               env={key: value for key, value in self.env.items() if key != "ORCA_TERMINAL_HANDLE"},
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(relay.returncode, 0, relay.stderr)
        self.assertEqual(json.loads(self.log.read_text())["answer"], answer.read_text())
        self.assertEqual(json.loads(self.answers.read_text().splitlines()[-1])["how"], "send")

        batch = self.run_tool("hourly_state.py", "answered-many", "--skip-recorded", stdin="\n".join(
            json.dumps({"id": question["id"], "answer": "待つ", "answered_at": self.AT})
            for question in (first, second)))
        self.assertEqual(batch.returncode, 0, batch.stderr)
        self.assertIn("行 1 は回答済み", batch.stderr)
        self.assertEqual([item["answer"] for item in json.loads(batch.stdout)], [answer.read_text(), "待つ"])
        self.assertEqual(json.loads(self.answers.read_text().splitlines()[-1])["id"], f"{self.ASK}:2")

    def test_numbered_deliveries_leave_other_questions_open_and_complete_a_whole_ask(self) -> None:
        self.write_lines(self.inbox, [{"id": self.ASK, "kind": "ask", "questions": [self.question, self.question]}])
        whole = self.ask()
        first = self.ask(key=f"ask:worker::{self.ASK}:1", covers=[f"answer:worker::{self.ASK}:1"])
        self.write_lines(self.answers, [{"id": f"{self.ASK}:1", "how": "inbox_read.py answered", "note": "読んだ", "ts": self.AT}])
        def deliver(number: int, text: str) -> None:
            command = f"import answer_sync; answer_sync.record([{self.ASK + ':' + str(number)!r}], {text!r}, 'send', at={self.AT!r})"
            result = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS, env=self.env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
        deliver(1, "公開する（{answered_at}）")
        questions = json.loads(self.state.read_text())["questions"]
        self.assertEqual(questions[first["id"]]["answer"], "公開する（18:01）")
        self.assertIsNone(questions[whole["id"]]["answer"])
        deliver(2, "待つ")
        questions = json.loads(self.state.read_text())["questions"]
        self.assertEqual(questions[whole["id"]]["answer"], "公開する（18:01）\n待つ")
        decisions = {item["decision"]: item for item in map(json.loads, self.log.read_text().splitlines())}
        self.assertEqual(decisions[self.KEY]["answer"], "公開する（18:01）\n待つ")

    def test_an_isolated_state_file_leaves_the_shared_ask_open(self) -> None:
        question = self.ask()
        copy = self.root / "copy.json"
        copy.write_bytes(self.state.read_bytes())
        answer = self.root / "answer.txt"
        answer.write_text("公開する")
        result = self.run_tool("hourly_state.py", "--state-file", str(copy), "answered",
                               "--id", question["id"], "--answer-file", str(answer), "--answered-at", self.AT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(copy.read_text())["questions"][question["id"]]["answer"], "公開する")
        self.assertIsNone(json.loads(self.state.read_text())["questions"][question["id"]]["answer"])
        self.assertFalse(self.answers.exists())

    def test_an_invalid_inbox_binding_rejects_the_batch_before_either_store_changes(self) -> None:
        keys = [f"answer:worker::{self.ASK}:{number}" for number in (1, 2)]
        questions = [self.ask(key=key, covers=[key]) for key in keys]
        before = self.state.read_bytes()
        result = self.run_tool("hourly_state.py", "answered-many", stdin="\n".join(
            json.dumps({"id": question["id"], "answer": "公開する", "answered_at": self.AT})
            for question in questions))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"no ask in the inbox has id {self.ASK}:2", result.stderr)
        self.assertIn("line 2", result.stderr)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.answers.exists())

    def test_a_corrected_hourly_answer_counts_when_a_delivery_completes_the_ask(self) -> None:
        self.write_lines(self.inbox, [{"id": self.ASK, "kind": "ask", "questions": [self.question, self.question]}])
        whole = self.ask()
        first = self.ask(key=f"answer:worker::{self.ASK}:1", covers=[f"answer:worker::{self.ASK}:1"])
        args = ("answered-many", "--skip-recorded")
        for note, extra in [("公開する", {}), ("待つ（{answered_at}）", {"replace_reason": "本人が訂正した"})]:
            record = self.run_tool("hourly_state.py", *args, stdin=json.dumps(
                {"id": first["id"], "answer": note, "answered_at": self.AT, **extra}))
            self.assertEqual(record.returncode, 0, record.stderr)
        self.assertEqual([item["note"] for item in map(json.loads, self.answers.read_text().splitlines())],
                         ["公開する", "待つ（18:01）"])
        command = ("import answer_sync; "
                   f"answer_sync.record([{self.ASK + ':2'!r}], '戻す', 'send', at='2026-11-14T11:01:00+00:00')")
        delivery = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS, env=self.env,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(delivery.returncode, 0, delivery.stderr)
        self.assertEqual(json.loads(self.state.read_text())["questions"][whole["id"]]["answer"], "待つ（18:01）\n戻す")

    def test_relay_retry_keeps_the_correction_and_existing_delivery(self) -> None:
        question = self.ask()
        command = f"import answer_sync; answer_sync.record([{self.ASK!r}], '公開する', 'send', at={self.AT!r})"
        for replay in (False, True):
            result = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS, env=self.env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            if not replay:
                self.assertEqual(self.run_tool("hourly_state.py", "answered-many", stdin=json.dumps(
                    {"id": question["id"], "answer": "公開しない", "replace_reason": "本人が訂正した",
                     "answered_at": "2026-11-14T11:01:00+00:00"})).returncode, 0)
        probe = ("import inbox_read, lead_decisions, json; path = inbox_read.answers_path(); "
                 "print(json.dumps([inbox_read.answer_records(path), lead_decisions.ask_answers(path)]))")
        result = subprocess.run([sys.executable, "-c", probe], cwd=SCRIPTS, env=self.env,
                                capture_output=True, text=True, timeout=30, check=True)
        answers, deliveries = json.loads(result.stdout)
        self.assertEqual(answers[self.ASK]["note"], "公開しない")
        self.assertEqual(deliveries[self.ASK]["note"], "公開しない")
        self.assertEqual(deliveries[self.ASK]["delivery"]["note"], "公開する")
        self.assertEqual(len(self.answers.read_text().splitlines()), 2)
        self.assertEqual(json.loads(self.log.read_text().splitlines()[-1])["answer"], "公開しない")
        for text in ("公開しません", "公開しない", "公開する"):
            relay = (f"import answer_sync; answer_sync.record([{self.ASK!r}], {text!r}, 'send', "
                     "at='2026-11-14T12:01:00+00:00')")
            result = subprocess.run([sys.executable, "-c", relay], cwd=SCRIPTS, env=self.env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.answers.read_text().splitlines()), 3)
        self.assertEqual(json.loads(self.log.read_text().splitlines()[-1])["answer"], "公開しない")

    def test_a_whole_ask_relay_keeps_numbered_answers_under_their_own_keys(self) -> None:
        self.write_lines(self.inbox, [{"id": self.ASK, "kind": "ask", "questions": [self.question, self.question]}])
        self.ask()
        answers = {f"{self.KEY}:1": "第一案を公開する", f"{self.KEY}:2": "第二案は見送る"}
        items = [{"id": self.ask(key=key, covers=[key])["id"], "answer": text, "answered_at": self.AT}
                 for key, text in answers.items()]
        recorded = self.run_tool("hourly_state.py", "answered-many", stdin="\n".join(map(json.dumps, items)))
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        command = f"import answer_sync; answer_sync.record([{self.ASK!r}], '第一案だけ公開してください。', 'send', at={self.AT!r})"
        result = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        decisions = {item["decision"]: item["answer"] for item in map(json.loads, self.log.read_text().splitlines())}
        self.assertEqual(decisions, {**answers, self.KEY: "\n".join(answers.values())})

    def test_an_inbox_append_retry_keeps_the_original_answering_session(self) -> None:
        question = self.ask()
        line = json.dumps({"id": question["id"], "answer": "公開する", "answered_at": self.AT})
        failure = ("import sys, hourly_state, inbox_read; from unittest.mock import patch; "
                   "patch.object(inbox_read, 'record_answers', side_effect=OSError('fixture append error')).start(); "
                   "sys.exit(hourly_state.main(['answered-many']))")
        result = subprocess.run([sys.executable, "-c", failure], cwd=SCRIPTS, env=self.env, input=line,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("行 1、質問 ID " + question["id"], result.stderr)
        origin = self.env["CLAUDE_CODE_SESSION_ID"]
        self.env["CLAUDE_CODE_SESSION_ID"] = "other-session"
        result = self.run_tool("hourly_state.py", "answered-many", "--skip-recorded", stdin=line)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.answers.read_text())["sender"]["session"], origin)

    def test_a_direct_ask_and_separate_cover_have_evidence_for_stale_asks(self) -> None:
        another = "b1234567-1234-4567-8888-123456789abc"
        with self.inbox.open("a") as stream:
            stream.write(json.dumps({"id": another, "kind": "ask"}) + "\n")
        scan = self.root / "scan.json"
        scan.write_text(json.dumps([{"path": "/w/worker", "repo": "", "class": "waiting",
                                     "claude": {"final_text": "公開しますか？", "final_at": "2026-11-14T09:00:00Z"}}]))
        asked = self.run_tool("hourly_state.py", "asked-many", "--no-comment", "--from", str(scan),
                              stdin=json.dumps({"ask": self.ASK, "decision": "publish", "question": "公開しますか？",
                                                "covers": [f"answer:other::{another}"], "asked_at": self.AT}))
        self.assertEqual(asked.returncode, 0, asked.stderr)
        recorded = self.run_tool("hourly_state.py", "answered-many", stdin=json.dumps(
            {"id": json.loads(asked.stdout)[0]["id"], "answer": "待つ", "answered_at": self.AT}))
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual({item["id"] for item in map(json.loads, self.answers.read_text().splitlines())},
                         {self.ASK, another})
        result = self.run_tool("hourly_state.py", "stale-asks", "--from", str(scan),
                               "--verdicts", str(self.root / "no-verdicts"), "--claude-projects", str(self.root / "projects"))
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)[0]
        self.assertEqual((row["state"], row["answer"]["text"]), ("stopped_after_answer", "待つ"))

    def test_a_partial_relay_preserves_the_hourly_answer_and_records_an_explicit_key(self) -> None:
        self.env["ORCA_TERMINAL_HANDLE"] = "term_fixture"
        other = "b1234567-1234-4567-8888-123456789abc"
        self.write_lines(self.inbox, [{"id": ask_id, "kind": "ask"} for ask_id in (self.ASK, other)])
        key = "keep:/w/current"
        question = self.ask(covers=[self.KEY, f"answer:worker::{other}", key])
        answer = "両方とも公開する"
        self.assertEqual(self.run_tool("hourly_state.py", "answered-many", stdin=json.dumps(
            {"id": question["id"], "answer": answer, "answered_at": self.AT})).returncode, 0)
        command = ("import answer_sync; "
                   f"answer_sync.record([{other!r}], '公開する', 'send', at={self.AT!r}, "
                   f"decisions={ {key: '維持する'}!r})")
        delivery = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS, env=self.env,
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(delivery.returncode, 0, delivery.stderr)
        self.assertEqual(json.loads(self.state.read_text())["questions"][question["id"]]["answer"], answer)
        self.assertEqual({item["decision"]: item["answer"] for item in map(json.loads, self.log.read_text().splitlines())}
                         [key], "維持する")

    def test_mixed_decisions_wait_for_every_cover_and_a_replay_preserves_a_newer_correction(self) -> None:
        question = self.ask(covers=[self.KEY, "remove:/w/other"])
        def deliver() -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, "-c", f"import answer_sync; answer_sync.record([{self.ASK!r}], '公開する', 'send', at={self.AT!r})"],
                                  cwd=SCRIPTS, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(deliver().returncode, 0)
        self.assertIsNone(json.loads(self.state.read_text())["questions"][question["id"]]["answer"])
        with self.log.open("a") as stream:
            stream.write(json.dumps({"id": "correction", "decision": self.KEY, "answer": "止める",
                                     "decided_at": "2026-11-14T09:05:00+00:00"}) + "\n")
        before = self.log.read_text()
        replay = deliver()
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("newer lead decision", replay.stderr)
        self.assertEqual(self.log.read_text(), before)

    def test_an_existing_explicit_decision_is_used_for_the_linked_hourly_answer(self) -> None:
        question = self.ask()
        self.write_lines(self.log, [{"id": "decision", "decision": self.KEY, "answer": "待つ", "decided_at": self.AT}])
        command = (f"import answer_sync; answer_sync.record([{self.ASK!r}], '公開する', 'send', "
                   "at='2026-11-14T10:01:00+00:00')")
        result = subprocess.run([sys.executable, "-c", command], cwd=SCRIPTS, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["questions"][question["id"]]["answer"], "待つ")
        self.assertEqual(json.loads(self.log.read_text())["answer"], "待つ")

    def test_keyed_handoff_requires_each_answer_and_writes_nothing_on_missing_key(self) -> None:
        text = "merge:repo#1\nkeep:/w/a\n—— 送る直前に道具が足した事実 ——"
        self.write_lines(self.inbox, [{"id": self.ASK, "kind": "ask", "text": text}])
        self.write_lines(self.handed, [{"decision": key, "handed_off_at": self.AT} for key in ("merge:repo#1", "keep:/w/a")])
        args = ["answered", self.ASK, "--note", "別のタブで答えた", "--state-file", str(self.state),
                "--decisions-file", str(self.log), "--handed-off-file", str(self.handed),
                "--handoffs-dir", str(self.root / "handoffs")]
        refused = self.run_tool("inbox_read.py", *args, "--decision", "merge:repo#1=merge する")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("keep:/w/a", refused.stderr)
        self.assertFalse(self.answers.exists())
        result = self.run_tool("inbox_read.py", *args, "--decision", "merge:repo#1=merge する",
                               "--decision", "keep:/w/a=残す")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item["answer"] for item in map(json.loads, self.log.read_text().splitlines())], ["merge する", "残す"])

    def test_saved_handoff_decisions_override_incidental_keys_and_its_note_does_not_become_the_answer(self) -> None:
        text = "残す判断を伺う\n—— 送る直前に道具が足した事実 ——\n報告: merge:repo#1 は回答済み"
        self.write_lines(self.inbox, [{"id": self.ASK, "kind": "ask", "text": text}])
        saved = self.root / "handoffs"
        saved.mkdir()
        (saved / "saved.json").write_text(json.dumps({"text": text, "decisions": ["keep:/w/current"]}))
        self.write_lines(self.handed, [{"decision": key, "handed_off_at": self.AT}
                                      for key in ("keep:/w/current", "merge:repo#1")])
        question = self.ask(covers=["keep:/w/current"])
        state = json.loads(self.state.read_text())
        state["questions"][question["id"]]["ask_id"] = self.ASK
        self.state.write_text(json.dumps(state))
        result = self.run_tool("inbox_read.py", "answered", self.ASK, "--note", "別のタブで答えた",
                               "--decision", "keep:/w/current=残す", "--state-file", str(self.state),
                               "--decisions-file", str(self.log), "--handed-off-file", str(self.handed),
                               "--handoffs-dir", str(saved))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.state.read_text())["questions"][question["id"]]["answer"], "残す")
        self.assertEqual(json.loads(self.log.read_text())["decision"], "keep:/w/current")

    def test_note_only_answer_is_a_candidate_and_asked_keys_show_the_asking_workspace(self) -> None:
        self.ask()
        self.write_lines(self.handed, [{"decision": self.KEY, "handed_off_at": self.AT, "ask_id": self.ASK}])
        self.write_lines(self.answers, [{"id": self.ASK, "ts": self.AT, "how": "inbox_read.py answered", "note": "読んだ"}])
        result = self.run_tool("lead_decisions.py", "--decisions-file", str(self.log), "unanswered",
                               "--handed-off-file", str(self.handed), "--state-file", str(self.state))
        self.assertEqual(result.returncode, 0, result.stderr)
        row = next(item for item in json.loads(result.stdout)["unanswered"] if item["decision"] == self.KEY)
        self.assertEqual(row["candidates"][0]["note"], "読んだ")
        self.assertEqual((row["asking"][0]["by"], row["asking"][0]["at"]), ("/w/hourly", self.AT))
        watching = self.run_tool("hourly_state.py", "asking", "--summary")
        self.assertIn("伺い中 /w/hourly", watching.stdout)
        self.assertIn(self.KEY, watching.stdout)

    def hook(self) -> subprocess.CompletedProcess[str]:
        state = json.loads(self.state.read_text()) if self.state.exists() else {"version": 1, "questions": {}}
        state["lead"] = {"worktree": "/w/lead"}
        self.state.write_text(json.dumps(state))
        return self.run_tool("stop_check.py", stdin=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                             "cwd": "/w/lead", "session_id": "lead-session", "tool_input": {"questions": [self.question]}}))

    def test_guard_names_an_answered_or_claimed_ask_and_another_tabs_asking_record(self) -> None:
        self.ask()
        guarded = self.hook()
        self.assertIn("伺い中", json.loads(guarded.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        state = json.loads(self.state.read_text())
        state["questions"] = {}
        self.state.write_text(json.dumps(state))
        self.write_lines(self.claims, [{"id": self.ASK + ":1", "by": "/w/hourly", "ts": self.AT}])
        guarded = self.hook()
        self.assertIn("claim 中", guarded.stdout)
        self.claims.write_text("")
        self.write_lines(self.answers, [{"id": self.ASK, "ts": self.AT, "how": "send", "note": "公開する"}])
        guarded = self.hook()
        self.assertIn("回答済み", guarded.stdout)
        self.assertIn(self.ASK, guarded.stdout)
        self.answers.write_text("")
        self.assertEqual(self.hook().stdout, "")

    def test_guard_matches_a_reworded_question_by_its_cover_and_allows_an_unrelated_reused_header(self) -> None:
        self.ask()
        state = json.loads(self.state.read_text())
        state["questions"]["lead-question"] = {
            "row": "/w/worker", "decision": "publish-copy", "covers": [self.KEY],
            "question": "公開の準備ができました。進めますか？", "header": "実施", "answer": None,
            "asked_in": "/w/lead", "session": "lead-session", "asked_at": self.AT}
        self.state.write_text(json.dumps(state))
        self.question = {**self.question, "question": "準備済みです。進めますか？", "header": "実施"}
        guarded = self.hook()
        self.assertIn("/w/hourly", guarded.stdout)
        self.assertIn(self.KEY, guarded.stdout)
        state["questions"] = {}
        self.state.write_text(json.dumps(state))
        self.write_lines(self.answers, [{"id": self.ASK, "ts": self.AT, "how": "send", "note": "公開する"}])
        self.question = {**self.question, "question": "別の公開を進めますか？", "header": "公開"}
        self.assertEqual(self.hook().stdout, "")

    def test_guard_allows_different_numbered_questions_from_the_same_worker(self) -> None:
        self.write_lines(self.inbox, [{"id": self.ASK, "kind": "ask", "questions": [
            self.question, {**self.question, "question": "次の公開を進めますか？", "header": "次の公開"}]}])
        self.ask(key=f"ask:worker::{self.ASK}:1", covers=[f"answer:worker::{self.ASK}:1"])
        state = json.loads(self.state.read_text())
        state["questions"]["lead-question"] = {
            "row": "/w/worker", "decision": f"ask:worker::{self.ASK}:2", "covers": [f"answer:worker::{self.ASK}:2"],
            "question": "次の公開を進めますか？", "header": "次の公開", "answer": None,
            "asked_in": "/w/lead", "session": "lead-session", "asked_at": self.AT}
        self.state.write_text(json.dumps(state))
        self.question = {**self.question, "question": "次の公開を進めますか？", "header": "次の公開"}
        self.assertEqual(self.hook().stdout, "")

    def test_current_binding_does_not_inherit_an_unrelated_answer_with_identical_wording(self) -> None:
        self.ask(key="remove:/w/old", covers=[])
        state = json.loads(self.state.read_text())
        old = next(iter(state["questions"].values()))
        old.update(answer="残す", answered_at=self.AT)
        state["questions"]["lead-current"] = {
            **old, "row": "/w/new", "decision": "remove:/w/new", "answer": None,
            "asked_in": "/w/lead", "session": "lead-session"}
        self.state.write_text(json.dumps(state))
        self.assertEqual(self.hook().stdout, "")

    def test_numbered_claim_takeover_overrides_another_workspaces_whole_ask_claim(self) -> None:
        self.write_lines(self.claims, [{"id": self.ASK, "by": "/w/hourly", "ts": self.AT},
                                       {"id": self.ASK + ":1", "by": "/w/lead", "ts": self.AT}])
        self.assertEqual(self.hook().stdout, "")


if __name__ == "__main__":
    unittest.main()
