"""cloud_launch.py against a fake `claude` that behaves on a pty as the real one was seen to."""

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
assert spec is not None and spec.loader is not None
cloud_launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_launch)

DOWN, ENTER = "\x1b[B", "\r"
SESSION = {"session_id": "session_01ABCdef", "url": "https://claude.ai/code/session_01ABCdef"}

# The trust dialog follows claude 2.1.293 (2026-10-13): "No, exit" is selected first, the pointer and its label
# are separated by a cursor move, Enter within 150 ms of opening is dropped, and at 150 ms the dialog is drawn
# again with "No, exit" selected. Keys the fake had not read when it is told to stop are logged before it leaves,
# so a test sees every key the launcher sent.
FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, re, select, signal, sys, time, tty

    mode = os.environ["FAKE_CLAUDE_MODE"]
    log = os.environ["FAKE_CLAUDE_LOG"]

    def record(event, value):
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps([event, value]) + "\\n")

    def out(text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def read_keys():
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            keys = ""
            while select.select([0], [], [], 0)[0]:
                data = os.read(0, 64)
                if not data:
                    break
                keys += data.decode("latin-1")
            if keys:
                record("key", keys)
            return re.findall(r"\\x1b\\[B|\\r|.", keys, re.S)
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})

    def leave(*_):
        read_keys()
        os._exit(0)

    def trust_dialog():
        tty.setraw(0)
        opened = time.monotonic()
        state = {"focus": "no", "remounted": False}

        def draw(first=False):
            rows = (("no", "No, exit"), ("yes", "Yes, I trust this folder"))
            out(("" if first else "\\x1b[2A") + "".join(
                (" \\u276f" if state["focus"] == name else "") + "\\x1b[4G" + label + "\\x1b[K\\r\\n"
                for name, label in rows))

        out("\\x1b7\\x1b[r\\x1b8\\x1b[?25l\\x1b[1mQuick safety check\\x1b[0m: is this a project you trust?\\r\\n")
        draw(first=True)
        out("\\x1b[>0q")
        while True:
            left = None if state["remounted"] else max(0.0, opened + 0.15 - time.monotonic())
            if not select.select([0], [], [], left)[0]:
                state.update(focus="no", remounted=True)
                draw()
                continue
            for key in read_keys():
                if key == "\\x1b[B":
                    if mode != "trust-stuck":
                        state["focus"] = "yes"
                    draw()
                elif key == "\\r":
                    if time.monotonic() - opened < 0.15:
                        continue
                    if mode == "trust-reverts":
                        state["focus"] = "no"
                        draw()
                    elif state["focus"] == "yes":
                        out("\\x1b[3A\\x1b[J")
                        return
                    else:
                        sys.exit(1)

    signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "deaf" else leave)
    record("start", {"args": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid()})
    if mode == "silent":
        time.sleep(30)
    if mode == "exits":
        out("Error: not logged in\\r\\n")
        sys.exit(1)
    if mode.startswith("trust"):
        trust_dialog()
    if mode == "echo":
        out("Task: " + sys.argv[2].replace("\\n", "\\r\\n") + "\\r\\n")
    out("Created cloud session: demo\\r\\n")
    if mode == "split":
        out("View: https://claude.ai/code/session_01AB")
        time.sleep(0.2)
        out("Cdef?from=cli&m=0\\r\\n")
    else:
        out("View: https://claude.ai/code/session_01ABCdef?from=cli&m=0\\r\\n")
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
        self.log = root / "fake.jsonl"
        self.brief = root / "brief.md"
        self.brief.write_text("issue #1 を直して PR を出す\n", encoding="utf-8")

    def fake(self, mode):
        os.environ["FAKE_CLAUDE_MODE"] = mode
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_MODE", None)
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_LOG", None)

    def launch(self, mode, brief=None, **options):
        self.fake(mode)
        options.setdefault("timeout", 20.0)
        options.setdefault("claude", str(self.claude))
        return cloud_launch.launch(str(self.checkout), brief or self.brief.read_text(encoding="utf-8"), **options)

    def events(self, name):
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [value for event, value in map(json.loads, lines) if event == name]

    def keys(self):
        return "".join(self.events("key"))

    def assert_claude_is_gone(self):
        with self.assertRaises(ProcessLookupError):
            os.kill(self.events("start")[0]["pid"], 0)

    def main(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cloud_launch.main(["--checkout", str(self.checkout), "--brief", str(self.brief),
                                      "--claude", str(self.claude), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_returns_the_session_of_a_trusted_checkout(self):
        result = self.launch("plain")

        self.assertEqual(result, SESSION)
        started = self.events("start")[0]
        self.assertEqual(started["args"], ["--cloud", "issue #1 を直して PR を出す\n"])
        self.assertEqual(os.path.realpath(started["cwd"]), os.path.realpath(self.checkout))
        self.assert_claude_is_gone()

    def test_leaves_the_trust_prompt_unanswered_without_the_flag(self):
        with self.assertRaises(cloud_launch.LaunchError) as raised:
            self.launch("trust")

        self.assertIn("--trust-folder", str(raised.exception))
        self.assertEqual(self.keys(), "")

    def test_trusts_the_checkout_with_the_flag(self):
        result = self.launch("trust", trust_folder=True)

        self.assertEqual(result, SESSION)
        self.assertEqual(self.keys(), DOWN + ENTER)

    def test_does_not_confirm_when_yes_is_not_selected(self):
        with self.assertRaises(cloud_launch.LaunchError) as raised:
            self.launch("trust-stuck", trust_folder=True)

        self.assertIn("did not show Yes", str(raised.exception))
        self.assertEqual(self.keys(), DOWN)

    def test_reports_a_trust_prompt_that_came_back_after_enter(self):
        with self.assertRaises(cloud_launch.LaunchError) as raised:
            self.launch("trust-reverts", trust_folder=True)

        self.assertIn("not trusted", str(raised.exception))

    def test_reports_that_no_url_came_within_the_timeout(self):
        with self.assertRaises(cloud_launch.LaunchError) as raised:
            self.launch("silent", timeout=1.0)

        self.assertIn("no session URL appeared in 1 s", str(raised.exception))
        self.assertIn("may still have been created", str(raised.exception))

    def test_reports_a_claude_that_exits_without_a_session(self):
        with self.assertRaises(cloud_launch.LaunchError) as raised:
            self.launch("exits", timeout=60.0)

        self.assertIn("claude exited", str(raised.exception))
        self.assertIn("not logged in", raised.exception.output)

    def test_reports_a_claude_that_cannot_start(self):
        for options in ({"claude": str(self.claude) + "-missing"}, {"brief": "task\0text"}):
            with self.subTest(options):
                with self.assertRaises(cloud_launch.LaunchError) as raised:
                    self.launch("plain", **options)

                self.assertIn("cannot start", raised.exception.output)

    def test_takes_a_url_that_arrives_in_pieces(self):
        self.assertEqual(self.launch("split"), SESSION)

    def test_skips_a_session_url_the_brief_quotes(self):
        brief = "https://claude.ai/code/session_01OLDone の続きをする\n"

        self.assertEqual(self.launch("echo", brief=brief), SESSION)

    def test_ends_a_claude_that_ignores_sigterm(self):
        self.assertEqual(self.launch("deaf"), SESSION)

        self.assert_claude_is_gone()

    def test_main_prints_the_url_or_json_and_keeps_the_output(self):
        self.fake("plain")
        output = Path(self.tmp.name) / "launch.log"

        code, stdout, _ = self.main("--log", str(output))
        self.assertEqual((code, stdout), (0, SESSION["url"] + "\n"))
        self.assertIn("Created cloud session", output.read_text(encoding="utf-8"))

        code, stdout, _ = self.main("--json")
        self.assertEqual((code, json.loads(stdout)), (0, SESSION))

    def test_main_refuses_a_brief_claude_cannot_take(self):
        self.fake("plain")
        for text, reason in (("\n", "empty"), ("- 一覧から始まる\n", "starts with '-'")):
            with self.subTest(text):
                self.brief.write_text(text, encoding="utf-8")

                code, _, stderr = self.main()

                self.assertEqual(code, 2)
                self.assertIn(reason, stderr)
                self.assertFalse(self.log.exists())

    def test_main_reports_the_failure_with_what_claude_showed(self):
        self.fake("trust")

        code, stdout, stderr = self.main()

        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("--trust-folder", stderr)
        self.assertIn("Quick safety check", stderr)

    def test_main_passes_the_trust_flag_and_the_timeout(self):
        self.fake("trust")
        code, stdout, _ = self.main("--trust-folder")
        self.assertEqual((code, stdout), (0, SESSION["url"] + "\n"))

        self.fake("silent")
        code, _, stderr = self.main("--timeout", "1")
        self.assertEqual(code, 1)
        self.assertIn("in 1 s", stderr)


if __name__ == "__main__":
    unittest.main()
