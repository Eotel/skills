"""Offline tests for close_tabs.py: a tab the scan listed closes only while nothing moved after the scan."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "orca-session-triage" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("close_tabs", SCRIPTS / "close_tabs.py")
assert spec is not None and spec.loader is not None
close_tabs = importlib.util.module_from_spec(spec)
# dataclasses resolve string annotations through sys.modules.
sys.modules[spec.name] = close_tabs
spec.loader.exec_module(close_tabs)

SCANNED_AT = datetime(2026, 10, 28, 5, 1, tzinfo=timezone.utc)


class FakeTabs:
    """Orca as close_tabs.py sees it: agent state changes per worktree, and the tabs it was asked to close."""

    def __init__(self, updates: dict[tuple[str | None, str], list[datetime]] | None = None) -> None:
        self.updates = updates or {}
        self.closed: list[tuple[str | None, str]] = []

    def agent_changes(self, environment: str | None, path: str) -> list[datetime]:
        return self.updates.get((environment, path), [])

    def close(self, environment: str | None, handle: str) -> None:
        self.closed.append((environment, handle))


class CloseTabsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.worktree = self.dir / "workspaces" / "fix-login"
        self.projects = self.dir / "projects"
        self.transcript = close_tabs.scan_sessions.claude_project_dir(self.projects, str(self.worktree)) / "6a0027d4.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("{}\n")
        self.touch(self.transcript, SCANNED_AT - timedelta(minutes=30))
        self.inbox = self.dir / "triage-inbox.jsonl"
        self.inbox.write_text("")
        self.scan = self.dir / "scan.json"
        self.scan.write_text(json.dumps([{
            "path": str(self.worktree), "repo": "web", "environment": None,
            "scanned_at": SCANNED_AT.isoformat(), "class": "finished",
            "terminals": [{"handle": "term_agent", "agent": "claude", "title": "fix-login"}],
            "claude": {"transcript": str(self.transcript)}, "codex": [],
            "closable": ["term_agent"], "meaningful_tabs": [],
        }]))

    @staticmethod
    def touch(path: Path, at: datetime) -> None:
        os.utime(path, (at.timestamp(), at.timestamp()))

    def run_close(self, tabs: FakeTabs, *handles: str) -> tuple[int, str]:
        argv = ["--from", str(self.scan), "--claude-projects", str(self.projects), "--inbox", str(self.inbox),
                *handles]
        with redirect_stdout(io.StringIO()) as out:
            code = close_tabs.main(argv, tabs)
        return code, out.getvalue()

    def write_transcript(self, *entries: dict) -> None:
        self.transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    def test_a_tab_whose_transcript_moved_after_the_scan_stays_open(self) -> None:
        self.write_transcript({"type": "assistant", "timestamp": "2026-10-28T05:12:58.000Z"},
                              {"type": "cost-state", "totalCostUSD": 1.0})
        self.touch(self.transcript, SCANNED_AT + timedelta(minutes=11, seconds=59))
        tabs = FakeTabs()

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual(code, 1)
        self.assertEqual(tabs.closed, [])
        self.assertIn("term_agent: KEPT -> 走査の後に動いた（", out)
        self.assertIn((SCANNED_AT + timedelta(minutes=11, seconds=58)).astimezone().strftime("%H:%M:%S"), out)

    def test_a_transcript_rewritten_after_the_scan_with_no_new_entry_closes(self) -> None:
        # Claude Code appends untimed bookkeeping (bridge-session, cost-state) to an idle session's transcript.
        self.write_transcript({"type": "assistant", "timestamp": "2026-10-28T01:46:42.803Z"},
                              {"type": "bridge-session", "sessionId": "6a0027d4"})
        self.touch(self.transcript, SCANNED_AT + timedelta(minutes=21))
        tabs = FakeTabs()

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual((code, tabs.closed), (0, [(None, "term_agent")]), out)

    def test_a_report_the_worktree_sent_after_the_scan_keeps_its_tab(self) -> None:
        self.inbox.write_text(json.dumps({
            "ts": "2026-10-28T14:12:40+0900", "kind": "info", "worktree": "fix-login",
            "repo": "web", "host": "eotel-macbook-pro", "text": "staging に配備しました。確かめを待ちます"},
            ensure_ascii=False) + "\n")
        tabs = FakeTabs()

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual(code, 1)
        self.assertEqual(tabs.closed, [])
        self.assertIn("走査の後に動いた（", out)
        self.assertIn("info", out)

    def test_a_remote_tab_whose_agent_changed_state_after_the_scan_stays_open(self) -> None:
        rows = json.loads(self.scan.read_text())
        rows[0] |= {"environment": "mbp16", "claude": None}
        self.scan.write_text(json.dumps(rows))
        tabs = FakeTabs({("mbp16", str(self.worktree)): [SCANNED_AT + timedelta(minutes=5)]})

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual((code, tabs.closed), (1, []))
        self.assertIn("走査の後に動いた（", out)

    def test_a_tab_with_nothing_after_the_scan_closes(self) -> None:
        self.inbox.write_text(json.dumps({"ts": "2026-10-28T13:40:00+0900", "kind": "done",
                                          "worktree": "fix-login", "repo": "web"}) + "\n")
        tabs = FakeTabs({(None, str(self.worktree)): [SCANNED_AT - timedelta(minutes=40)]})

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual((code, tabs.closed), (0, [(None, "term_agent")]))
        self.assertIn("term_agent: CLOSED", out)

    def test_a_tab_whose_host_cannot_be_read_stays_open(self) -> None:
        class Unreachable(FakeTabs):
            def agent_changes(self, environment: str | None, path: str) -> list[datetime]:
                raise RuntimeError("Could not read Orca runtime metadata")
        tabs = Unreachable()

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual((code, tabs.closed), (1, []))
        self.assertIn("term_agent: KEPT -> Could not read Orca runtime metadata", out)

    def test_a_transcript_written_after_the_scan_that_cannot_be_read_keeps_its_tab(self) -> None:
        self.write_transcript({"type": "assistant", "timestamp": "2026-10-28T05:12:58.000Z"})
        self.touch(self.transcript, SCANNED_AT + timedelta(minutes=12))
        self.transcript.chmod(0)
        self.addCleanup(self.transcript.chmod, 0o600)
        tabs = FakeTabs()

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual((code, tabs.closed), (1, []))
        self.assertIn("term_agent: KEPT", out)

    def test_an_inbox_that_cannot_be_read_keeps_the_tab(self) -> None:
        self.inbox.chmod(0)
        self.addCleanup(self.inbox.chmod, 0o600)
        tabs = FakeTabs()

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual((code, tabs.closed), (1, []))
        self.assertIn("term_agent: KEPT", out)

    def test_the_transcript_the_scan_read_is_read_again_under_another_projects_root(self) -> None:
        # The scan ran with another --claude-projects; the close command it printed does not repeat it.
        self.write_transcript({"type": "assistant", "timestamp": "2026-10-28T05:12:58.000Z"})
        self.touch(self.transcript, SCANNED_AT + timedelta(minutes=12))
        tabs = FakeTabs()
        argv = ["--from", str(self.scan), "--claude-projects", str(self.dir / "default-projects"),
                "--inbox", str(self.inbox), "term_agent"]

        with redirect_stdout(io.StringIO()) as out:
            code = close_tabs.main(argv, tabs)

        self.assertEqual((code, tabs.closed), (1, []))
        self.assertIn("走査の後に動いた（", out.getvalue())

    def test_orca_is_read_again_for_each_tab_it_closes(self) -> None:
        # A second worktree whose agent starts working while the first tab closes must not be closed with it.
        other = self.dir / "workspaces" / "fix-header"
        rows = json.loads(self.scan.read_text())
        rows.append({**rows[0], "path": str(other), "claude": None,
                     "terminals": [{"handle": "term_other", "agent": "claude", "title": "fix-header"}]})
        self.scan.write_text(json.dumps(rows))
        orca = self.dir / "orca"
        state = self.dir / "orca-state"
        before = int((SCANNED_AT - timedelta(minutes=30)).timestamp() * 1000)
        orca.write_text(f"""#!{sys.executable}
import json, sys, time
from pathlib import Path
state = Path({str(state)!r})
if sys.argv[1:3] == ["terminal", "close"]:
    state.write_text("closed")
    print(json.dumps({{"ok": True, "result": {{}}}}))
else:
    other = int(time.time() * 1000) if state.exists() else {before}
    print(json.dumps({{"ok": True, "result": {{"worktrees": [
        {{"path": {str(self.worktree)!r}, "agents": [{{"stateStartedAt": {before}}}]}},
        {{"path": {str(other)!r}, "agents": [{{"stateStartedAt": other}}]}}]}}}}))
""")
        orca.chmod(0o755)
        argv = ["--from", str(self.scan), "--claude-projects", str(self.projects), "--inbox", str(self.inbox),
                "--orca", str(orca), "term_agent", "term_other"]

        with redirect_stdout(io.StringIO()) as out:
            code = close_tabs.main(argv)

        self.assertEqual(code, 1)
        self.assertIn("term_agent: CLOSED", out.getvalue())
        self.assertIn("term_other: KEPT -> 走査の後に動いた（", out.getvalue())

    def test_a_scan_saved_without_its_start_time_closes_nothing(self) -> None:
        rows = json.loads(self.scan.read_text())
        del rows[0]["scanned_at"]
        self.scan.write_text(json.dumps(rows))
        tabs = FakeTabs()

        code, out = self.run_close(tabs, "term_agent")

        self.assertEqual((code, tabs.closed), (1, []))
        self.assertIn("scanned_at", out)


if __name__ == "__main__":
    unittest.main()
