"""Offline behavior tests for the lead's decision log read by hourly triage runs."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "orca-session-triage" / "scripts" / "lead_decisions.py"


class LeadDecisionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "lead-decisions.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--decisions-file", str(self.log), *args],
                              capture_output=True, text=True)

    def record(self, decision: str, answer: str) -> dict[str, str]:
        answer_file = self.root / "answer.txt"
        answer_file.write_text(answer, encoding="utf-8")
        result = self.run_tool("record", "--decision", decision, "--answer-file", str(answer_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_unanswered_lists_only_the_questions_the_lead_has_not_answered(self) -> None:
        self.record("remove:macbook-pro-16::/Users/eotel/orca/workspaces/x/fix", "消す")

        result = self.run_tool("unanswered", "--decision", "remove:macbook-pro-16::/Users/eotel/orca/workspaces/x/fix",
                               "--decision", "keep-uncommitted:/Users/eotel/orca/workspaces/y/z")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["keep-uncommitted:/Users/eotel/orca/workspaces/y/z"])

    def test_a_later_answer_wins_and_earlier_lines_stay_as_written(self) -> None:
        self.record("merge:Eotel/skills#80", "待つ")
        first_line = self.log.read_text(encoding="utf-8").splitlines()[0]
        self.record("merge:Eotel/skills#80", "merge する")

        result = self.run_tool("show")

        self.assertEqual(result.returncode, 0, result.stderr)
        shown = json.loads(result.stdout)
        self.assertEqual([(item["decision"], item["answer"]) for item in shown],
                         [("merge:Eotel/skills#80", "merge する")])
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines()[0], first_line)

    def test_an_empty_answer_is_refused_and_nothing_is_written(self) -> None:
        answer_file = self.root / "empty.txt"
        answer_file.write_text("  \n", encoding="utf-8")

        result = self.run_tool("record", "--decision", "merge:Eotel/skills#80", "--answer-file", str(answer_file))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_a_damaged_line_is_reported_instead_of_hiding_an_answer(self) -> None:
        self.record("merge:Eotel/skills#80", "merge する")
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write("{not json\n")

        result = self.run_tool("unanswered", "--decision", "merge:Eotel/skills#80")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(":2", result.stderr)


if __name__ == "__main__":
    unittest.main()
