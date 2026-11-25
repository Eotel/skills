from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "orca-session-triage/scripts/issue-schedule.py"


class IssueScheduleTests(unittest.TestCase):
    def run_schedule(self, body: str, *options: str) -> tuple[subprocess.CompletedProcess[str], dict | None]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gh = root / "gh"
            gh.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "if sys.argv[1:3] == ['issue', 'view']:\n"
                "    print(json.dumps({'body': os.environ['TEST_ISSUE_BODY']}))\n"
                "elif sys.argv[1:3] == ['issue', 'edit']:\n"
                "    Path(os.environ['TEST_ISSUE_EDIT']).write_text(\n"
                "        json.dumps({'args': sys.argv[3:], 'body': sys.stdin.read()}))\n"
                "else:\n"
                "    sys.exit(2)\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            edit = root / "edit.json"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "example/project", "17", *options],
                env={**os.environ, "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                     "TEST_ISSUE_BODY": body, "TEST_ISSUE_EDIT": str(edit)},
                capture_output=True, text=True, check=False,
            )
            return result, json.loads(edit.read_text(encoding="utf-8")) if edit.exists() else None

    def test_start_replaces_from_preserving_the_planned_date_and_adds_assignee(self) -> None:
        result, edit = self.run_schedule(
            "概要\r\n\r\n## 予定\r\n- 着手: From 未定 / To 2026-11-30 18:00（予定）\r\n"
            "- 予定工数: 0.1 人日（確認）\r\n\r\n## 手順\r\n本文\r\n",
            "--start", "2026-11-25",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(edit, {
            "args": ["17", "-R", "example/project", "--body-file", "-", "--add-assignee", "@me"],
            "body": "概要\r\n\r\n## 予定\r\n- 着手: From 2026-11-25 / To 2026-11-30 18:00（予定）\r\n"
                    "- 予定工数: 0.1 人日（確認）\r\n\r\n## 手順\r\n本文\r\n",
        })

    def test_done_updates_to_and_effort_without_using_a_heading_in_code(self) -> None:
        example = "```markdown\n## 予定\n- 着手: From / To\n- 予定工数:\n```\n\n"
        result, edit = self.run_schedule(
            example + "## Schedule\n- Start: From 2026-11-20 18:00 / To 未定 (planned)\n"
            "- Estimated effort: 0.1 person-days (review)\n",
            "--done", "2026-11-25", "--effort", "0.25",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(edit, {
            "args": ["17", "-R", "example/project", "--body-file", "-"],
            "body": example + "## Schedule\n- Start: From 2026-11-20 18:00 / To 2026-11-25 (planned)\n"
                    "- Estimated effort: 0.25 person-days (review)\n",
        })

    def test_appends_a_schedule_to_an_issue_without_one(self) -> None:
        result, edit = self.run_schedule("既存の本文。", "--effort", "0.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(edit, {
            "args": ["17", "-R", "example/project", "--body-file", "-"],
            "body": "既存の本文。\n\n## 予定\n- 着手: From / To\n- 予定工数: 0.5 人日\n",
        })

    def test_refuses_ambiguous_schedule_without_editing_the_issue(self) -> None:
        result, edit = self.run_schedule(
            "## 予定\n- 着手: From / To\n- 着手: From / To\n- 予定工数:\n", "--start",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("1行ずつ", result.stderr)
        self.assertIsNone(edit)
