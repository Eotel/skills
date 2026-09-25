import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "exec-plan-migration"
    / "scripts"
    / "migrate_plans.py"
)
COMPLETED = "docs/exec-plans/completed/2026-01-01-done.md"
ACTIVE = "docs/exec-plans/active/2026-01-02-live.md"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class MigratePlansTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        write(self.root, COMPLETED, "# Done\n")
        write(self.root, ACTIVE, "# Live\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def commit(self, message: str = "commit") -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_reports_references_to_completed_plans_in_any_file(self) -> None:
        write(self.root, "docker/Dockerfile", f"# see {COMPLETED}\n")
        write(self.root, "AGENTS.md", "Read `2026-01-01-done.md` first.\n")
        write(self.root, "README.md", "No plan here.\n")
        self.commit()

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("docker/Dockerfile:1", result.stdout)
        self.assertIn("AGENTS.md:1", result.stdout)
        self.assertNotIn("README.md", result.stdout)

    def test_apply_refuses_while_completed_plans_are_referenced(self) -> None:
        write(self.root, "AGENTS.md", f"See {COMPLETED}.\n")
        self.commit()

        result = self.run_script("--apply")

        self.assertEqual(result.returncode, 1)
        self.assertIn("AGENTS.md:1", result.stdout)
        self.assertTrue((self.root / COMPLETED).exists())

    def test_apply_removes_unreferenced_completed_plans(self) -> None:
        self.commit()

        result = self.run_script("--apply")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / COMPLETED).exists())
        self.assertIn(f"D  {COMPLETED}", git(self.root, "status", "--short"))
        self.assertTrue((self.root / ACTIVE).exists())

    def test_lists_active_plans_with_last_commit_date_and_references(self) -> None:
        write(self.root, "web/app.py", "# plan: 2026-01-02-live.md\n")
        self.commit()
        date = git(self.root, "log", "-1", "--format=%cs").strip()

        result = self.run_script()

        section = result.stdout.split("## Active plans", 1)[1]
        self.assertIn(f"{ACTIVE} (last commit {date})", section)
        self.assertIn("web/app.py:1", section)

    def test_reports_unmerged_branches_that_touch_plans(self) -> None:
        self.commit()
        git(self.root, "switch", "-q", "-c", "feature")
        write(self.root, ACTIVE, "# Live\n\nMore.\n")
        self.commit("edit plan")
        git(self.root, "switch", "-q", "-c", "unrelated", "main")
        write(self.root, "README.md", "Other work.\n")
        self.commit("other")
        git(self.root, "switch", "-q", "main")

        result = self.run_script()

        section = result.stdout.split("## Unmerged branches touching plans", 1)[1]
        self.assertIn(f"{ACTIVE}: feature", section)
        self.assertNotIn("unrelated", section)

    def test_reports_repository_without_commits(self) -> None:
        git(self.root, "add", "-A")

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{ACTIVE} (last commit none)", result.stdout)
        self.assertIn("base HEAD does not resolve", result.stdout)


if __name__ == "__main__":
    unittest.main()
