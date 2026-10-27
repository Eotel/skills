"""Offline tests for pr/scripts/pr_template.py: real git checkouts, a fake gh on PATH."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "pr" / "scripts" / "pr_template.py"
spec = importlib.util.spec_from_file_location("pr_template", SCRIPT)
assert spec is not None and spec.loader is not None
pr_template = importlib.util.module_from_spec(spec)
# dataclasses resolve string annotations through sys.modules.
sys.modules[spec.name] = pr_template
spec.loader.exec_module(pr_template)

# gh in this setup can print debug lines to stdout; the fake does too.
FAKE_GH = """#!/bin/sh
echo '* Request at 2026-10-27 15:46:00 +0900 JST'
case "$2" in
  user) echo Eotel ;;
  user/orgs) echo acme ;;
esac
"""

REVIEW_FIRST_BODY = "\n\n".join(
    f"## {title}\n\n1. 書いた。"
    for title in (
        "何が問題だったか",
        "この PR で変わること",
        "reviewer に見てほしいこと",
        "確かめたこと・確かめていないこと",
        "戻すときの注意",
    )
)
SHOW_ME_BODY = "## Summary\n\n```text\nsave → retry\n```\n\n## Evidence\n\n- 30 passed\n\n## Merge Danger\n\n**Door:** one-way\n"


class PrTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
        environment = patch.dict(
            os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.body = root / "body.md"

    def clone_of(self, url: str) -> None:
        subprocess.run(
            ["git", "remote", "add", "origin", url], cwd=self.repo, check=True
        )

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = pr_template.main([*argv, "--repo-dir", str(self.repo)])
        return status, out.getvalue(), err.getvalue()

    def test_owned_and_outside_repositories_get_different_templates(self) -> None:
        self.clone_of("git@github.com:acme/api.git")
        status, out, _ = self.run_main("pick")
        self.assertEqual((status, json.loads(out)["kind"]), (0, "review-first"))

        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/someone/oss"],
            cwd=self.repo,
            check=True,
        )
        status, out, _ = self.run_main("pick")
        self.assertEqual(
            (status, json.loads(out)["headings"]),
            (0, ["Summary", "Evidence", "Merge Danger"]),
        )

    def test_check_rejects_the_upstream_shape_in_an_owned_repository(self) -> None:
        self.clone_of("https://github.com/Eotel/skills.git")
        self.body.write_text(SHOW_ME_BODY, encoding="utf-8")
        status, out, _ = self.run_main("check", str(self.body))
        self.assertEqual(status, 1)
        self.assertIn("missing heading: ## 何が問題だったか", out)

        # Bodies edited on GitHub keep CRLF when piped in.
        with patch("sys.stdin", io.StringIO(REVIEW_FIRST_BODY.replace("\n", "\r\n"))):
            self.assertEqual(self.run_main("check", "-")[0], 0)

    def test_repository_template_outranks_the_builtin_ones(self) -> None:
        self.clone_of("https://github.com/Eotel/skills.git")
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text(
            "## 概要\n\n<!-- ここに書く -->\n\n## 検証\n", encoding="utf-8"
        )
        self.body.write_text(REVIEW_FIRST_BODY, encoding="utf-8")
        status, out, _ = self.run_main("check", str(self.body))
        self.assertEqual(status, 1)
        self.assertEqual(
            out.splitlines(), ["missing heading: ## 概要", "missing heading: ## 検証"]
        )

    def test_a_checkout_without_a_github_origin_is_undecided(self) -> None:
        self.clone_of("https://gitlab.com/someone/project.git")
        status, _, err = self.run_main("pick")
        self.assertEqual(status, 2)
        self.assertIn("not a GitHub repository", err)


if __name__ == "__main__":
    unittest.main()
