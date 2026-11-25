from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "natural-japanese/scripts/post_check.py"


class PostCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.cwd = root / "worktree"
        self.cwd.mkdir()
        scratchpad = root / "scratchpad"
        scratchpad.mkdir()
        self.original = scratchpad / "source.md"
        self.final = scratchpad / "final.md"
        self.final.write_bytes("定例会の資料を更新しました。".encode())
        self.env = {**os.environ, "HOME": str(root), "YOMIYASU_STATE_DIR": str(root / "state")}

    def check(self, *options: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--final", str(self.final), *options],
                              cwd=self.cwd, env=self.env, capture_output=True, text=True, check=False)

    def test_final_only_runs_both_checks_and_verifies_the_same_post(self) -> None:
        self.assertNotEqual(self.check("--verify-only").returncode, 0)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("見直し候補はありません", result.stdout)
        self.assertIn("via=verify", result.stdout)
        self.assertIn("mode=compose", result.stdout)
        self.assertEqual(self.check("--verify-only").returncode, 0)

        self.final.write_bytes(self.final.read_bytes() + b"\n")
        self.assertNotEqual(self.check("--verify-only").returncode, 0)

    def test_rewrite_accepts_original_and_final_outside_the_worktree(self) -> None:
        self.original.write_bytes("定例会の資料の修正を行いました。".encode())
        result = self.check("--source", str(self.original), "--doc-type", "pr")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("via=verify", result.stdout)
        self.assertIn("mode=rewrite", result.stdout)
        self.assertEqual(self.check("--verify-only").returncode, 0)
