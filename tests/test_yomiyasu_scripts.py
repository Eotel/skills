"""Offline CLI checks for flag replacements and linked worktree word lists."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "yomiyasu" / "scripts" / "yomiyasu_lint.py"


class YomiyasuScriptsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        self.env = os.environ.copy()
        local_variables = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"], capture_output=True,
            text=True, check=True,
        ).stdout.splitlines()
        for name in local_variables:
            self.env.pop(name, None)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = str(self.root / "no-global-config")
        self.git("init", "--quiet")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "--allow-empty", "--no-gpg-sign", "-m", "Initial fixture")

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.repo,
            env=self.env, capture_output=True, text=True, check=True,
        )

    def lint(self, text: str, cwd: Path) -> list[dict]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"], input=text, cwd=cwd,
            env=self.env, capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [finding for finding in json.loads(result.stdout)["findings"]
                if finding["rule"] == "project_term"]

    def test_flag_replacements_preserve_plain_entries(self) -> None:
        words = self.repo / ".yomiyasu-words"
        words.mkdir()
        (words / "flag.txt").write_text("指紋\tfingerprint\n承認\n", encoding="utf-8")

        findings = self.lint("指紋を照合します。\n承認を待ちます。\nfingerprint を照合します。\n", self.repo)

        self.assertEqual([finding["line"] for finding in findings], [1, 2])
        self.assertIn("語の一覧（flag.txt）の語（指紋）の候補です", findings[0]["message"])
        self.assertIn("指紋 → fingerprint", findings[0]["message"])
        self.assertNotIn("使う語", findings[1]["message"])

    def test_check_shows_replacement_for_kept_project_term(self) -> None:
        words = self.repo / ".yomiyasu-words"
        words.mkdir()
        (words / "flag.txt").write_text("指紋\tfingerprint\n", encoding="utf-8")
        source, final = self.repo / "source.md", self.repo / "final.md"
        for document in (source, final):
            document.write_text("指紋を照合します。\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(SCRIPT.with_name("yomiyasu_check.py")), "run",
             "--source", str(source), "--final", str(final), "--state-dir", str(self.root / "state")],
            cwd=self.repo, env=self.env, capture_output=True, text=True, encoding="utf-8", check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- 指紋（語の一覧。L1。使う語: fingerprint）", result.stdout)

    def test_linked_worktree_uses_original_checkout_words(self) -> None:
        words = self.repo / ".yomiyasu-words"
        words.mkdir()
        (words / "flag.txt").write_text("指紋\n", encoding="utf-8")
        worktree = self.root / "linked-worktree"
        self.git("worktree", "add", "--quiet", "--detach", str(worktree))
        nested = worktree / "docs"
        nested.mkdir()

        findings = self.lint("指紋を照合します。\n", nested)

        self.assertEqual(len(findings), 1)
        self.assertIn("語の一覧（flag.txt）の語（指紋）の候補です", findings[0]["message"])


if __name__ == "__main__":
    unittest.main()
