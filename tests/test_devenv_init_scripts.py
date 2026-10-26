"""Offline checks for devenv-init scaffolding, with `nix flake init` mocked."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "devenv-init" / "scripts" / "scaffold.sh"

TEMPLATE_DEVENV_NIX = """{ pkgs, ... }:
{
  languages.python = {
    enable = true;
    package = pkgs.python312;
  };
}
"""

FAKE_NIX = """#!/bin/sh
[ "$1 $2" = "flake init" ] || exit 1
cp "$TEMPLATE_DIR/devenv.nix" devenv.nix
"""


class DevenvInitScriptsTests(unittest.TestCase):
    def run_scaffold(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            nix = bin_dir / "nix"
            nix.write_text(FAKE_NIX)
            nix.chmod(0o755)
            template = tmp / "template"
            template.mkdir()
            (template / "devenv.nix").write_text(TEMPLATE_DEVENV_NIX)
            project = tmp / "my-project"
            project.mkdir()
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "TEMPLATE_DIR": str(template),
            }
            result = subprocess.run(
                ["bash", str(SCRIPT), *args], cwd=project, env=env,
                capture_output=True, text=True, check=False,
            )
            devenv_nix = project / "devenv.nix"
            return result, devenv_nix.read_text() if devenv_nix.exists() else None

    def test_python_option_selects_the_nixpkgs_interpreter(self):
        result, devenv_nix = self.run_scaffold("--lang", "python", "--python", "3.13")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("package = pkgs.python313;", devenv_nix)
        self.assertNotIn("python312", devenv_nix)

    def test_rejects_python_missing_from_nixpkgs(self):
        result, devenv_nix = self.run_scaffold("--lang", "python", "--python", "3.10")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--python must be one of", result.stderr)
        self.assertIsNone(devenv_nix)


if __name__ == "__main__":
    unittest.main()
