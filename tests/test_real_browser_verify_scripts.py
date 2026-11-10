"""Offline tests for real-browser-verify/scripts/desktop_app.py: a fake app stands in for a desktop app."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "real-browser-verify" / "scripts" / "desktop_app.py"

# Stands in for an app with drive.dylib loaded: its page reports ready, then it waits to be stopped.
FAKE_APP = """#!/bin/sh
echo "loaded $$" >> "$DRIVE_LOG"
echo "ready $$" >> "$DRIVE_LOG"
exec sleep 30
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class DesktopAppTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        # Already built, so that launch does not need clang and AppKit.
        for name in ("noactivate.dylib", "drive.dylib", "send"):
            (self.tools / name).touch()

    def script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--tools", str(self.tools), *args], capture_output=True, text=True,
            check=False, timeout=30,
        )

    def app(self, body: str) -> Path:
        path = self.root / "app"
        path.write_text(body)
        path.chmod(0o755)
        return path

    def test_launch_waits_for_the_helpers_and_stop_ends_the_app(self) -> None:
        launched = self.script("launch", str(self.app(FAKE_APP)), "--timeout", "10")
        self.assertEqual(launched.returncode, 0, launched.stderr)
        pid = json.loads(launched.stdout)["pid"]
        self.addCleanup(lambda: alive(pid) and os.kill(pid, 9))
        self.assertTrue(alive(pid))

        stopped = self.script("stop", str(pid))

        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(pid))
        self.assertNotEqual(self.script("stop", str(pid)).returncode, 0)

    def test_launch_reports_an_app_that_exits_before_taking_commands(self) -> None:
        launched = self.script("launch", str(self.app("#!/bin/sh\necho 'no display'\nexit 3\n")), "--timeout", "10")

        self.assertEqual(launched.returncode, 1)
        self.assertIn("exited with 3", launched.stderr)
        self.assertIn("no display", launched.stderr)


if __name__ == "__main__":
    unittest.main()
