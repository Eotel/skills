"""cloud_launch.py against a fake `claude` that draws the real prompts on a pty."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "claude-cloud-dispatch" / "scripts" / "cloud_launch.py"

spec = importlib.util.spec_from_file_location("cloud_launch", SCRIPT)
cloud_launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_launch)

FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys, time, tty

    mode = os.environ["FAKE_CLAUDE_MODE"]
    log = os.environ["FAKE_CLAUDE_LOG"]
    seen = {"args": sys.argv[1:], "cwd": os.getcwd(), "keys": []}

    def record():
        with open(log, "w", encoding="utf-8") as handle:
            json.dump(seen, handle)

    def out(text):
        sys.stdout.write(text)
        sys.stdout.flush()

    record()
    if mode == "silent":
        time.sleep(30)
    if mode in ("trust", "trust-stuck"):
        tty.setraw(0)
        out("\\x1b[1mQuick safety check\\x1b[0m\\r\\n \\u276f No, exit\\r\\n   Yes, I trust this folder\\r\\n")
        seen["keys"].append(os.read(0, 3).decode("latin-1"))
        record()
        if mode == "trust":
            out("\\x1b[2A   No, exit\\r\\n \\u276f Yes, I trust this folder\\r\\n")
        else:
            out("\\x1b[2A \\u276f No, exit\\r\\n   Yes, I trust this folder\\r\\n")
        seen["keys"].append(os.read(0, 1).decode("latin-1"))
        record()
    out("Created cloud session: demo\\r\\nView: https://claude.ai/code/session_01ABCdef?from=cli&m=0\\r\\n")
    time.sleep(30)
    """
)


class CloudLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        self.claude = root / "claude"
        self.claude.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        self.log = root / "fake.json"
        self.brief = root / "brief.md"
        self.brief.write_text("issue #1 を直して PR を出す\n", encoding="utf-8")

    def launch(self, mode, **options):
        os.environ["FAKE_CLAUDE_MODE"] = mode
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_MODE", None)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_LOG", None)
        options.setdefault("timeout", 20.0)
        return cloud_launch.launch(str(self.checkout), self.brief.read_text(encoding="utf-8"),
                                   claude=str(self.claude), **options)

    def seen(self):
        return json.loads(self.log.read_text(encoding="utf-8"))

    def test_returns_the_session_of_a_trusted_checkout(self):
        result = self.launch("plain")

        self.assertEqual(result, {"session_id": "session_01ABCdef",
                                  "url": "https://claude.ai/code/session_01ABCdef"})
        seen = self.seen()
        self.assertEqual(seen["args"], ["--cloud", "issue #1 を直して PR を出す\n"])
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(self.checkout))

    def test_leaves_the_trust_prompt_unanswered_without_the_flag(self):
        with self.assertRaises(cloud_launch.LaunchError) as raised:
            self.launch("trust")

        self.assertIn("--trust-folder", str(raised.exception))
        self.assertEqual(self.seen()["keys"], [])

    def test_answers_the_trust_prompt_with_the_flag(self):
        result = self.launch("trust", trust_folder=True)

        self.assertEqual(result["session_id"], "session_01ABCdef")
        self.assertEqual(self.seen()["keys"], ["\x1b[B", "\r"])

    def test_does_not_confirm_when_yes_is_not_selected(self):
        with self.assertRaises(cloud_launch.LaunchError) as raised:
            self.launch("trust-stuck", trust_folder=True)

        self.assertIn("did not show Yes", str(raised.exception))
        self.assertEqual(self.seen()["keys"], ["\x1b[B"])

    def test_gives_up_when_no_session_url_appears(self):
        with self.assertRaises(cloud_launch.LaunchError) as raised:
            self.launch("silent", timeout=1.5)

        self.assertIn("no session URL", str(raised.exception))

    def test_reports_a_missing_claude(self):
        os.environ["FAKE_CLAUDE_MODE"] = "plain"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_MODE", None)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_LOG", None)

        with self.assertRaises(cloud_launch.LaunchError) as raised:
            cloud_launch.launch(str(self.checkout), "task", claude=str(self.claude) + "-missing", timeout=5.0)

        self.assertIn("cannot start", raised.exception.output)

    def test_main_prints_json_and_refuses_an_empty_brief(self):
        os.environ["FAKE_CLAUDE_MODE"] = "plain"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_MODE", None)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_LOG", None)
        arguments = ["--checkout", str(self.checkout), "--brief", str(self.brief), "--claude", str(self.claude)]

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cloud_launch.main([*arguments, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["session_id"], "session_01ABCdef")

        self.brief.write_text("\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = cloud_launch.main(arguments)
        self.assertEqual(code, 2)
        self.assertIn("empty", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
