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
  repos/*) echo "${FAKE_GH_PARENT:-}" ;;
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
        git_environment = subprocess.check_output(
            ["git", "rev-parse", "--local-env-vars"], text=True
        ).splitlines()
        environment = patch.dict(
            os.environ,
            {
                key: value
                for key, value in os.environ.items()
                if key not in git_environment
            } | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
            clear=True,
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

    def commit_files(self, files: dict[str, str]) -> str:
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "--", *files], cwd=self.repo, check=True)
        subprocess.run(
            [
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                "commit", "-qm", "manifest fixture",
            ],
            cwd=self.repo,
            check=True,
        )
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
        ).strip()

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

        # A fork of an outside project sends its PR to the parent.
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/Eotel/oss"],
            cwd=self.repo,
            check=True,
        )
        with patch.dict(os.environ, {"FAKE_GH_PARENT": "someone/oss"}):
            status, out, _ = self.run_main("pick")
        self.assertEqual((status, json.loads(out)["kind"]), (0, "show-me"))

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
            "### 概要\n\n<!-- ここに書く -->\n\n### 検証\n", encoding="utf-8"
        )
        self.body.write_text(REVIEW_FIRST_BODY, encoding="utf-8")
        status, out, _ = self.run_main("check", str(self.body))
        self.assertEqual(status, 1)
        self.assertEqual(
            out.splitlines(), ["missing heading: ### 概要", "missing heading: ### 検証"]
        )

        self.body.write_text("", encoding="utf-8")
        self.assertEqual(self.run_main("check", str(self.body))[0], 1)
        self.body.write_text(
            "### 概要\n\n#### 詳細\n\n書いた。\n\n### 検証\n\n通った。\n",
            encoding="utf-8",
        )
        self.assertEqual(self.run_main("check", str(self.body))[0], 0)

    def test_a_checkout_without_a_github_origin_is_undecided(self) -> None:
        self.clone_of("https://gitlab.com/someone/project.git")
        status, _, err = self.run_main("pick")
        self.assertEqual(status, 2)
        self.assertIn("not a GitHub repository", err)

    def test_check_requires_added_npm_packages_but_not_versions_or_removals(self) -> None:
        base = self.commit_files({
            "web/package.json": json.dumps({"dependencies": {"old": "1", "removed": "1"}}),
        })
        self.commit_files({
            "web/package.json": json.dumps({
                "dependencies": {"old": "2", "@scope/runtime": "1"},
                "devDependencies": {"dev-tool": "1"},
                "peerDependencies": {"peer-lib": "1"},
                "optionalDependencies": {"optional-lib": "1"},
            }),
        })
        self.body.write_text(REVIEW_FIRST_BODY, encoding="utf-8")
        argv = ("check", str(self.body), "--kind", "review-first", "--base", base)
        status, out, _ = self.run_main(*argv)
        self.assertEqual(status, 1)
        expected = {"@scope/runtime", "dev-tool", "peer-lib", "optional-lib"}
        self.assertEqual({line.rsplit(": ", 1)[1] for line in out.splitlines()}, expected)
        self.body.write_text(
            REVIEW_FIRST_BODY + "\n\n" + " ".join(expected),
            encoding="utf-8",
        )
        self.assertEqual(self.run_main(*argv)[0], 0)

    def test_check_reads_cargo_dependency_tables_and_declared_features(self) -> None:
        base = self.commit_files({"crates/app/Cargo.toml": '''
[dependencies]
serde = { version = "1", features = ["derive"] }
serde_json = "1"
[features]
old_feature = []
'''})
        self.commit_files({"crates/app/Cargo.toml": '''
[dependencies]
serde = { version = "2", features = ["derive"] }
serde_json = { version = "1", features = ["preserve_order"] }
[dev-dependencies.test_alias]
package = "test-crate"
version = "1"
[build-dependencies]
build-crate = "1"
[target.'cfg(unix)'.dependencies]
unix-crate = "1"
[target.'cfg(windows)'.dev-dependencies]
windows-test = "1"
[target.'cfg(unix)'.build-dependencies]
unix-build = "1"
[workspace.dependencies]
workspace-crate = "1"
[features]
new_feature = []
'''})
        self.body.write_text(REVIEW_FIRST_BODY, encoding="utf-8")
        argv = ("check", str(self.body), "--kind", "review-first")
        self.assertEqual(self.run_main(*argv)[0], 0)
        status, out, _ = self.run_main(*argv, "--base", base)
        self.assertEqual(status, 1)
        expected = {
            "test-crate", "build-crate", "unix-crate", "windows-test", "unix-build",
            "workspace-crate", "new_feature", "preserve_order",
        }
        self.assertEqual({line.rsplit(": ", 1)[1] for line in out.splitlines()}, expected)
        self.body.write_text(REVIEW_FIRST_BODY + "\n\n" + " ".join(expected), encoding="utf-8")
        self.assertEqual(self.run_main(*argv, "--base", base)[0], 0)

    def test_check_matches_whole_names_ignoring_case(self) -> None:
        base = self.commit_files({"Cargo.toml": ""})
        self.commit_files({"Cargo.toml": '[dependencies]\nlog = "1"\nrand = "1"\n'})
        argv = ("check", str(self.body), "--kind", "review-first", "--base", base)
        self.body.write_text(
            REVIEW_FIRST_BODY + "\n\nlogin random clog brand log1 rand2 1log 2rand\n"
            "log_name rand_name name_log name_rand\n"
            "log-name rand-name name-log name-rand\n"
            "log.name rand.name name.log name.rand",
            encoding="utf-8",
        )
        status, out, _ = self.run_main(*argv)
        self.assertEqual(status, 1)
        self.assertEqual({line.rsplit(": ", 1)[1] for line in out.splitlines()}, {"log", "rand"})
        self.body.write_text(REVIEW_FIRST_BODY + "\n\n`LOG` と (RaNd)。", encoding="utf-8")
        self.assertEqual(self.run_main(*argv)[0], 0)

    def test_check_reads_python_dependencies_extras_and_normalized_names(self) -> None:
        base = self.commit_files({"pyproject.toml": '''
[project]
dependencies = ["PyYAML>=6", "Foo._Bar>=1", "requests", "removed==1"]
[tool.poetry.dependencies]
existing = "1"
'''})
        self.commit_files({"pyproject.toml": '''
[project]
dependencies = ["pyyaml>=7", "foo-bar>=2", "requests[socks]>=2", "new-package>=1; python_version >= '3.12'"]
[project.optional-dependencies]
gpu = ["requests", "test_package~=2"]
[dependency-groups]
dev = ["dev.package @ https://example.invalid/dev.whl", { include-group = "test" }]
test = []
[tool.poetry.dependencies]
python = "^3.12"
existing = "2"
httpx = "^0.28"
[tool.poetry.group.dev.dependencies]
pytest = { version = "^8" }
'''})
        self.body.write_text(REVIEW_FIRST_BODY, encoding="utf-8")
        argv = ("check", str(self.body), "--kind", "review-first", "--base", base)
        status, out, _ = self.run_main(*argv)
        self.assertEqual(status, 1)
        expected = {"new-package", "test-package", "dev-package", "socks", "gpu", "httpx", "pytest"}
        self.assertEqual({line.rsplit(": ", 1)[1] for line in out.splitlines()}, expected)
        self.body.write_text(REVIEW_FIRST_BODY + "\n\n" + " ".join(expected), encoding="utf-8")
        self.assertEqual(self.run_main(*argv)[0], 0)

    def test_check_uses_the_merge_base_and_committed_head(self) -> None:
        base = self.commit_files({"package.json": "{}"})
        head = self.commit_files({"package.json": '{"dependencies": {"new-package": "1"}}'})
        subprocess.run(["git", "checkout", "-q", "--detach", base], cwd=self.repo, check=True)
        self.commit_files({
            "package.json": '{"dependencies": {"new-package": "2"}}',
            "base-only/package.json": '{"dependencies": {"base-only": "1"}}',
        })
        subprocess.run(["git", "branch", "base"], cwd=self.repo, check=True)
        subprocess.run(["git", "checkout", "-q", "--detach", head], cwd=self.repo, check=True)
        # Uncommitted changes must not replace HEAD's manifest in the comparison.
        (self.repo / "package.json").write_text("{}", encoding="utf-8")
        self.body.write_text(REVIEW_FIRST_BODY, encoding="utf-8")
        status, out, _ = self.run_main(
            "check", str(self.body), "--kind", "review-first", "--base", "base"
        )
        self.assertEqual(status, 1)
        self.assertIn("new-package", out)
        self.assertNotIn("base-only", out)


if __name__ == "__main__":
    unittest.main()
