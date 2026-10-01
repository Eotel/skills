"""Offline behavior tests for the public review-response command line tools."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "review-response" / "scripts"
WORKTREE_SETUP = ROOT / "codex-tdd-orchestration" / "scripts" / "worktree-setup.sh"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


asana = load_script("asana_tasks")
prs = load_script("watch_prs")
checks = load_script("watch_checks")
renderer = load_script("render_status")


class RenderStatusTests(unittest.TestCase):
    repos = {"api": "acme/api", "ui": "acme/ui", "api-tools": "acme/api-tools"}

    def assert_link(self, text, expected, row_repo=None):
        self.assertEqual(renderer.linkify(text, row_repo, self.repos), expected)

    def test_full_repo(self):
        self.assert_link("acme/api#12", "[acme/api#12](https://github.com/acme/api/issues/12)")

    def test_alias(self):
        self.assert_link("api#12", "[api#12](https://github.com/acme/api/issues/12)")

    def test_alias_with_hyphen(self):
        self.assert_link("api-tools#12", "[api-tools#12](https://github.com/acme/api-tools/issues/12)")

    def test_pr_uses_row_repo_and_pull_path(self):
        self.assert_link("PR#12", "[PR#12](https://github.com/acme/api/pull/12)", "api")
        self.assert_link("PR#12", "PR#12")

    def test_bare_uses_row_repo(self):
        self.assert_link("#1", "[#1](https://github.com/acme/api/issues/1)", "acme/api")

    def test_bare_without_context_stays_unlinked(self):
        self.assert_link("#12", "#12")
        self.assert_link("unknown#12", "unknown#12", "api")

    def test_bare_uses_last_alias_without_row_repo(self):
        self.assert_link("api#12 and #13", "[api#12](https://github.com/acme/api/issues/12) and [#13](https://github.com/acme/api/issues/13)")

    def test_full_repo_sets_context_for_bare_and_continuation(self):
        self.assert_link("acme/api#12/#13 and #14", "[acme/api#12](https://github.com/acme/api/issues/12)/[#13](https://github.com/acme/api/issues/13) and [#14](https://github.com/acme/api/issues/14)")

    def test_row_repo_wins_for_bare_but_continuation_uses_last_named_repo(self):
        self.assert_link("ui#12/#13 and #14", "[ui#12](https://github.com/acme/ui/issues/12)/[#13](https://github.com/acme/ui/issues/13) and [#14](https://github.com/acme/api/issues/14)", "api")

    def test_japanese_adjacency(self):
        self.assert_link("#201は完了", "[#201](https://github.com/acme/api/issues/201)は完了", "api")
        self.assert_link("api#201は確認済み", "[api#201](https://github.com/acme/api/issues/201)は確認済み")
        self.assert_link("対象#201は", "対象[#201](https://github.com/acme/api/issues/201)は", "api")

    def test_ascii_word_char_before_number_prevents_link(self):
        for text in ("word#201", "a#201は", "_#201", "1#201", "xapi#201"):
            with self.subTest(text=text):
                self.assert_link(text, text, "api")

    def test_alias_does_not_match_inside_longer_repository_token(self):
        for text in ("unknown-api#12", "unknown.api#12", "unknown_api#12", "unknownapi#12", "1api#12", "/api#12"):
            with self.subTest(text=text):
                self.assert_link(text, text, "api")

    def test_code_spans_with_multiple_backticks_are_preserved(self):
        for width in (2, 3, 4, 8, 20):
            delimiter = "`" * width
            text = delimiter + "api#12" + delimiter
            with self.subTest(width=width):
                self.assert_link(text, text, "api")
        for text in ("``api#12 ` #13``", "``api#12 ``` #13``", "```api#12 `` #13```", "``api#12\n#13``"):
            with self.subTest(text=text):
                self.assert_link(text, text, "api")
        self.assert_link("``api#12`` #13", "``api#12`` [#13](https://github.com/acme/api/issues/13)", "api")

    def test_existing_markdown_urls_and_code_are_preserved(self):
        for text in ("[#12](https://github.com/acme/api/issues/12)", "`api#12`", "https://example.invalid/api#12"):
            with self.subTest(text=text):
                self.assert_link(text, text, "api")

    def data(self):
        return {
            "repos": self.repos,
            "units": [
                {"id": "one", "repo": "api", "target": "PR#12", "what": "Fix #201は", "state": "done: merged", "next": "none", "done": False},
                {"id": "two", "repo": "api", "target": "PR#13", "what": "Wait", "state": "pending", "next": "review #202", "done": True},
                {"id": "three", "repo": "ui", "target": "PR#14", "what": "Fix", "state": "done", "next": "none"},
            ],
            "out_of_scope": ["ui#20/#21 and #22: another author's work", "acme/api#23 and #24: deferred"],
        }

    def test_done_count_uses_state_prefix_and_out_of_scope_links(self):
        rendered = renderer.render_status(self.data())
        self.assertEqual(rendered.splitlines()[0], "# Review response: 2/3 done")
        self.assertIn("[#201](https://github.com/acme/api/issues/201)は", rendered)
        for number in (20, 21, 22):
            self.assertIn(f"https://github.com/acme/ui/issues/{number}", rendered)
        self.assertIn("https://github.com/acme/api/issues/24", rendered)
        self.assertIn("https://github.com/acme/api/pull/12", rendered)

    def test_configurable_done_prefix(self):
        data = self.data()
        data["units"][1]["state"] = "完了: 確認済み"
        self.assertEqual(renderer.render_status(data, "完了").splitlines()[0], "# Review response: 1/3 done")

    def test_cells_escape_pipes_and_newlines(self):
        data = self.data()
        data["units"][0]["what"] = "one | two\nthree"
        self.assertIn(r"one \| two<br>three", renderer.render_status(data))

    def test_context_does_not_leak_between_cells(self):
        data = self.data()
        data["units"][0].update(repo="", target="api#12", what="#201")
        self.assertIn(" | #201 | ", renderer.render_status(data))

    def test_cli_renders_read_only_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "status.json"
            output = Path(temporary) / "status.md"
            source.write_text(json.dumps(self.data()), encoding="utf-8")
            original = source.read_bytes()
            result = subprocess.run([sys.executable, str(SCRIPTS / "render_status.py"), str(source), "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), renderer.render_status(self.data()))
            self.assertEqual(source.read_bytes(), original)

    def test_cli_rejects_output_aliases_without_changing_input(self):
        for alias in ("same", "normalized", "symlink", "hardlink"):
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "status.json"
                source.write_text(json.dumps(self.data()), encoding="utf-8")
                original = source.read_bytes()
                if alias == "same":
                    output = source
                elif alias == "normalized":
                    (source.parent / "sub").mkdir()
                    output = source.parent / "sub" / ".." / source.name
                elif alias == "symlink":
                    output = source.parent / "status-link.json"
                    output.symlink_to(source)
                else:
                    output = source.parent / "status-hardlink.json"
                    os.link(source, output)
                result = subprocess.run([sys.executable, str(SCRIPTS / "render_status.py"), str(source), "--output", str(output)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--output", result.stderr)
                self.assertIn("input JSON file", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(source.read_bytes(), original)


class WatchPrsTests(unittest.TestCase):
    spec = "acme/api#12"
    me = "author"

    def snapshot(self, **changes):
        result = {
            "state": "OPEN", "headRefOid": "a" * 40, "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "unit", "status": "IN_PROGRESS", "conclusion": None}],
            "reviews": [], "line_comments": [], "comments": [],
        }
        result.update(changes)
        return result

    def transition(self, before, after):
        _, previous = prs.diff_events(self.spec, {}, before, self.me)
        preserved = copy.deepcopy(previous)
        snapshot_copy = copy.deepcopy(after)
        events, state = prs.diff_events(self.spec, previous, after, self.me)
        self.assertEqual(previous, preserved)
        self.assertEqual(after, snapshot_copy)
        repeated, _ = prs.diff_events(self.spec, state, after, self.me)
        self.assertEqual(repeated, [])
        return events, state

    def test_success_and_failure_completion_events(self):
        for conclusion, word in (("SUCCESS", "SUCCESS"), ("FAILURE", "FAILURE")):
            with self.subTest(conclusion=conclusion):
                after = self.snapshot(statusCheckRollup=[{"name": "unit", "status": "COMPLETED", "conclusion": conclusion}])
                events, _ = self.transition(self.snapshot(), after)
                self.assertEqual(len(events), 1)
                self.assertIn("CI " + word, events[0])
                self.assertIn("1 checks", events[0])
                self.assertIn(f"{int(word == 'FAILURE')} failed", events[0])

    def test_new_head_is_a_new_completion_even_with_same_result(self):
        finished = [{"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        before = self.snapshot(statusCheckRollup=finished)
        # The entire SHA is the identity, even when the displayed prefix matches.
        after = self.snapshot(statusCheckRollup=finished, headRefOid="a" * 39 + "b")
        events, _ = self.transition(before, after)
        self.assertEqual(len(events), 1)
        self.assertIn("CI SUCCESS", events[0])

    def test_same_head_reports_changed_failing_checks(self):
        before = self.snapshot(statusCheckRollup=[
            {"name": "unit", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"name": "security", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        after = self.snapshot(statusCheckRollup=[
            {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "security", "status": "COMPLETED", "conclusion": "FAILURE"},
        ])
        events, _ = self.transition(before, after)
        self.assertEqual(len(events), 1)
        self.assertIn('CI FAILURE (2 checks, 1 failed', events[0])
        self.assertIn('failed=["security"]', events[0])

    def test_same_head_reports_changed_commit_statuses(self):
        before = self.snapshot(statusCheckRollup=[
            {"context": "unit", "state": "FAILURE"},
            {"context": "security", "state": "SUCCESS"},
        ])
        after = self.snapshot(statusCheckRollup=[
            {"context": "unit", "state": "SUCCESS"},
            {"context": "security", "state": "ERROR"},
        ])
        events, _ = self.transition(before, after)
        self.assertEqual(len(events), 1)
        self.assertIn('failed=["security"]', events[0])

    def test_same_head_reports_changed_terminal_conclusion(self):
        for before_check, after_check in (
            ({"name": "unit", "status": "COMPLETED", "conclusion": "FAILURE"},
             {"name": "unit", "status": "COMPLETED", "conclusion": "TIMED_OUT"}),
            ({"context": "legacy", "state": "FAILURE"},
             {"context": "legacy", "state": "ERROR"}),
        ):
            with self.subTest(before=before_check, after=after_check):
                events, _ = self.transition(self.snapshot(statusCheckRollup=[before_check]), self.snapshot(statusCheckRollup=[after_check]))
                self.assertEqual(len(events), 1)
                self.assertIn("CI FAILURE", events[0])

    def test_reordered_terminal_checks_and_statuses_emit_nothing(self):
        terminal = [
            {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"context": "legacy", "state": "ERROR"},
        ]
        events, _ = self.transition(self.snapshot(statusCheckRollup=terminal), self.snapshot(statusCheckRollup=list(reversed(terminal))))
        self.assertEqual(events, [])

    def test_pending_then_unchanged_terminal_result_does_not_duplicate(self):
        terminal = self.snapshot(statusCheckRollup=[{"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"}])
        _, previous = prs.diff_events(self.spec, {}, terminal, self.me)
        events, pending = prs.diff_events(self.spec, previous, self.snapshot(), self.me)
        self.assertEqual(events, [])
        events, _ = prs.diff_events(self.spec, pending, terminal, self.me)
        self.assertEqual(events, [])

    def test_conflicting_event(self):
        events, _ = self.transition(self.snapshot(), self.snapshot(mergeable="CONFLICTING"))
        self.assertEqual(events, [self.spec + " CONFLICTING with base"])

    def test_each_comment_feed_and_ignore_self(self):
        for feed, kind in (("reviews", "REVIEW"), ("line_comments", "LINE"), ("comments", "COMMENT")):
            with self.subTest(feed=feed):
                items = [
                    {"id": 1, "user": {"login": "reviewer"}, "state": "COMMENTED", "path": "file.py", "body": "first\nsecond"},
                    {"id": 2, "user": {"login": "AUTHOR"}, "state": "COMMENTED", "body": "own comment"},
                ]
                events, state = self.transition(self.snapshot(), self.snapshot(**{feed: items}))
                self.assertEqual(len(events), 1)
                self.assertIn(f"NEW {kind} by reviewer", events[0])
                self.assertIn("first second", events[0])
                self.assertNotIn("\n", events[0])
                self.assertIn(kind + ":2", state["seen"])

    def test_existing_comments_are_not_reemitted(self):
        old = {"id": 1, "user": {"login": "reviewer"}, "body": "old"}
        new = {"id": 2, "user": {"login": "reviewer"}, "body": "new"}
        events, _ = self.transition(self.snapshot(comments=[old]), self.snapshot(comments=[old, new]))
        self.assertEqual(len(events), 1)
        self.assertIn(" :: new", events[0])

    def test_pending_review_waits_for_submission(self):
        draft = {"id": 1, "user": {"login": "reviewer"}, "state": "PENDING", "body": "draft"}
        submitted = dict(draft, state="CHANGES_REQUESTED")
        events, _ = self.transition(self.snapshot(reviews=[draft]), self.snapshot(reviews=[submitted]))
        self.assertEqual(len(events), 1)
        self.assertIn("NEW REVIEW", events[0])

    def test_terminal_states(self):
        for terminal in ("MERGED", "CLOSED"):
            with self.subTest(terminal=terminal):
                events, _ = self.transition(self.snapshot(), self.snapshot(state=terminal))
                self.assertEqual(events, [self.spec + " " + terminal])

    def test_legacy_statuses_and_incomplete_checks(self):
        self.assertEqual(prs.finished_checks([{ "context": "legacy", "state": "SUCCESS"}]), (True, []))
        self.assertEqual(prs.finished_checks([{ "context": "legacy", "state": "ERROR"}]), (True, ["legacy"]))
        self.assertEqual(prs.finished_checks([]), (False, []))
        for check in (
            {"name": "unit", "status": "QUEUED", "conclusion": "SUCCESS"},
            {"name": "unit", "status": "IN_PROGRESS", "conclusion": None},
            {"context": "legacy", "state": "PENDING"},
        ):
            with self.subTest(check=check):
                self.assertEqual(prs.finished_checks([check]), (False, []))

    def test_re_reads_list_each_cycle_and_persists_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            listing = Path(temporary) / "prs.txt"
            state_file = Path(temporary) / "state.json"
            listing.write_text(self.spec + "\n", encoding="utf-8")
            extra = "acme/ui#13"
            output = io.StringIO()
            cycles = iter((True, False))

            def next_cycle(_):
                if not next(cycles):
                    raise KeyboardInterrupt
                listing.write_text("# list updated\n" + self.spec + "\n" + extra + "\n", encoding="utf-8")

            snapshots = [self.snapshot(state="MERGED"), self.snapshot(state="MERGED"), self.snapshot(state="CLOSED")]
            with patch.object(prs, "fetch_snapshot", side_effect=snapshots) as fetch, patch.object(prs.time, "sleep", side_effect=next_cycle), redirect_stdout(output):
                self.assertEqual(prs.main([str(listing), "--state-file", str(state_file), "--me", self.me, "--baseline"]), 130)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [self.spec, self.spec, extra])
            self.assertEqual(output.getvalue(), extra + " CLOSED\n")
            self.assertEqual(json.loads(state_file.read_text())[self.spec]["state"], "MERGED")

    def test_api_pagination_is_flattened(self):
        item = {"id": 1, "user": {"login": "reviewer"}}
        with patch.object(prs, "gh_json", side_effect=[self.snapshot(), [[item], [{"id": 2}]], [[]], [[]]]) as gh:
            snapshot = prs.fetch_snapshot(self.spec)
        self.assertEqual(len(snapshot["reviews"]), 2)
        self.assertIn("--paginate", gh.call_args_list[1].args[0])
        self.assertIn("--slurp", gh.call_args_list[1].args[0])

    def test_invalid_state_file_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            listing = Path(temporary) / "prs.txt"
            state_file = Path(temporary) / "state.json"
            listing.write_text(self.spec, encoding="utf-8")
            state_file.write_text("invalid-json", encoding="utf-8")
            with redirect_stderr(io.StringIO()), patch.object(prs, "fetch_snapshot") as fetch:
                self.assertEqual(prs.main([str(listing), "--state-file", str(state_file), "--me", self.me, "--once"]), 1)
            fetch.assert_not_called()
            self.assertEqual(state_file.read_text(), "invalid-json")

    def test_failed_poll_does_not_advance_state_or_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            listing = Path(temporary) / "prs.txt"
            state_file = Path(temporary) / "state.json"
            listing.write_text(self.spec, encoding="utf-8")
            _, state = prs.diff_events(self.spec, {}, self.snapshot(), self.me)
            state_file.write_text(json.dumps({self.spec: state}), encoding="utf-8")
            errors = io.StringIO()
            with patch.object(prs, "fetch_snapshot", side_effect=ValueError("API unavailable")), redirect_stderr(errors):
                self.assertEqual(prs.main([str(listing), "--state-file", str(state_file), "--me", self.me, "--once"]), 1)
            self.assertEqual(json.loads(state_file.read_text()), {self.spec: state})
            self.assertIn("API unavailable", errors.getvalue())


class WatchChecksTests(unittest.TestCase):
    def poll_commit(self, runs, status_pages, suite_pages=None):
        output = io.StringIO()
        errors = io.StringIO()
        if suite_pages is None:
            suite_pages = [{"check_suites": [{"status": "completed"}] if runs else []}]

        def api_response(command, **kwargs):
            if command[-1] == "repos/example/api/commits/abcdef123/check-runs?per_page=100":
                payload = [{"check_runs": runs}]
            elif command[-1] == "repos/example/api/commits/abcdef123/status?per_page=100":
                payload = status_pages
            elif command[-1] == "repos/example/api/commits/abcdef123/check-suites?per_page=100":
                payload = suite_pages
            else:
                self.fail(f"unexpected API command: {command}")
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        with patch.object(checks.subprocess, "run", side_effect=api_response) as api, patch.object(checks.time, "sleep", side_effect=KeyboardInterrupt) as sleep, redirect_stdout(output), redirect_stderr(errors):
            code = checks.main(["example/api@abcdef123"])
        self.assertEqual(errors.getvalue(), "")
        return code, output.getvalue(), api.call_args_list, sleep

    def test_incomplete_check_suites_keep_completed_runs_pending(self):
        runs = [{"name": "unit", "status": "completed", "conclusion": "success"}]
        statuses = [{"state": "success", "statuses": [{"context": "legacy", "state": "success"}]}]
        for state in ("queued", "in_progress"):
            with self.subTest(state=state):
                suites = [{"check_suites": [{"status": "completed"}]}, {"check_suites": [{"status": state}]}]
                code, output, _, sleep = self.poll_commit(runs, statuses, suites)
                self.assertEqual(code, 130)
                self.assertEqual(output, "")
                sleep.assert_called_once_with(60)

    def test_pending_commit_status_prevents_completion(self):
        runs = [{"name": "unit", "status": "completed", "conclusion": "success"}]
        code, output, _, sleep = self.poll_commit(runs, [{"state": "pending", "statuses": [{"context": "legacy", "state": "pending"}], "total_count": 1}])
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        sleep.assert_called_once_with(60)

    def test_failed_and_errored_commit_statuses_are_reported(self):
        runs = [{"name": "unit", "status": "completed", "conclusion": "success"}]
        statuses = [{"context": "legacy", "state": "failure"}, {"context": "security", "state": "error"}]
        code, output, _, sleep = self.poll_commit(runs, [{"state": "failure", "statuses": statuses, "total_count": 2}])
        self.assertEqual(code, 0)
        self.assertEqual(output, 'DONE example/api@abcdef123 FAILED ["legacy", "security"]\n')
        sleep.assert_not_called()

    def test_statuses_only_commit_finishes(self):
        for state, expected in (("success", "ok (1 checks)"), ("failure", 'FAILED ["legacy"]'), ("error", 'FAILED ["legacy"]')):
            with self.subTest(state=state):
                combined_state = "success" if state == "success" else "failure"
                code, output, _, sleep = self.poll_commit([], [{"state": combined_state, "statuses": [{"context": "legacy", "state": state}], "total_count": 1}])
                self.assertEqual(code, 0)
                self.assertEqual(output, f"DONE example/api@abcdef123 {expected}\n")
                sleep.assert_not_called()

    def test_check_runs_only_commit_finishes_with_empty_pending_status(self):
        runs = [{"name": "unit", "status": "completed", "conclusion": "success"}]
        code, output, calls, sleep = self.poll_commit(runs, [{"state": "pending", "statuses": [], "total_count": 0}])
        self.assertEqual(code, 0)
        self.assertEqual(output, "DONE example/api@abcdef123 ok (1 checks)\n")
        self.assertEqual(len(calls), 3)
        sleep.assert_not_called()

    def test_successful_status_does_not_finish_incomplete_check_run(self):
        code, output, _, sleep = self.poll_commit([{"name": "unit", "status": "queued"}], [{"state": "success", "statuses": [{"context": "legacy", "state": "success"}], "total_count": 1}])
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        sleep.assert_called_once_with(60)

    def test_combined_failure_does_not_hide_pending_status(self):
        runs = [{"name": "unit", "status": "completed", "conclusion": "success"}]
        statuses = [{"context": "legacy", "state": "failure"}, {"context": "security", "state": "pending"}]
        code, output, _, sleep = self.poll_commit(runs, [{"state": "failure", "statuses": statuses, "total_count": 2}])
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        sleep.assert_called_once_with(60)

    def test_status_pagination_includes_failures_on_later_pages(self):
        runs = [{"name": "unit", "status": "completed", "conclusion": "success"}]
        pages = [
            {"state": "failure", "statuses": [{"context": "legacy", "state": "success"}], "total_count": 2},
            {"state": "failure", "statuses": [{"context": "security", "state": "error"}], "total_count": 2},
        ]
        code, output, calls, _ = self.poll_commit(runs, pages)
        self.assertEqual(code, 0)
        self.assertEqual(output, 'DONE example/api@abcdef123 FAILED ["security"]\n')
        self.assertIn("--paginate", calls[1].args[0])
        self.assertIn("--slurp", calls[1].args[0])

    def test_empty_or_incomplete_is_not_done(self):
        for runs in ([], [{"status": "queued"}], [{"status": "in_progress"}], [{"status": "completed", "conclusion": "success"}, {"status": "queued"}]):
            with self.subTest(runs=runs):
                self.assertEqual(checks.classify_checks(runs), (False, []))

    def test_success_skipped_neutral_are_ok(self):
        runs = [{"name": result, "status": "completed", "conclusion": result} for result in ("success", "skipped", "neutral")]
        self.assertEqual(checks.classify_checks(runs), (True, []))

    def test_failed_classification(self):
        runs = [{"name": result, "status": "completed", "conclusion": result} for result in ("failure", "cancelled", "timed_out", "action_required")]
        runs.append({"name": "unknown", "status": "completed", "conclusion": None})
        self.assertEqual(checks.classify_checks(runs), (True, ["failure", "cancelled", "timed_out", "action_required", "unknown"]))

    def test_main_emits_once_per_commit_and_exits_without_final_sleep(self):
        ok = [{"name": "unit", "status": "completed", "conclusion": "success"}]
        bad = [{"name": "lint", "status": "completed", "conclusion": "failure"}]
        output = io.StringIO()
        with patch.object(checks, "check_runs", side_effect=[ok, [{"status": "queued"}], bad]) as runs, patch.object(checks, "combined_status", return_value={"state": "pending", "statuses": []}), patch.object(checks, "check_suites", return_value=[{"status": "completed"}]), patch.object(checks.time, "sleep") as sleep, redirect_stdout(output):
            code = checks.main(["acme/api@abcdef123", "acme/api@abcdef123", "acme/ui@fedcba321"])
        self.assertEqual(code, 0)
        self.assertEqual(runs.call_count, 3)
        sleep.assert_called_once_with(60)
        self.assertEqual(output.getvalue().splitlines(), ["DONE acme/api@abcdef123 ok (1 checks)", 'DONE acme/ui@fedcba321 FAILED ["lint"]'])

    def test_api_pagination_covers_all_check_runs(self):
        completed = {"name": "unit", "status": "completed", "conclusion": "success"}
        queued = {"name": "lint", "status": "queued"}
        result = subprocess.CompletedProcess([], 0, json.dumps([{"check_runs": [completed]}, {"check_runs": [queued]}]), "")
        with patch.object(checks.subprocess, "run", return_value=result) as run:
            all_runs = checks.check_runs("acme/api", "abcdef123")
        self.assertEqual(checks.classify_checks(all_runs), (False, []))
        self.assertIn("--paginate", run.call_args.args[0])
        self.assertIn("--slurp", run.call_args.args[0])

    def test_failed_api_cannot_be_classified_as_empty_success(self):
        with patch.object(checks.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "not authorized")):
            with self.assertRaisesRegex(ValueError, "gh api failed"):
                checks.check_runs("acme/api", "abcdef123")

    def test_status_api_error_does_not_emit_completion(self):
        completed = {"name": "unit", "status": "completed", "conclusion": "success"}
        responses = [
            subprocess.CompletedProcess([], 0, json.dumps([{"check_runs": [completed]}]), ""),
            subprocess.CompletedProcess([], 1, "", "not authorized"),
        ]
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(checks.subprocess, "run", side_effect=responses), patch.object(checks.time, "sleep", side_effect=KeyboardInterrupt), redirect_stdout(output), redirect_stderr(errors):
            code = checks.main(["example/api@abcdef123"])
        self.assertEqual(code, 130)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("gh api failed: not authorized", errors.getvalue())


class AsanaTasksTests(unittest.TestCase):
    def test_missing_pat_cli_exits_without_echoing_secrets(self):
        environment = dict(os.environ)
        environment.pop("ASANA_PAT", None)
        environment["UNRELATED_SECRET"] = "unrelated-test-secret"
        result = subprocess.run([sys.executable, str(SCRIPTS / "asana_tasks.py"), "show", "task-id"], env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing ASANA_PAT", result.stderr)
        self.assertNotIn("unrelated-test-secret", result.stdout + result.stderr)

    def test_env_file_reads_only_pat_and_does_not_execute_assignments(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "test.env"
            marker = Path(temporary) / "must-not-exist"
            env_file.write_text(f"OTHER_SECRET=ignore-this\nASANA_PROJECT=do-not-load\nASANA_PAT_EXTRA=wrong\nexport ASANA_PAT = 'test-token' # comment\nOTHER=$(touch {marker})\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(asana.read_pat(env_file), "test-token")
                self.assertNotIn("ASANA_PROJECT", os.environ)
            self.assertFalse(marker.exists())

    def test_environment_pat_precedes_file(self):
        self.assertEqual(asana.read_pat("missing.env", {"ASANA_PAT": "environment-token"}), "environment-token")

    def test_env_file_quotes_and_unquoted_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "test.env"
            for assignment in ('ASANA_PAT="test-token"', "ASANA_PAT='test-token'", " ASANA_PAT = test-token # comment", "ASANA_PAT=test=token"):
                with self.subTest(assignment=assignment):
                    env_file.write_text(assignment + "\n", encoding="utf-8")
                    expected = "test=token" if "test=token" in assignment else "test-token"
                    self.assertEqual(asana.read_pat(env_file, {}), expected)

    def test_malformed_or_missing_file_assignment_never_echoes_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "test.env"
            for content in ("OTHER=unrelated-test-secret\n", 'ASANA_PAT="malformed-test-secret\n', "ASANA_PAT=\n"):
                with self.subTest(content=content):
                    env_file.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError) as context:
                        asana.read_pat(env_file, {})
                    self.assertNotIn("test-secret", str(context.exception))

    def task(self, gid="task-id", assignee="user-id", completed=False, url="https://github.com/acme/api/pull/12"):
        return {"gid": gid, "name": "Review #12", "completed": completed, "assignee": {"gid": assignee, "name": "reviewer"}, "custom_fields": [{"gid": "url-field", "text_value": url}]}

    def run_mocked(self, args, client):
        output = io.StringIO()
        with patch.dict(os.environ, {"ASANA_PAT": "test-token", "ASANA_PROJECT": "project-id", "ASANA_URL_FIELD": "url-field"}, clear=True), patch.object(asana, "Asana", return_value=client), redirect_stdout(output):
            self.assertEqual(asana.main(args), 0)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_returned_filters_assignee_and_completion_and_outputs_json_lines(self):
        client = Mock()
        client.project_tasks.return_value = [self.task(), self.task(gid="completed-id", completed=True), self.task(gid="other-id", assignee="other-user-id")]
        rows = self.run_mocked(["returned", "--assignee", "user-id"], client)
        client.project_tasks.assert_called_once_with("project-id", incomplete=True)
        self.assertEqual([row["gid"] for row in rows], ["task-id"])
        self.assertEqual(rows[0]["url"], "https://github.com/acme/api/pull/12")

    def test_find_uses_url_not_title_or_number_and_includes_completed(self):
        client = Mock()
        client.project_tasks.return_value = [self.task(completed=True), self.task(gid="other-repo", url="https://github.com/acme/ui/pull/12")]
        rows = self.run_mocked(["find", "https://github.com/acme/api/pull/12/"], client)
        client.project_tasks.assert_called_once_with("project-id", incomplete=False)
        self.assertEqual([row["gid"] for row in rows], ["task-id"])

    def test_create_keeps_full_title_and_accepts_options_after_subcommand(self):
        client = Mock()
        client.call.return_value = {"data": {"gid": "task-id"}}
        client.show.return_value = self.task()
        url = "https://github.com/acme/api/pull/12"
        self.run_mocked(["create", url, "Full title #12", "--assignee", "user-id", "--project", "override-project", "--url-field", "url-field"], client)
        client.call.assert_called_once_with("POST", "/tasks", {"name": "Full title #12", "projects": ["override-project"], "assignee": "user-id", "notes": "", "custom_fields": {"url-field": url}})

    def test_common_options_also_work_before_subcommand(self):
        client = Mock()
        client.project_tasks.return_value = []
        self.run_mocked(["--project", "override-project", "returned", "--assignee", "user-id"], client)
        client.project_tasks.assert_called_once_with("override-project", incomplete=True)

    def test_show_and_complete_accept_multiple_ids(self):
        for command in ("show", "complete"):
            with self.subTest(command=command):
                client = Mock()
                client.show.side_effect = [self.task("first-id"), self.task("second-id")]
                rows = self.run_mocked([command, "first-id", "second-id"], client)
                self.assertEqual([row["gid"] for row in rows], ["first-id", "second-id"])
                self.assertEqual(client.call.call_count, 2 if command == "complete" else 0)
                if command == "complete":
                    self.assertEqual([call.args for call in client.call.call_args_list], [
                        ("PUT", "/tasks/first-id", {"completed": True}),
                        ("PUT", "/tasks/second-id", {"completed": True}),
                    ])

    def test_project_pagination_retains_fields_and_offset(self):
        client = asana.Asana("test-token")
        with patch.object(client, "call", side_effect=[{"data": [self.task()], "next_page": {"offset": "next-page"}}, {"data": [self.task("second-id")], "next_page": None}]) as call:
            rows = list(client.project_tasks("project-id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(call.call_args.kwargs["params"]["offset"], "next-page")
        self.assertIn("custom_fields.text_value", call.call_args.kwargs["params"]["opt_fields"])
        self.assertNotEqual(call.call_args.kwargs["params"]["completed_since"], "now")

    def test_http_error_redacts_pat_and_response_body(self):
        client = asana.Asana("test-token")
        error = urllib.error.HTTPError("https://app.asana.com/api/1.0/tasks", 401, "test-token", {}, io.BytesIO(b"test-token"))
        with patch.object(asana.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, r"Asana request failed \(HTTP 401\)") as context:
                client.call("GET", "/tasks/task-id")
        self.assertNotIn("test-token", str(context.exception))

    def test_header_validation_error_cannot_echo_pat(self):
        client = asana.Asana("test-token")
        with patch.object(asana.urllib.request, "urlopen", side_effect=ValueError("Invalid header value: test-token")):
            with self.assertRaisesRegex(ValueError, "Invalid Asana response or request") as context:
                client.call("GET", "/tasks/task-id")
        self.assertNotIn("test-token", str(context.exception))


class WorktreeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.origin = self.root / "origin.git"
        self.repo = self.root / "repo"
        self.git(self.root, "init", "--bare", "-q", "-b", "main", str(self.origin))
        self.git(self.root, "clone", "-q", str(self.origin), str(self.repo))
        (self.repo / "README.md").write_text("# Example\n", encoding="utf-8")
        self.git(self.repo, "add", "README.md")
        self.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "initial fixture")
        self.git(self.repo, "push", "-q", "origin", "main")

    def git(self, cwd, *args):
        environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        return subprocess.run(["git", *args], cwd=cwd, env=environment, capture_output=True, text=True, check=True).stdout

    def setup(self, **options):
        environment = dict(os.environ)
        for name in ("WORKTREE_LOCAL_FILES", "WORKTREE_SMOKE", "TOPIC_BRANCH"):
            environment.pop(name, None)
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        environment.update(options)
        return subprocess.run(["bash", str(WORKTREE_SETUP), "topic", "main"], cwd=self.repo, env=environment, capture_output=True, text=True)

    def test_copies_untracked_local_files_reports_missing_and_smoke_sees_them(self):
        (self.repo / "config").mkdir()
        (self.repo / "config" / "local.env").write_text("LOCAL_OPTION=example\n", encoding="utf-8")
        (self.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        result = self.setup(WORKTREE_LOCAL_FILES="config/local.env\nextra.txt\tmissing.env", WORKTREE_SMOKE="test -f config/local.env && test -f extra.txt && test -f README.md")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        worktree = self.repo / ".worktrees" / "topic"
        self.assertEqual((worktree / "config" / "local.env").read_text(), "LOCAL_OPTION=example\n")
        self.assertEqual((worktree / "extra.txt").read_text(), "extra\n")
        self.assertIn("missing file: missing.env (skipped)", result.stderr)
        self.assertEqual(Path(result.stdout.splitlines()[-1]).resolve(), worktree.resolve())

    def test_smoke_failure_aborts_with_clear_message(self):
        result = self.setup(WORKTREE_SMOKE="test -f README.md || exit 2; exit 7")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("WORKTREE_SMOKE failed", result.stderr)
        self.assertIn("exit 7", result.stderr)
        self.assertNotEqual(result.stdout.splitlines()[-1], str(self.repo / ".worktrees" / "topic"))

    def test_smoke_failing_pipeline_aborts_before_following_command(self):
        result = self.setup(WORKTREE_SMOKE="false | cat; touch smoke-passed")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("WORKTREE_SMOKE failed", result.stderr)
        self.assertIn("exit 1", result.stderr)
        self.assertFalse((self.repo / ".worktrees" / "topic" / "smoke-passed").exists())
        self.assertNotEqual(result.stdout.splitlines()[-1], str(self.repo / ".worktrees" / "topic"))

    def test_default_behavior_does_not_copy_local_files(self):
        (self.repo / "local.env").write_text("LOCAL_OPTION=example\n", encoding="utf-8")
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.repo / ".worktrees" / "topic" / "local.env").exists())
        self.assertEqual(self.git(self.repo / ".worktrees" / "topic", "branch", "--show-current").strip(), "refactor/topic")


if __name__ == "__main__":
    unittest.main()
