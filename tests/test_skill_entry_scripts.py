"""Skill scripts run by their path even where the first python3 on PATH is macOS's 3.9."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# yomiyasu/scripts is an imported upstream snapshot (README.md); it keeps its own shebang.
SNAPSHOTS = {"yomiyasu"}


def entry_scripts() -> list[Path]:
    return sorted(path for path in ROOT.glob("*/scripts/*.py")
                  if path.parts[-3] not in SNAPSHOTS and '__name__ == "__main__"' in path.read_text())


def python39() -> str | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    found = subprocess.run([uv, "python", "find", "--no-project", "3.9"], capture_output=True, text=True,
                           timeout=60, check=False)
    return found.stdout.strip() if found.returncode == 0 else None


class EntryScriptTests(unittest.TestCase):
    def test_help_exits_zero_with_python39_first_on_path(self) -> None:
        old = python39()
        if old is None:
            self.skipTest("no Python 3.9 (uv python install 3.9)")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "python3").symlink_to(old)
            env = {**os.environ, "PATH": os.pathsep.join((tmp, os.environ.get("PATH", "")))}
            self.assertTrue(subprocess.run(["python3", "--version"], env=env, capture_output=True,
                                           text=True, check=False).stdout.startswith("Python 3.9"))
            for script in entry_scripts():
                with self.subTest(script=str(script.relative_to(ROOT))):
                    result = subprocess.run([script, "--help"], env=env, stdin=subprocess.DEVNULL,
                                            capture_output=True, text=True, timeout=60, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
