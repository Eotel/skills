"""Offline tests for orca-session-triage's stall_watch.py: which stopped lanes wake the lead, and once."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import ModuleType
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "orca-session-triage" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stall = load_script("stall_watch")
NOW = datetime.fromisoformat("2026-11-16T10:45:00+09:00")
MAC_ZONE = patch.dict(os.environ, {"TZ": "Asia/Tokyo"})


def setUpModule() -> None:
    MAC_ZONE.start()
    time.tzset()


def tearDownModule() -> None:
    MAC_ZONE.stop()
    time.tzset()


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def report(entry_id: str, kind: str, when: str, text: str = "経過です。", handle: str = "") -> object:
    return stall.Report(entry_id, kind, at(when), text, handle)


def row(name: str, **fields: object) -> dict:
    return {"path": f"/w/app/{name}", "repo": "app", "terminals": [], "agents": [], "processes": [],
            "claude": None, "codex": [], "stop": None, **fields}


def claude(final_at: str, final_text: str = "経過を送りました。", **fields: object) -> dict:
    return {"transcript": None, "final_at": final_at, "final_text": final_text, **fields}


def codex(last_at: str, said: str = "経過を送りました。") -> list[dict]:
    return [{"last_assistant_at": last_at, "last_assistant": said, "last_role": "assistant"}]


class FakeProbe:
    def __init__(self, pr_states: dict[str, str] | None = None, origins: dict[str, str] | None = None,
                 reachable: bool = False) -> None:
        self.pr_states, self.origins, self.reachable = pr_states or {}, origins or {}, reachable

    def command(self, pid: int, saved: str) -> str:
        return saved

    def origin(self, path: str) -> str | None:
        return self.origins.get(path)

    def pr_state(self, slug: str, number: int) -> str | None:
        return self.pr_states.get(f"{slug}#{number}")

    def forge_reachable(self, path: str) -> bool | None:
        return self.reachable


LOOP = ("/bin/zsh -c until s=$(gh pr checks 3690 --json name,bucket | grep -v '^\\[') && [ -z \"$s\" ]; "
        "do sleep 30; done")


def morning() -> tuple[list[tuple[dict, dict | None]], Any]:
    """The shapes of the lanes found stopped on the morning of 2026-11-16, renamed."""
    lanes: list[tuple[dict, dict | None]] = [
        (row("watch-loop", claude=claude("2026-11-11T03:29:38Z"), processes=[
            {"pid": 2779, "parent": 1, "command": LOOP, "elapsed": 425227.0, "cpu": 8.3, "still_since": None,
             "closed": []},
            {"pid": 54607, "parent": 2779, "command": "sleep 30", "elapsed": 12.0, "cpu": 0.0, "still_since": None,
             "closed": []}]), None),
        (row("answer-left", claude=claude("2026-11-15T19:22:23+09:00")), None),
        (row("answer-elsewhere", claude=claude("2026-11-14T16:34:16+09:00")),
         {"class": "in_progress", "next_step": "再発防止の issue を起こす"}),
        (row("mac-wait", claude=claude("2026-11-13T08:47:04+09:00")),
         {"class": "blocked", "waits_on": "MBP16 への SSH 到達(ネットワーク)"}),
        (row("morning-info", codex=codex("2026-11-15T09:26:10Z")), {"class": "blocked", "waits_on": "今夜の full run"}),
        (row("morning-screen", claude=claude("2026-11-15T19:11:51+09:00",
                                             "明朝の確かめは、手順書のとおりに進めます。")), None),
        (row("named-in-readme", claude=claude("2026-11-14T23:50:25+09:00")),
         {"class": "blocked", "waits_on": "取り込みの担当"}),
        (row("done-promise", codex=codex("2026-11-15T07:51:43Z")), None),
        (row("dated-wait", claude=claude("2026-11-10T18:50:26+09:00")),
         {"class": "blocked", "waits_on": "起票者の確認、11/16 の本番移行"}),
        (row("forge-wait", claude=claude("2026-11-10T21:38:32+09:00")),
         {"class": "blocked", "waits_on": "git.example.org の復旧(push 先)"}),
        (row("person-answer", claude=claude("2026-11-05T17:50:33+09:00"),
             stop={"kind": "usage_limit", "action": "wait", "agent": "claude", "error": "weekly limit",
                   "resets": "Nov 7"}), {"class": "blocked", "waits_on": "三浦さん（直接の問い合わせ）"}),
        (row("own-question", agents=[{"state": "done"}], comment="要判断: Q4 の取り込み方"),
         {"class": "waiting", "question": "Q4"}),
        (row("automation", codex=codex("2026-11-15T07:17:07Z"), automation_workspaces=["接続確認"]),
         {"class": "blocked", "waits_on": "Forgejo の復旧"}),
        (row("held", terminals=[{"handle": "term_held"}], claude=claude("2026-11-15T19:15:56+09:00")),
         {"class": "blocked", "waits_on": "指揮の解除"}),
        (row("watching", claude=claude("2026-11-16T00:04:58+09:00"), processes=[
            {"pid": 900, "parent": 1, "command": "python3 watch_prs.py 306 767", "elapsed": 38000.0, "cpu": 4.0,
             "still_since": None, "closed": []}]), {"class": "blocked", "waits_on": "reviewer の review"}),
        (row("reviewer-wait", agents=[{"state": "done"}]), {"class": "blocked", "waits_on": "相手の回答"}),
        (row("monitor", claude=claude("2026-11-16T10:00:36+09:00")), {"class": "blocked", "waits_on": "review"}),
        (row("api-error", claude=claude("2026-11-15T20:43:27+09:00"),
             stop={"kind": "connection", "action": "wake", "agent": "claude",
                   "error": "API Error: getaddrinfo ENOTFOUND api.anthropic.com", "resets": None}),
         {"class": "blocked", "waits_on": "reviewer の PR #52 の review"}),
    ]
    records = stall.Records(
        reports_by_worktree={
            "watch-loop": (report("r1", "done", "2026-11-13T13:13:06+09:00", "投稿と起票を終えました。"),),
            "answer-left": (report("ask-left-0001", "ask", "2026-11-15T19:22:08+09:00", "台帳に記録してください。",
                                   "term_left"),),
            "answer-elsewhere": (report("r3", "info", "2026-11-14T16:19:41+09:00", "記録を直しました。"),),
            "mac-wait": (report("r4", "info", "2026-11-13T08:46:59+09:00", "MBP16 は今回も届かず、未反映です。"),),
            "morning-info": (report("r5", "info", "2026-11-15T18:26:01+09:00",
                                    "今夜の待機を終え、再照合と 2 件の補正を11/16の朝に行います。"),),
            "morning-screen": (report("r6", "info", "2026-11-15T18:01:27+09:00", "写しを消しました。"),),
            "named-in-readme": (report("r7", "info", "2026-11-14T19:14:59+09:00", "確かめを用意しました。"),),
            "done-promise": (report("r8", "done", "2026-11-15T16:51:43+09:00",
                                    "追加の変更は、明日午前の結果に再び当たった場合に行います。"),),
            "dated-wait": (report("r9", "done", "2026-11-10T18:41:43+09:00", "返信を投稿しました。"),),
            "forge-wait": (report("r10", "done", "2026-11-10T21:38:26+09:00", "deploy しました。"),),
            "person-answer": (report("r11", "done", "2026-10-29T14:55:40+09:00", "下書きを直しました。"),),
            "own-question": (report("r12", "info", "2026-11-13T15:32:44+09:00", "Q4 の回答待ちです。"),),
            "automation": (report("r13", "info", "2026-11-15T16:16:58+09:00",
                                  "18:00以降もつながらなければ、info を一度送ります。"),),
            "held": (report("r14", "done", "2026-11-14T23:50:07+09:00", "結びました。"),),
            "watching": (report("r15", "info", "2026-11-16T00:04:55+09:00", "まだ review がありません。"),),
            "reviewer-wait": (report("r16", "done", "2026-11-13T15:13:06+09:00", "控えを取り直しました。"),),
            "monitor": (report("r17", "info", "2026-11-13T13:13:08+09:00", "タスクを作りました。"),),
            "api-error": (report("r18", "info", "2026-11-12T13:44:32+09:00", "review がまだ付いていません。"),),
        },
        answers_by_ask={"ask-left-0001": (stall.Answer(at("2026-11-15T19:22:52+09:00"), "inbox_read.py answered"),)},
        sends_by_handle={"term_left": (at("2026-11-15T18:58:59+09:00"),)},
        held_handles=frozenset({"term_held"}),
        done_at_by_worktree={"own-question": at("2026-11-13T15:33:41+09:00"),
                    "reviewer-wait": at("2026-11-13T15:15:00+09:00")},
        reached_environments=frozenset({"macbook-pro-16"}), environment_by_alias={"mbp16": "macbook-pro-16"})
    return lanes, records


class MorningLanesTests(unittest.TestCase):
    def signals(self, probe: FakeProbe) -> dict[str, set[str]]:
        lanes, records = morning()
        found: dict[str, set[str]] = {}
        for lane, verdict in lanes:
            for item in stall.judge(lane, verdict, records, NOW, probe, live=False):
                found.setdefault(Path(lane["path"]).name, set()).add(item.signal)
        return found

    def test_each_lane_that_needs_a_hand_gets_its_signal_and_the_waiting_ones_none(self) -> None:
        probe = FakeProbe({"acme/app#3690": "MERGED"},
                          {"/w/app/watch-loop": "https://github.com/acme/app.git",
                           "/w/app/forge-wait": "https://git.example.org/me/app.git"})

        self.assertEqual(self.signals(probe), {
            "watch-loop": {"watcher_outlived"},
            "answer-left": {"answer_undelivered", "idle_after_report"},
            "answer-elsewhere": {"idle_after_report"},
            "mac-wait": {"wait_resolved", "idle_after_report"},
            "morning-info": {"promise_unscheduled", "idle_after_report"},
            "morning-screen": {"promise_unscheduled", "idle_after_report"},
            "named-in-readme": {"idle_after_report"},
            "done-promise": {"promise_unscheduled"},
            "dated-wait": {"wait_resolved"},
            "api-error": {"api_error_turn", "idle_after_report"},
        })

    def test_an_open_handoff_naming_the_lane_by_its_worktree_name_keeps_its_promise(self) -> None:
        lanes, records = morning()
        lane, verdict = next((lane, verdict) for lane, verdict in lanes if lane["path"].endswith("/morning-info"))
        kept = replace(records, open_handoff_names=frozenset({"morning-info"}))

        self.assertEqual(stall.judge(lane, verdict, kept, NOW, FakeProbe(), live=False), [])

    def test_a_codex_request_answered_later_in_chat_does_not_explain_the_quiet(self) -> None:
        lanes, records = morning()
        lane, _ = next((lane, verdict) for lane, verdict in lanes if lane["path"].endswith("/answer-elsewhere"))
        lane = {**lane, "claude": None,
                "codex": [{**codex("2026-11-14T07:34:16Z")[0],
                           "pending_requests": [{"answered_in_chat_later": True}]}]}

        self.assertEqual([item.signal for item in stall.judge(lane, None, records, NOW, FakeProbe(), live=False)],
                         ["idle_after_report"])

    def test_an_ask_with_a_question_still_unanswered_is_still_waiting(self) -> None:
        lanes, records = morning()
        lane, _ = next((lane, verdict) for lane, verdict in lanes if lane["path"].endswith("/answer-left"))
        two_questions = replace(records, reports_by_worktree={
            "answer-left": (replace(records.reports_by_worktree["answer-left"][0], questions=2),)},
            answers_by_ask={"ask-left-0001:1": records.answers_by_ask["ask-left-0001"]})

        self.assertEqual(stall.judge(lane, None, two_questions, NOW, FakeProbe(), live=False), [])

    def test_a_lane_whose_earlier_ask_is_still_open_is_waiting_not_idle(self) -> None:
        lanes, records = morning()
        lane, verdict = next((lane, verdict) for lane, verdict in lanes if lane["path"].endswith("/answer-elsewhere"))
        asked_first = replace(records, reports_by_worktree={**records.reports_by_worktree, "answer-elsewhere": (
            report("ask-open-0001", "ask", "2026-11-14T16:10:00+09:00", "どちらで進めますか。"),
            *records.reports_by_worktree["answer-elsewhere"])})

        self.assertEqual(stall.judge(lane, verdict, asked_first, NOW, FakeProbe(), live=False), [])

    def test_the_same_lane_name_on_another_mac_is_a_different_work(self) -> None:
        lanes, records = morning()
        lane, verdict = next((lane, verdict) for lane, verdict in lanes if lane["path"].endswith("/dated-wait"))
        here = stall.judge(lane, verdict, records, NOW, FakeProbe(), live=False)
        there = stall.judge({**lane, "environment": "mac-two"}, verdict, records, NOW, FakeProbe(), live=False)

        self.assertNotEqual({stall.key_of(asdict(item)) for item in here}, {stall.key_of(asdict(item)) for item in there})

    def test_a_forge_wait_signals_once_the_forge_answers(self) -> None:
        probe = FakeProbe(origins={"/w/app/forge-wait": "https://git.example.org/me/app.git"}, reachable=True)

        self.assertEqual(self.signals(probe)["forge-wait"], {"wait_resolved"})


class PromiseTests(unittest.TestCase):
    def test_deadlines_references_and_times_already_past_are_not_promises(self) -> None:
        said = at("2026-11-15T15:09:00+09:00")
        for sentence in ("書き込みは 14:48 に終え、これから merge します。",
                         "11/16 までに作り、検証環境で確かめます。",
                         "直す変更を 11/16 より前に入れ、検証環境で確かめます。",
                         "11/14 と同じ流し方で確かめを進めます。",
                         "取込は、手順 3/4 の入れ替えの前に流します。"):
            with self.subTest(sentence):
                self.assertIsNone(stall.promise_due(sentence, said))

    def test_a_promised_time_falls_due_in_the_mac_zone(self) -> None:
        said = at("2026-11-15T18:26:00+09:00")

        self.assertEqual(stall.promise_due("再照合と 2 件の補正を11/16の朝に行います。", said),
                         at("2026-11-16T09:00:00+09:00"))
        self.assertEqual(stall.promise_due("差が小さいので、18:30 に登録します。", said),
                         at("2026-11-15T18:30:00+09:00"))
        self.assertEqual(stall.promise_due("明日 18:30 に確認します。", said), at("2026-11-16T18:30:00+09:00"))
        self.assertEqual(stall.promise_due("11/16 7:30 から流します。", said), at("2026-11-16T07:30:00+09:00"))


def utc(minutes_before_now: int) -> str:
    return (NOW - timedelta(minutes=minutes_before_now)).astimezone(stall.timezone.utc).isoformat().replace("+00:00", "Z")


def claude_call(minutes_before_now: int, command: str = "sed -n 1,80p app/view.py", failed: bool = False) -> list[dict]:
    use = f"toolu_{minutes_before_now}_{abs(hash(command)) % 1000}"
    return [{"type": "assistant", "timestamp": utc(minutes_before_now), "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": use, "name": "Bash", "input": {"command": command}}]}},
            {"type": "user", "timestamp": utc(minutes_before_now), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": use, "content": "…", "is_error": failed}]}}]


def codex_call(minutes_before_now: int, command: str = "sed -n 1,80p app/view.py", exit_code: int = 0) -> list[dict]:
    call = f"call_{minutes_before_now}_{abs(hash(command)) % 1000}"
    return [{"timestamp": utc(minutes_before_now), "type": "response_item", "payload": {
                "type": "custom_tool_call", "call_id": call, "name": "exec",
                "input": f"text(await tools.exec_command({{cmd:{json.dumps(command)}}}));"}},
            {"timestamp": utc(minutes_before_now), "type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": call,
                "output": f'[{{"type":"input_text","text":"{{\\"exit_code\\":{exit_code},\\"output\\":\\"…\\"}}"}}]'}}]


class BusyWithoutProgressTests(unittest.TestCase):
    """A lane that keeps calling tools with no report, commit, push, PR, or word from anyone (pstack's orchestrate)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def lane(self, agent: str, entries: list[dict]) -> dict:
        path = self.dir / f"{agent}.jsonl"
        in_order = sorted(entries, key=lambda entry: entry["timestamp"])
        path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in in_order), encoding="utf-8")
        if agent == "claude":
            return row("busy", claude=claude(utc(1), "", transcript=str(path)))
        return row("busy", codex=[{"rollout": str(path), "last_assistant_at": utc(1), "last_assistant": "",
                                   "last_role": "assistant"}])

    def signals(self, lane: dict, records: object = None, now: datetime = NOW) -> list[tuple[str, str]]:
        return [(item.signal, item.fingerprint)
                for item in stall.judge(lane, None, records or stall.Records(), now, FakeProbe(), live=False)]

    def test_a_lane_calling_tools_for_ninety_minutes_with_nothing_to_show_wakes_the_lead_once(self) -> None:
        for agent, call in (("claude", claude_call), ("codex", codex_call)):
            with self.subTest(agent):
                busy = [entry for minutes in range(104, 0, -1) for entry in call(minutes)]
                first = self.signals(self.lane(agent, busy))
                later = self.signals(self.lane(agent, busy + [e for m in range(-1, -15, -1) for e in call(m)]),
                                     now=NOW + timedelta(minutes=15))

                self.assertEqual([signal for signal, _ in first], ["busy_without_progress"])
                self.assertEqual(later, first)

    def test_a_commit_push_report_or_typed_word_within_the_ninety_minutes_is_progress(self) -> None:
        busy = [entry for minutes in range(104, 0, -1) for entry in claude_call(minutes)]
        codex_busy = [entry for minutes in range(104, 0, -1) for entry in codex_call(minutes)]
        typed = {"type": "user", "timestamp": utc(45), "message": {"role": "user", "content": "その方針で進めてください"}}
        reported = stall.Records(reports_by_worktree={
            "busy": (report("r1", "info", (NOW - timedelta(minutes=45)).isoformat()),)})
        for name, agent, entries, records in (
                ("commit in another repo", "claude", busy + claude_call(45, "git -C ../tools commit -m 'fix: retry'"), None),
                ("codex push", "codex", codex_busy + codex_call(45, "git push"), None),
                ("report", "claude", busy, reported),
                ("typed word", "claude", busy + [typed], None)):
            with self.subTest(name):
                self.assertEqual(self.signals(self.lane(agent, entries), records), [])
        for name, agent, entries in (("failed push", "claude", busy + claude_call(45, "git push", failed=True)),
                                     ("failed codex push", "codex", codex_busy + codex_call(45, "git push", 1))):
            with self.subTest(name):
                self.assertEqual([signal for signal, _ in self.signals(self.lane(agent, entries))],
                                 ["busy_without_progress"])

    def test_a_codex_lane_calling_tools_without_a_word_for_forty_minutes_is_judged_in_a_live_run(self) -> None:
        lane = self.lane("codex", [entry for minutes in range(104, 0, -1) for entry in codex_call(minutes)])
        silent = {**lane, "codex": [{**lane["codex"][0], "last_assistant_at": utc(40)}]}

        self.assertEqual([item.signal for item in stall.judge(silent, None, stall.Records(), NOW, FakeProbe())],
                         ["busy_without_progress"])

    def test_a_lane_whose_tool_calls_mostly_fail_is_moved_to_the_other_agent(self) -> None:
        for agent, call, other in (("claude", lambda m: claude_call(m, failed=m % 3 > 0), "Codex"),
                                   ("codex", lambda m: codex_call(m, exit_code=int(m % 3 > 0)), "Claude")):
            with self.subTest(agent):
                found = stall.judge(self.lane(agent, [e for m in range(104, 0, -1) for e in call(m)]), None,
                                    stall.Records(), NOW, FakeProbe(), live=False)

                self.assertEqual([item.signal for item in found], ["busy_without_progress"])
                self.assertIn(f"立て直し（1 回目）: 道具のエラー。画面で同じ道具の失敗が続いていれば {other} の担当に移す",
                              found[0].reason)

    def test_a_lane_waking_every_twenty_minutes_to_check_its_pr_is_waiting_not_busy(self) -> None:
        polling = [entry for minutes in range(185, 0, -20) for entry in claude_call(minutes, "watch_prs.py --once 52")]

        self.assertEqual(self.signals(self.lane("claude", polling)), [])


class RecoveryTests(unittest.TestCase):
    """A stopped lane is recovered by its cause, and a work recovered twice is replanned (pstack's orchestrate)."""

    def stopped(self, kind: str) -> dict:
        return row("cut", terminals=[{"handle": "term_cut"}], claude=claude("2026-11-16T09:00:00+09:00"),
                   stop={"kind": kind, "action": "wake", "agent": "claude", "error": "API Error: …", "resets": None})

    def reason(self, lane: dict, records: object = None) -> str:
        found = stall.judge(lane, None, records or stall.Records(), NOW, FakeProbe(), live=False)
        return next(item.reason for item in found if item.signal == "api_error_turn")

    def test_each_cause_names_its_own_recovery(self) -> None:
        self.assertIn("立て直し（1 回目）: 通信断。同じ続きを送る", self.reason(self.stopped("connection")))
        self.assertIn("立て直し（1 回目）: 文脈の上限。範囲を小さくして新しい担当に渡す",
                      self.reason(self.stopped("context_limit")))

    def test_a_work_recovered_twice_is_replanned_instead_of_recovered_a_third_time(self) -> None:
        stops = tuple(stall.EarlierStop(at(when), ask) for when, ask in (
            ("2026-11-15T10:00:00+09:00", "ask-1"), ("2026-11-15T15:00:00+09:00", "ask-2"),
            ("2026-11-16T08:00:00+09:00", "ask-3")))
        sends = {"term_cut": tuple(at(f"2026-11-{when}+09:00") for when in ("15T10:05:00", "15T15:40:00", "16T08:30:00"))}
        # The second ask's send woke another lane, and the third stop was closed as waiting: the sends this lane got
        # afterwards relayed answers.
        once = stall.Records(stops_by_work={"app/cut": stops}, sends_by_handle=sends, answers_by_ask={
            "ask-1": (stall.Answer(at("2026-11-15T10:05:00+09:00"), "send"),),
            "ask-2": (stall.Answer(at("2026-11-15T15:05:00+09:00"), "send"),),
            "ask-3": (stall.Answer(at("2026-11-16T08:10:00+09:00"), "inbox_read.py answered"),)})
        twice = replace(once, answers_by_ask={**once.answers_by_ask,
                                              "ask-2": (stall.Answer(at("2026-11-15T15:40:00+09:00"), "send"),)})

        self.assertIn("立て直し（2 回目）: 通信断。同じ続きを送る", self.reason(self.stopped("connection"), once))
        replanned = self.reason(self.stopped("connection"), twice)
        self.assertIn("11/15 10:05・11/15 15:40 に立て直した。3 回目はせず、この作業を外して計画を立て直す", replanned)
        self.assertNotIn("同じ続きを送る", replanned)

    def test_a_work_handed_to_a_new_lane_keeps_its_recoveries_there(self) -> None:
        # One ask named two lanes, and only cut was handed to a new lane; the lead recorded it twice.
        handed = stall.Answer(at("2026-11-15T10:20:00+09:00"), "inbox_read.py answered",
                              "立て直し: cut → cut-2。other は待つ")
        again = replace(handed, at=at("2026-11-15T10:25:00+09:00"))
        records = stall.Records(
            stops_by_work={"app/cut": (stall.EarlierStop(at("2026-11-15T10:00:00+09:00"), "ask-1"),),
                           "app/other": (stall.EarlierStop(at("2026-11-15T10:00:00+09:00"), "ask-1"),),
                           "app/cut-2": (stall.EarlierStop(at("2026-11-15T20:00:00+09:00"), "ask-2"),)},
            answers_by_ask={"ask-1": (handed, again), "ask-2": (stall.Answer(at("2026-11-15T20:05:00+09:00"), "send"),)},
            sends_by_handle={"term_cut2": (at("2026-11-15T20:05:00+09:00"),)})
        lane = {**self.stopped("context_limit"), "path": "/w/app/cut-2", "terminals": [{"handle": "term_cut2"}]}

        self.assertIn("11/15 10:20・11/15 20:05 に立て直した。3 回目はせず", self.reason(lane, records))
        self.assertIn("立て直し（1 回目）", self.reason({**self.stopped("connection"), "path": "/w/app/other"}, records))
        # The old lane never reported, so only its signal remembers the tab its earlier wake reached.
        woken_first = replace(records, stops_by_work={**records.stops_by_work, "app/cut": (
            stall.EarlierStop(at("2026-11-15T08:00:00+09:00"), "ask-0", ("term_cut",)),
            *records.stops_by_work["app/cut"])}, answers_by_ask={
            **records.answers_by_ask, "ask-0": (stall.Answer(at("2026-11-15T08:05:00+09:00"), "send"),)},
            sends_by_handle={**records.sends_by_handle, "term_cut": (at("2026-11-15T08:05:00+09:00"),)})
        self.assertIn("11/15 08:05・11/15 10:20・11/15 20:05 に立て直した", self.reason(lane, woken_first))
        # A lane of the same name in another repository is another work.
        self.assertIn("立て直し（1 回目）", self.reason({**lane, "path": "/w/lib/cut-2", "repo": "lib"}, records))


class LiveProbeTests(unittest.TestCase):
    def test_a_forge_that_does_not_answer_is_asked_once_per_run(self) -> None:
        probe = stall.LiveProbe({})
        calls: list[list[str]] = []

        def git(args: list[str], **_: object) -> object:
            calls.append(args)
            if "get-url" in args:
                return subprocess.CompletedProcess(args, 0, "https://git.example.org/me/app.git\n", "")
            raise subprocess.TimeoutExpired(args, stall.PROBE_TIMEOUT)

        with patch.object(stall.subprocess, "run", side_effect=git):
            self.assertEqual([probe.forge_reachable(path) for path in ("/w/app/one", "/w/app/two")], [False, False])
        self.assertEqual(sum("ls-remote" in args for args in calls), 1)


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.inbox = self.dir / "triage-inbox.jsonl"
        self.inbox.write_text(json.dumps({"id": "ask-left-0001", "ts": "2026-11-15T19:22:08+09:00",
                                          "worktree": "answer-left", "kind": "ask",
                                          "text": "明朝の確かめの前に、台帳に記録してください。",
                                          "handle": "term_left"}) + "\n")
        (self.dir / "answers.jsonl").write_text(json.dumps({"id": "ask-left-0001", "ts": "2026-11-15T19:22:52+09:00",
                                                            "how": "inbox_read.py answered"}) + "\n")
        self.scan = self.dir / "scan.json"
        self.scan.write_text(json.dumps([row("answer-left", claude=claude("2026-11-15T19:22:23+09:00"))]))
        notify = self.dir / "triage-notify"
        notify.write_text("#!/usr/bin/env python3\nimport json, os, sys, uuid\n"
                          "if '明朝' in sys.argv[-1] and '--no-follow-up' not in sys.argv:\n"
                          "    sys.exit('triage-notify: 本文に時刻の約束「明朝」があります（送っていません）')\n"
                          "line = {'id': str(uuid.uuid4()), 'ts': '2026-11-16T10:45:00+09:00', 'kind': 'ask',\n"
                          "        'worktree': os.path.basename(os.getcwd()), 'text': sys.argv[-1],\n"
                          "        'env': os.environ.get('ORCA_WORKTREE_ID', '')}\n"
                          f"open({str(self.inbox)!r}, 'a').write(json.dumps(line, ensure_ascii=False) + '\\n')\n")
        notify.chmod(0o755)
        self.patches = [patch.object(stall, name, value) for name, value in (
            ("INBOX", self.inbox), ("ANSWERS", self.dir / "answers.jsonl"), ("HOLDS", self.dir / "none.sqlite"),
            ("BOARD", self.dir / "none.sqlite3"), ("HOURLY_STATE", self.dir / "none.json"),
            ("REMOTES", self.dir / "none.json"), ("VERDICTS", self.dir / "verdicts.json"),
            ("CHECKS", self.dir / "work-checks.json"), ("NOTIFY", notify), ("SENDER", self.dir / "stall-watch"),
            ("BOARD_CHECK", self.dir / "missing"), ("refreshed", lambda rows, now: rows))]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def run_watch(self, now: datetime) -> int:
        with patch.dict(os.environ, {"ORCA_WORKTREE_ID": "repo::/w/orca/triage-lead"}), \
                patch.object(stall, "datetime", wraps=datetime) as clock, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            clock.now.return_value = now
            return stall.main(["run", "--scan", str(self.scan)])

    def test_new_signals_go_out_as_one_ask_from_stall_watch_and_are_not_sent_again(self) -> None:
        board, taken = self.dir / "board-check", self.dir / "board-records.jsonl"
        board.write_text("#!/usr/bin/env python3\nimport json, sys\nrecord = json.load(sys.stdin)\n"
                         "'tabs' in record and sys.exit('知らない列: tabs')\n"
                         f"open({str(taken)!r}, 'a').write(json.dumps(record) + '\\n')\n")
        board.chmod(0o755)
        with patch.object(stall, "BOARD_CHECK", board):
            self.assertEqual(self.run_watch(NOW), 0)
        self.assertEqual(len(taken.read_text().splitlines()), 2)
        self.assertEqual(self.run_watch(NOW + timedelta(minutes=15)), 0)

        asks = [json.loads(line) for line in self.inbox.read_text().splitlines()][1:]
        self.assertEqual([(item["worktree"], item["env"]) for item in asks], [("stall-watch", "")])
        self.assertIn("answer-left", asks[0]["text"])
        checks = json.loads((self.dir / "work-checks.json").read_text())
        self.assertEqual({(item["signal"], item["status"], item["inbox_id"], tuple(item["tabs"])) for item in checks},
                         {("answer_undelivered", "open", asks[0]["id"], ("term_left",)),
                          ("idle_after_report", "open", asks[0]["id"], ("term_left",))})

    def test_a_lane_woken_after_two_earlier_signals_is_replanned_on_the_third(self) -> None:
        (self.dir / "work-checks.json").write_text(json.dumps([
            {"work": "app/answer-left", "signal": "idle_after_report", "fingerprint": f"earlier-{number}",
             "checked_at": checked_at, "status": "open", "inbox_id": f"stall-{number}"}
            for number, checked_at in enumerate(("2026-11-15T22:00:00+09:00", "2026-11-16T08:00:00+09:00"))]))
        with (self.dir / "answers.jsonl").open("a") as answers:
            answers.writelines(json.dumps({"id": f"stall-{number}", "ts": ts, "how": "send"}) + "\n"
                               for number, ts in enumerate(("2026-11-15T22:05:00+09:00", "2026-11-16T08:05:00+09:00")))
        with sqlite3.connect(self.dir / "board.sqlite3") as db:
            db.executescript("create table events(type, to_actor, ts); create table work_due(node_key);"
                             "create table agents(worktree, state, state_started_at);")
            db.executemany("insert into events values ('send', 'term_left', ?)",
                           [("2026-11-15T22:05:00+09:00",), ("2026-11-16T08:05:00+09:00",)])

        with patch.object(stall, "BOARD", self.dir / "board.sqlite3"):
            self.assertEqual(self.run_watch(NOW), 0)

        ask = json.loads(self.inbox.read_text().splitlines()[-1])
        self.assertIn("11/15 22:05・11/16 08:05 に立て直した。3 回目はせず、この作業を外して計画を立て直す", ask["text"])

    def test_a_verdict_read_before_the_lane_spoke_again_does_not_explain_its_quiet(self) -> None:
        saved = json.loads(self.scan.read_text())[0]
        (self.dir / "verdicts.json").write_text(json.dumps(
            {saved["path"]: {"class": "waiting", "read": stall.scan.read_key(saved)}}))
        spoke_again = [{**saved, "claude": claude("2026-11-15T20:10:00+09:00")}]

        with patch.object(stall, "refreshed", lambda rows, now: spoke_again):
            self.assertEqual(self.run_watch(NOW), 0)

        checks = json.loads((self.dir / "work-checks.json").read_text())
        self.assertIn("idle_after_report", {item["signal"] for item in checks})


if __name__ == "__main__":
    unittest.main()
