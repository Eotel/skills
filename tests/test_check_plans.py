import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "agentic-docs" / "scripts" / "check_plans.py"


def run_check(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class CheckPlansTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_plans_kept_in_completed(self) -> None:
        write(self.root, "docs/exec-plans/completed/2026-01-01-done.md", "# Done\n")

        result = run_check(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("completed/2026-01-01-done.md", result.stdout)

    def test_rejects_active_plan_over_line_limit(self) -> None:
        write(self.root, "docs/exec-plans/active/long.md", "line\n" * 11)
        write(self.root, "docs/exec-plans/active/short.md", "line\n" * 10)

        result = run_check(self.root, "--max-lines", "10")

        self.assertEqual(result.returncode, 1)
        self.assertIn("active/long.md", result.stdout)
        self.assertNotIn("active/short.md", result.stdout)

    def test_rejects_history_headings_in_active_plans(self) -> None:
        write(
            self.root,
            "docs/exec-plans/active/plan.md",
            "# Plan\n\n## Progress\n\n### Outcomes And Retrospective\n\n"
            "## Surprises and Discoveries\n\n## Decision Log\n\n"
            "Progress is tracked elsewhere.\n",
        )

        result = run_check(self.root)

        self.assertEqual(result.returncode, 1)
        for heading in ("Progress", "Outcomes And Retrospective", "Surprises and Discoveries", "Decision Log"):
            self.assertIn(f"'{heading}'", result.stdout)
        self.assertEqual(result.stdout.count("plan.md"), 4)

    def test_rejects_nested_completed_and_active_plans(self) -> None:
        write(self.root, "docs/exec-plans/completed/2026/done.md", "# Done\n")
        write(self.root, "docs/exec-plans/active/team/plan.md", "# Plan\n\n## Progress\n")

        result = run_check(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("completed/2026/done.md", result.stdout)
        self.assertIn("active/team/plan.md", result.stdout)

    def test_default_limit_allows_at_most_150_lines(self) -> None:
        write(self.root, "docs/exec-plans/active/at-limit.md", "line\n" * 150)
        write(self.root, "docs/exec-plans/active/over-limit.md", "line\n" * 151)

        result = run_check(self.root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("over-limit.md", result.stdout)
        self.assertNotIn("at-limit.md", result.stdout)

    def test_rejects_indented_and_japanese_history_headings(self) -> None:
        write(
            self.root,
            "docs/exec-plans/active/plan.md",
            "# Plan\n\n   ## Progress\n\n## 進捗\n\n## 作業ログ\n\n## 決定ログ\n\n## 振り返り\n",
        )

        result = run_check(self.root)

        self.assertEqual(result.returncode, 1)
        for heading in ("Progress", "進捗", "作業ログ", "決定ログ", "振り返り"):
            self.assertIn(f"'{heading}'", result.stdout)

    def test_ignores_history_words_outside_markdown_headings(self) -> None:
        write(
            self.root,
            "docs/exec-plans/active/plan.md",
            "# Plan\n\n##\nProgress\n\n```markdown\n## Progress\n```\n\n"
            "~~~\n## Decision Log\n~~~\n\n    ## Outcomes\n",
        )

        result = run_check(self.root)

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_accepts_forward_looking_active_plans(self) -> None:
        write(self.root, "docs/exec-plans/active/plan.md", "# Plan\n\n## Next\n\n- do it\n")

        result = run_check(self.root)

        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
