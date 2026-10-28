"""Offline behavior tests for the lead's decision log read by hourly triage runs."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "orca-session-triage" / "scripts" / "lead_decisions.py"
# gh is outside this repository: answer each GraphQL query from a fixture of {"owner/name": {number: pr}} and
# log the repository it asked for; a repository missing from the fixture fails as gh does for one it cannot read.
FAKE_GH = """#!{python}
import json, os, re, sys
query = next(arg[len("query="):] for arg in sys.argv if arg.startswith("query="))
slug = "/".join(re.search(r'repository\\(owner: "([^"]+)", name: "([^"]+)"\\)', query).groups())
open(os.environ["FAKE_GH_LOG"], "a").write(slug + "\\n")
prs = json.load(open(os.environ["FAKE_GH_PRS"])).get(slug)
if prs is None:
    sys.exit("GraphQL: Could not resolve to a Repository with the name '" + slug + "'.")
print(json.dumps({{"data": {{"repository": {{
    "pr_" + number: prs.get(number) for number in re.findall(r"pr_(\\d+):", query)}}}}}}))
"""


class LeadDecisionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "lead-decisions.jsonl"
        self.handed_off = self.root / "handed-off.jsonl"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
        self.gh_log = self.root / "gh.log"
        self.gh_prs = self.root / "prs.json"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_GH_LOG": str(self.gh_log), "FAKE_GH_PRS": str(self.gh_prs)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_tool(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--decisions-file", str(self.log), *args],
                              capture_output=True, text=True, input=stdin, env=self.env)

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


    def test_unanswered_without_keys_lists_every_handed_off_key_with_its_prs(self) -> None:
        self.handed_off.write_text("".join(json.dumps(row) + "\n" for row in [
            {"decision": "merge:Eotel/orca-board#12+#14", "handed_off_at": "2026-10-27T01:00:00+00:00"},
            {"decision": "remove:mac::/w/a", "handed_off_at": "2026-10-27T01:00:00+00:00"},
            {"decision": "spawn:acme/web#6303::review-fix", "handed_off_at": "2026-10-27T02:00:00+00:00"},
            {"decision": "merge:Eotel/orca-board#12+#14", "handed_off_at": "2026-10-28T01:00:00+00:00"},
            {"decision": "keep-uncommitted:/w/b", "handed_off_at": "2026-10-28T01:00:00+00:00"}]))
        self.record("remove:mac::/w/a", "消す")
        self.gh_prs.write_text(json.dumps({"Eotel/orca-board": {
            "12": {"number": 12, "state": "MERGED", "updatedAt": "2026-10-28T02:00:00Z"},
            "14": {"number": 14, "state": "OPEN", "updatedAt": "2026-10-27T23:00:00Z"}}}))

        result = self.run_tool("unanswered", "--handed-off-file", str(self.handed_off))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            {"decision": "merge:Eotel/orca-board#12+#14", "handed_off_at": "2026-10-28T01:00:00+00:00", "prs": [
                {"repo": "Eotel/orca-board", "number": 12, "state": "MERGED", "updatedAt": "2026-10-28T02:00:00Z",
                 "updated_after_handoff": True},
                {"repo": "Eotel/orca-board", "number": 14, "state": "OPEN", "updatedAt": "2026-10-27T23:00:00Z",
                 "updated_after_handoff": False}]},
            {"decision": "spawn:acme/web#6303::review-fix", "handed_off_at": "2026-10-27T02:00:00+00:00",
             "prs": [{"repo": "acme/web", "number": 6303, "state": None, "updatedAt": None,
                      "updated_after_handoff": None}]},
            {"decision": "keep-uncommitted:/w/b", "handed_off_at": "2026-10-28T01:00:00+00:00", "prs": []}])
        self.assertIn("acme/web", result.stderr)
        self.assertEqual(sorted(self.gh_log.read_text().split()), ["Eotel/orca-board", "acme/web"])

    def test_record_many_checks_every_line_before_writing_any(self) -> None:
        lines = [{"decision": "merge:Eotel/skills#80", "answer": "merge する"},
                 {"decision": "remove:mac::/w/a", "answer": "消す", "note": "?"},
                 {"decision": "merge:Eotel/skills#81", "answer": "待つ", "decided_at": "2026-10-27T10:00:00"},
                 {"decision": " ", "answer": "空の鍵"}]
        answers = self.root / "answers.jsonl"
        answers.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")

        refused = self.run_tool("record-many", "--file", str(answers))

        self.assertEqual(refused.returncode, 1)
        self.assertEqual(sorted({int(number) for number in re.findall(r"line (\d+)", refused.stderr)}), [2, 3, 4])
        self.assertFalse(self.log.exists())

        recorded = self.run_tool("record-many", stdin=json.dumps(lines[0], ensure_ascii=False) + "\n"
                                 + json.dumps({**lines[2], "decided_at": "2026-10-27T10:00:00+09:00"}) + "\n")

        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual([item["decision"] for item in json.loads(recorded.stdout)],
                         ["merge:Eotel/skills#80", "merge:Eotel/skills#81"])
        shown = {item["decision"]: item for item in json.loads(self.run_tool("show").stdout)}
        self.assertEqual(shown["merge:Eotel/skills#80"]["answer"], "merge する")
        self.assertEqual(shown["merge:Eotel/skills#81"]["decided_at"], "2026-10-27T10:00:00+09:00")


if __name__ == "__main__":
    unittest.main()
