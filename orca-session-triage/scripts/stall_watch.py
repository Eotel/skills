#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Find Orca lanes that look stopped but need someone, by fixed rules, and wake the lead with one inbox ask.

usage: stall_watch.py run [--scan SCAN_JSON] [--dry-run]
       stall_watch.py replay --scan SCAN_JSON --now TIME
       stall_watch.py stopped-at PATH [PATH ...]

run      reads the newest saved scan (~/.cache/orca-session-triage/reads/*/scan.json, or --scan), reads this Mac's
         transcripts, rollouts, and background processes again, and judges every row. New signals go to the triage
         inbox as one `ask` from the worktree `stall-watch` (triage-notify run in ~/.cache/orca-briefs/stall-watch
         without ORCA_WORKTREE_ID or ORCA_TERMINAL_HANDLE), and to the work_check table through
         ~/.cache/orca-briefs/bin/orca_board_commitments.py check when that command exists. Every signal is kept in
         ~/.cache/orca-session-triage/work-checks.json, and one already there (same work, signal, and fingerprint)
         is never sent again. --dry-run prints the ask and writes nothing.
replay   judges a saved scan at a past moment from the records as they stood then (inbox, answers, sends, holds),
         with no process, forge, or GitHub read, and prints one line per signal. It writes nothing.
stopped-at prints, per worktree path, when its agent last did anything and how that was measured (lanes-idle).

Signals:
  watcher_outlived     a wait loop whose command runs `sleep` has run an hour or more, and the PR it names is merged
                       or closed
  answer_undelivered   the lane's ask has an answer recorded, nothing was sent to the lane since the ask, and the
                       lane has not moved since the answer
  promise_unscheduled  the lane's last report or final message promises work at a time that has passed (明朝,
                       明日午前, 11/16 の朝, 今夜, 18:00 に), no hold, open handoff, work_due, Orca automation, or
                       one-shot schedule keeps it, and the lane has not moved since that time
  wait_resolved        the saved verdict waits on an outside state that now holds: a date that has come, another Mac
                       the scan reaches, or a forge `git ls-remote` reaches
  api_error_turn       the newest copy of the lane's transcript ends in an API error that is not a usage limit,
                       fifteen minutes or more ago
  idle_after_report    the lane's last report is an info or an answered ask, its agent has done nothing for two
                       hours, and no open question, active hold, live background job, or Orca automation explains it
  busy_without_progress  the lane's agent has called tools without a break of 30 minutes, at 30 calls an hour or more,
                       for 90 minutes since its last report, successful commit, push, PR or issue write, or user
                       message (Claude's transcript or Codex's rollout, read from the end); a lane with an open ask
                       or an Orca automation is left out

Recovery: api_error_turn, idle_after_report, and busy_without_progress name the step by cause: a lost connection gets
the same work continued, a full context window (stop kind context_limit) a new lane with a smaller scope, and a run whose
tool calls failed half the time or more a lane of the other agent (Claude for Codex, Codex for Claude). A recovery is an
earlier one of those signals of the same work in work-checks.json whose ask the lead answered with a send (`send --ask`)
that reached the lane's tabs (or the tabs its reports came from) within a minute, or closed with `inbox_read.py answered
<id> --note "立て直し: <old worktree> → <new worktree>"`; the new worktree's lane carries the work's recoveries on. Once a work has had two,
the signal says to take the work out and replan instead of a third recovery.

When the agent last did anything: Claude's newest transcript entry (the newest copy across Claude accounts), Codex's
newest assistant message in its rollout, else the board's done time for the agent, else the lane's last report;
never Orca's lastOutputAt, which a status line redraws every 30 seconds.

Exit status: 0 the check finished, 1 a record could not be read or the inbox write failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Literal, Protocol
from urllib.parse import urlparse

import inbox_read
import scan_sessions as scan

HOME = Path.home()
READS = HOME / ".cache/orca-session-triage/reads"
CHECKS = HOME / ".cache/orca-session-triage/work-checks.json"
CPU_FILE = HOME / ".cache/orca-session-triage/stall-watch-cpu.json"
HOURLY_STATE = HOME / ".cache/orca-session-triage/hourly-state.json"
VERDICTS = HOME / ".cache/orca-session-triage/verdicts.json"
BRIEFS = HOME / ".cache/orca-briefs"
INBOX = BRIEFS / "triage-inbox.jsonl"
ANSWERS = BRIEFS / "ask-answers.jsonl"
HOLDS = BRIEFS / "holds.sqlite"
REMOTES = BRIEFS / "remote-inboxes.json"
NOTIFY = BRIEFS / "bin/triage-notify"
BOARD_CHECK = BRIEFS / "bin/orca_board_commitments.py"
SENDER = BRIEFS / "stall-watch"
BOARD = HOME / ".local/share/orca-board/board.sqlite3"

SIGNALS = {
    "watcher_outlived": "見張りの長生き",
    "answer_undelivered": "答えの未配達",
    "promise_unscheduled": "時刻の約束が置き場に無い",
    "wait_resolved": "外の待ち先が解けた",
    "api_error_turn": "エラーで終わった turn",
    "idle_after_report": "報告の後の放置",
    "busy_without_progress": "動き続けて進まない",
}
WATCHER_AFTER = timedelta(hours=1)
IDLE_AFTER = timedelta(hours=2)
BUSY_AFTER = timedelta(minutes=90)
BUSY_GAP = timedelta(minutes=30)
BUSY_CALLS_PER_HOUR = 30
TAIL_CHUNK = 1 << 20
TAIL_LIMIT = 64 << 20
GRACE = timedelta(minutes=15)
PROMISE_HORIZON = timedelta(days=60)
CODEX_SINCE = timedelta(days=2)
PROBE_TIMEOUT = 15
DELIVERED = ("send", "send --decision")
WITHDRAWN = "triage-notify --withdraws"
STOP_KINDS = ("connection", "context_limit", "other", "login")
RECOVERED = ("api_error_turn", "idle_after_report", "busy_without_progress")
MAX_RECOVERIES = 2
STEPS = {"connection": "通信断。同じ続きを送る（止まったところから続ける 1 行）",
         "context_limit": "文脈の上限。範囲を小さくして新しい担当に渡す（残りの作業だけの指示書で spawn-lane し、今の担当は止める）",
         "tool_error": "道具のエラー。画面で同じ道具の失敗が続いていれば {other} の担当に移す（同じ作業を {other} で"
                       " spawn-lane し、今の担当は止める）"}
HANDED_TO = re.compile(r"立て直し[:：]\s*([\w.-]+)\s*(?:→|->)\s*([\w.-]+)")
SEND_LANDS = timedelta(minutes=1)
PERSON_STEPS = {"login": "人がログインし直す（起こしても直らない）", "other": "人がエラーを読む（起こしても同じエラーになる）"}
DECISION_MARKER = "要判断"
MORNING, AFTERNOON, EVENING, NIGHT = 9, 13, 17, 21

PR_REF = re.compile(r"gh\s+pr\s+(?:checks|view|status|merge)\s+(\d+)|/pull/(\d+)")
REPO_FLAG = re.compile(r"(?:-R|--repo)[ =]([\w.-]+/[\w.-]+)|github\.com/([\w.-]+/[\w.-]+)/pull/")
SENTENCE = re.compile(r"[^。\n]+[。]?")
QUOTED = re.compile(r"「[^」]*」|『[^』]*』|`[^`]*`")
PROMISE_VERB = re.compile(r"(?:行い|進め|確かめ|確認し|対応し|流し|再開し|直し|送り|知らせ|伺い|見直し|やり|し)ます(?:。|$)")
NOT_PROMISE = re.compile(r"ました|でした|予定です|ません|ですか|ますか|お願い|ください|いただ|提案")
DAY_PART = {"朝": MORNING, "午前": MORNING, "午前中": MORNING, "昼": 12, "午後": AFTERNOON, "夕方": EVENING,
            "夜": NIGHT}
DATED = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])\s*(?:の)?\s*(朝|午前中|午前|昼|午後|夕方|夜)?")
TOMORROW = re.compile(r"明朝|翌朝|明日\s*の?\s*(朝|午前中|午前|昼|午後|夕方|夜)?")
TONIGHT = re.compile(r"今夜|今晩")
DEADLINE = re.compile(r"\s*(?:まで|より前|の?前|以前)")
CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])\s*(?:に|から|以降|ごろ|頃)")
WAIT_DATE = re.compile(r"(?<![\d/])(?:(\d{4})[-/])?(\d{1,2})[/-](\d{1,2})(?![\d/])|(\d{1,2})月(\d{1,2})日")
FORGE_WORDS = ("forge", "forgejo", "gitea")
PROGRESS_COMMAND = re.compile(r"\bgit\s+(?:-[Cc]\s+\S+\s+)*(?:commit|push)\b"
                              r"|\bgh\s+(?:pr|issue)\s+(?:create|merge|comment|review|edit|close)\b|triage-notify")
EXIT_CODE = re.compile(r'\\*"exit_code\\*"\s*:\s*(\d+)')


MeasuredBy = Literal["transcript", "rollout", "agent_state", "report"]
WaitKind = Literal["pr", "issue", "time", "event", "external", "person"]


@dataclass(frozen=True)
class Report:
    id: str
    kind: str
    at: datetime
    text: str
    handle: str
    questions: int = 0


@dataclass(frozen=True)
class Answer:
    at: datetime
    how: str
    note: str = ""


@dataclass(frozen=True)
class EarlierStop:
    at: datetime
    ask: str


@dataclass(frozen=True)
class Activity:
    at: datetime | None
    by: MeasuredBy


@dataclass(frozen=True)
class Records:
    reports_by_worktree: Mapping[str, tuple[Report, ...]] = field(default_factory=dict)
    answers_by_ask: Mapping[str, tuple[Answer, ...]] = field(default_factory=dict)
    sends_by_handle: Mapping[str, tuple[datetime, ...]] = field(default_factory=dict)
    held_handles: frozenset[str] = frozenset()
    open_handoff_names: frozenset[str] = frozenset()
    due_worktrees: frozenset[str] = frozenset()
    done_at_by_worktree: Mapping[str, datetime] = field(default_factory=dict)
    stops_by_work: Mapping[str, tuple[EarlierStop, ...]] = field(default_factory=dict)
    reached_environments: frozenset[str] = frozenset()
    environment_by_alias: Mapping[str, str] = field(default_factory=dict)
    lead_path: str | None = None


class Probe(Protocol):
    def command(self, pid: int, saved: str) -> str: ...
    def origin(self, path: str) -> str | None: ...
    def pr_state(self, slug: str, number: int) -> str | None: ...
    def forge_reachable(self, path: str) -> bool | None: ...


@dataclass(frozen=True)
class Signal:
    work: str
    host: str
    signal: str
    fingerprint: str
    reason: str
    path: str
    stopped_at: str | None = None
    stopped_by: str | None = None
    last_report_kind: str | None = None
    last_report_at: str | None = None
    wait_kind: str | None = None
    wait_target: str | None = None
    wait_resolved: bool | None = None
    watcher_pid: int | None = None
    watcher_command: str | None = None
    watcher_started_at: str | None = None
    watcher_pr: str | None = None

    def record(self, checked_at: datetime, inbox_id: str | None) -> dict:
        fields = {key: value for key, value in asdict(self).items() if key != "path"}
        return {**fields, "checked_at": iso(checked_at), "status": "open", "handled_by": None, "inbox_id": inbox_id}


def iso(at: datetime | None) -> str | None:
    return at.isoformat(timespec="seconds") if at else None


def parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.astimezone()


def short(at: datetime | None) -> str:
    return at.astimezone().strftime("%m/%d %H:%M") if at else "不明"


def work_name(row: Mapping) -> str:
    """repo/name, qualified as `<environment>::repo/name` for another Mac's lane, as scan's row_name does."""
    name = Path(row["path"]).name
    work = f"{row['repo']}/{name}" if row.get("repo") else name
    return f"{row['environment']}::{work}" if row.get("environment") else work


def lane_reports(row: Mapping, records: Records) -> tuple[Report, ...]:
    return records.reports_by_worktree.get(Path(row["path"]).name, ())


def handles(row: Mapping) -> set[str]:
    return {term["handle"] for term in row.get("terminals") or [] if term.get("handle")}


def activity(row: Mapping, records: Records, live: bool) -> Activity:
    """When the agent last did anything, and from which record."""
    claude = row.get("claude") or {}
    found: list[tuple[datetime, MeasuredBy]] = []
    transcript = Path(claude["transcript"]) if claude.get("transcript") else None
    if live and transcript and transcript.is_file():
        try:
            if at := scan.last_entry_at(transcript):
                found.append((at, "transcript"))
        except OSError:
            pass
    found += [(at, "transcript") for key in ("final_at", "last_user_at") if (at := parse(claude.get(key)))]
    found += [(at, "rollout") for rollout in row.get("codex") or [] if (at := parse(rollout.get("last_assistant_at")))]
    if found:
        at, by = max(found)
        return Activity(at, by)
    if done := records.done_at_by_worktree.get(Path(row["path"]).name):
        return Activity(done, "agent_state")
    if reports := lane_reports(row, records):
        return Activity(reports[-1].at, "report")
    return Activity(None, "report")


def moved_since(act: Activity, moment: datetime) -> bool:
    return act.at is not None and act.at > moment


@dataclass(frozen=True)
class Wait:
    kind: WaitKind
    target: str


@dataclass(frozen=True)
class Watcher:
    pid: int
    command: str
    started: datetime
    pr: str


def base_signal(row: Mapping, act: Activity, records: Records, kind: str, fingerprint: str, reason: str,
                wait: Wait | None = None, watcher: Watcher | None = None) -> Signal:
    reports = lane_reports(row, records)
    last = reports[-1] if reports else None
    return Signal(work=work_name(row), host=row.get("environment") or socket.gethostname(), signal=kind,
                  fingerprint=fingerprint, reason=reason, path=row["path"], stopped_at=iso(act.at),
                  stopped_by=act.by, last_report_kind=last.kind if last else None,
                  last_report_at=iso(last.at) if last else None, wait_kind=wait.kind if wait else None,
                  wait_target=wait.target if wait else None, wait_resolved=True if wait else None,
                  watcher_pid=watcher.pid if watcher else None,
                  watcher_command=watcher.command if watcher else None,
                  watcher_started_at=iso(watcher.started) if watcher else None,
                  watcher_pr=watcher.pr if watcher else None)


def watcher_signals(row: Mapping, act: Activity, records: Records, now: datetime, probe: Probe) -> list[Signal]:
    found = []
    for process in row.get("processes") or []:
        elapsed = process.get("elapsed")
        if not isinstance(elapsed, (int, float)) or elapsed < WATCHER_AFTER.total_seconds():
            continue
        command = probe.command(process["pid"], process.get("command") or "")
        refs = [int(a or b) for a, b in PR_REF.findall(command)]
        if "sleep" not in command or not refs:
            continue
        named = REPO_FLAG.search(command)
        slug = (named[1] or named[2]) if named else scan.github_slug(probe.origin(row["path"]))
        if not slug:
            continue
        number = refs[0]
        state = probe.pr_state(slug, number)
        if state not in ("MERGED", "CLOSED"):
            continue
        started = now - timedelta(seconds=elapsed)
        found.append(base_signal(
            row, act, records, "watcher_outlived", f"{process['pid']}@{started:%Y%m%dT%H%M}",
            f"PID {process['pid']} の待ちの loop が {elapsed / 3600:.0f} 時間動いているが、名指す {slug}#{number} は"
            f" {state.lower()}。loop を止めてよいか確かめる",
            Wait("pr", f"{slug}#{number}"), Watcher(process["pid"], command[:300], started, f"{slug}#{number}")))
    return found


def covering_answers(ask: Report, records: Records) -> list[Answer]:
    """The answers that settle the ask: its own, or one for every structured question (inbox_read.open_asks's rule)."""
    keys = ([ask.id] if ask.id in records.answers_by_ask
            else [f"{ask.id}:{number}" for number in range(1, ask.questions + 1)])
    return ([answer for key in keys for answer in records.answers_by_ask[key]]
            if keys and all(key in records.answers_by_ask for key in keys) else [])


def answer_signals(row: Mapping, act: Activity, records: Records, now: datetime) -> list[Signal]:
    found = []
    for ask in (report for report in lane_reports(row, records) if report.kind == "ask"):
        answers = [answer for key, given in records.answers_by_ask.items()
                   if key == ask.id or key.startswith(ask.id + ":") for answer in given]
        covering = covering_answers(ask, records)
        if not covering or any(answer.how in DELIVERED or answer.how == WITHDRAWN for answer in answers):
            continue
        answered = max(answer.at for answer in covering)
        sent = [at for handle in {ask.handle, *handles(row)} for at in records.sends_by_handle.get(handle, ()) if at > ask.at]
        if sent or now - answered < GRACE or moved_since(act, answered):
            continue
        found.append(base_signal(
            row, act, records, "answer_undelivered", ask.id,
            f"ask {ask.id[:8]}（{short(ask.at)}）の答えが {short(answered)} に記録されたが、その後この担当へ何も送っていない",
            Wait("person", f"ask {ask.id[:8]}")))
    return found


def ahead_of_saying(due: datetime, said: datetime) -> bool:
    """A date already past when said is a reference (「11/05 の判断どおり」); one months ahead is a fraction (「手順 3/4」)."""
    return said < due <= said + PROMISE_HORIZON


def promise_due(text: str, said: datetime) -> datetime | None:
    """When the earliest promise of the speaker's own work in the text falls due, read against the time it was said.
    A sentence with several times promises the work at its latest one (「今夜の待機を終了し、11/16 の朝に行います」)."""
    local = said.astimezone()
    promised = []
    for sentence in SENTENCE.findall(text):
        plain = QUOTED.sub("", sentence)
        if plain.lstrip().startswith(">") or not PROMISE_VERB.search(plain) or NOT_PROMISE.search(plain):
            continue
        days = []
        for month, day, part in (match.groups() for match in DATED.finditer(plain)
                                 if not DEADLINE.match(plain, match.end())):
            try:
                at = local.replace(month=int(month), day=int(day), hour=DAY_PART.get(part, MORNING), minute=0,
                                   second=0, microsecond=0)
            except ValueError:
                continue
            days.append(at.replace(year=at.year + 1) if at < local - PROMISE_HORIZON else at)
        for match in TOMORROW.finditer(plain):
            days.append((local + timedelta(days=1)).replace(hour=DAY_PART.get(match[1] or "", MORNING), minute=0,
                                                            second=0, microsecond=0))
        if TONIGHT.search(plain):
            days.append(local.replace(hour=NIGHT, minute=0, second=0, microsecond=0))
        clocks = [(int(hour), int(minute)) for hour, minute in CLOCK.findall(plain) if int(hour) < 24 and int(minute) < 60]
        dues = ([at.replace(hour=hour, minute=minute) for at in days or [local.replace(second=0, microsecond=0)] for hour, minute in clocks]
                if clocks else days)
        if dues := [at for at in dues if ahead_of_saying(at, local)]:
            promised.append(max(dues))
    return min(promised, default=None)


def kept_elsewhere(row: Mapping, records: Records) -> bool:
    """A hold, an open handoff, a work_due, an Orca automation, or a one-shot schedule keeps the lane's next step."""
    claude = row.get("claude") or {}
    return bool(handles(row) & records.held_handles or scan.handoff_names(row) & records.open_handoff_names
                or Path(row["path"]).name in records.due_worktrees or row.get("automation_workspaces")
                or claude.get("one_shot_schedules"))


def promise_signals(row: Mapping, act: Activity, records: Records, now: datetime) -> list[Signal]:
    reports = lane_reports(row, records)
    said: list[tuple[str, str, datetime]] = [(reports[-1].id, reports[-1].text, reports[-1].at)] if reports else []
    claude, codex = row.get("claude") or {}, (row.get("codex") or [{}])[-1]
    if claude.get("final_text") and (at := parse(claude.get("final_at"))):
        said.append(("transcript", claude["final_text"], at))
    if codex.get("last_assistant") and (at := parse(codex.get("last_assistant_at"))):
        said.append(("rollout", codex["last_assistant"], at))
    if kept_elsewhere(row, records):
        return []
    for source, text, at in sorted(said, key=lambda item: item[2], reverse=True):
        due = promise_due(text, at)
        if due is None or now < due or moved_since(act, due):
            continue
        line = next(sentence.strip() for sentence in SENTENCE.findall(text) if promise_due(sentence, at) == due)
        return [base_signal(
            row, act, records, "promise_unscheduled", f"{source[:8]}@{due:%Y%m%dT%H%M}",
            f"{short(at)} に「{line[:120]}」と書いたが、{short(due)} を過ぎても動いておらず、hold・引き継ぎ・期日・"
            "定期実行のどれにも入っていない",
            Wait("time", iso(due) or ""))]
    return []


def wait_dates(text: str, around: datetime) -> list[datetime]:
    local = around.astimezone()
    dates = []
    for year, month, day, kanji_month, kanji_day in WAIT_DATE.findall(text):
        try:
            at = local.replace(year=int(year) if year else local.year, month=int(month or kanji_month),
                               day=int(day or kanji_day), hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            continue
        dates.append(at)
    return dates


def came_while_quiet(date: datetime, act: Activity, now: datetime) -> bool:
    """A date the lane had passed before it went quiet is when its wait began, not what it waits for."""
    return (act.at is None or date > act.at) and date <= now


def mentioned_macs(text: str, records: Records) -> list[str]:
    lowered = text.lower()
    names = {**records.environment_by_alias, **{env: env for env in records.reached_environments}}
    return sorted({env for name, env in names.items() if name and name.lower() in lowered})


def origin_host(origin: str | None) -> str:
    return (urlparse(origin).hostname or origin.rpartition("@")[2].partition(":")[0]) if origin else ""


def forge_named(text: str, origin: str | None) -> bool:
    """The wait names the worktree's own forge: by a word for one, or by its origin's host or that host's name."""
    lowered = text.lower()
    host = origin_host(origin)
    if not host or host == "github.com":
        return False
    labels = host.split(".")
    return any(word in lowered for word in FORGE_WORDS) or host in lowered or (len(labels) > 1 and labels[-2] in lowered)


def wait_signals(row: Mapping, verdict: Mapping | None, act: Activity, records: Records, now: datetime,
                 probe: Probe) -> list[Signal]:
    waits_on = (verdict or {}).get("waits_on") or ""
    if not waits_on:
        return []
    found: list[tuple[WaitKind, str, str]] = []
    if dates := [at for at in wait_dates(waits_on, act.at or now) if came_while_quiet(at, act, now)]:
        found.append(("time", short(max(dates))[:5], "待ち先の日付が来た"))
    for env in mentioned_macs(waits_on, records):
        if env in records.reached_environments and env != row.get("environment"):
            found.append(("external", env, f"待ち先の Mac（{env}）に今は届く"))
    if not row.get("environment") and forge_named(waits_on, probe.origin(row["path"])) and probe.forge_reachable(row["path"]):
        found.append(("external", "origin", "待ち先の forge に今は git ls-remote が届く"))
    return [base_signal(row, act, records, "wait_resolved", f"{kind}:{target}:{waits_on[:40]}",
                        f"判定の待ち先「{waits_on[:100]}」: {why}", Wait(kind, target))
            for kind, target, why in found]


def recoveries(row: Mapping, records: Records, now: datetime) -> list[datetime]:
    """When each earlier stop of the work was recovered: its ask answered by a send that reached the lane within a
    minute, or closed as handed to a new lane (「立て直し: <old worktree> → <new worktree>」), whose stops then count as
    the same work's."""
    def tabs(name: str) -> set[str]:
        return {report.handle for report in records.reports_by_worktree.get(name, ()) if report.handle}

    def answered(stop: EarlierStop) -> tuple[Answer, ...]:
        return tuple(answer for answer in records.answers_by_ask.get(stop.ask, ()) if answer.at <= now)

    def handoffs(work: str, stop: EarlierStop) -> list[tuple[Answer, str]]:
        return [(answer, new) for answer in answered(stop) if answer.how == "inbox_read.py answered"
                for old, new in HANDED_TO.findall(answer.note) if old == work.rpartition("/")[2]]

    handed = {new: work for work, stops in records.stops_by_work.items() for stop in stops
              for _, new in handoffs(work, stop)}
    chain, name = [work_name(row)], Path(row["path"]).name
    while (earlier := handed.get(name)) and earlier not in chain:
        chain.append(earlier)
        name = earlier.rpartition("/")[2]
    found = set()
    for work in chain:
        lane = tabs(work.rpartition("/")[2]) | (handles(row) if work == chain[0] else set())
        sends = [at for tab in lane for at in records.sends_by_handle.get(tab, ())]
        for stop in {stop.ask: stop for stop in records.stops_by_work.get(work, ()) if stop.at <= now}.values():
            found.update(answer.at for answer, _ in handoffs(work, stop))
            found.update(sent for answer in answered(stop) if answer.how in DELIVERED
                         and (sent := next((at for at in sends if abs(at - answer.at) <= SEND_LANDS), None)))
    return sorted(found)


def recovery(row: Mapping, records: Records, now: datetime, step: str | None) -> str:
    """The step a stop of this cause gets, or replanning once the work has been recovered MAX_RECOVERIES times."""
    done = recoveries(row, records, now)
    if len(done) >= MAX_RECOVERIES:
        return (f"。{'・'.join(short(at) for at in done)} に立て直した。{len(done) + 1} 回目はせず、"
                "この作業を外して計画を立て直す")
    return f"。立て直し（{len(done) + 1} 回目）: {step}" if step else ""


def error_signals(row: Mapping, act: Activity, records: Records, now: datetime) -> list[Signal]:
    stop = row.get("stop")
    if not stop or stop.get("kind") not in STOP_KINDS:
        return []
    if act.at is None or now - act.at < GRACE:
        return []
    then = (f"。{PERSON_STEPS[stop['kind']]}" if stop["kind"] in PERSON_STEPS
            else recovery(row, records, now, STEPS[stop["kind"]]))
    return [base_signal(row, act, records, "api_error_turn", f"{act.by}@{act.at:%Y%m%dT%H%M%S}",
                        f"最後の turn が {short(act.at)} に API エラー（{stop['kind']}）で終わった: "
                        f"{stop['error'][:100]}{then}")]


def waits_explained(row: Mapping, verdict: Mapping | None, records: Records, now: float) -> bool:
    claude = row.get("claude") or {}
    asking = ((verdict or {}).get("class") == "waiting" or row.get("prompt_waiting")
              or DECISION_MARKER in (row.get("comment") or "") or claude.get("pending_questions")
              or any(not request.get("answered_in_chat_later") for session in row.get("codex") or []
                     for request in session.get("pending_requests") or []))
    ends = {process["parent"] for process in row.get("processes") or []}
    live_job = any(not scan.stopped(process, now) for process in row.get("processes") or []
                   if process["pid"] not in ends)
    return bool(asking or live_job or kept_elsewhere(row, records))


def idle_signals(row: Mapping, verdict: Mapping | None, act: Activity, records: Records,
                 now: datetime) -> list[Signal]:
    reports = lane_reports(row, records)
    if not reports or act.at is None or now - act.at < IDLE_AFTER:
        return []
    last = reports[-1]
    asking = [report for report in reports if report.kind == "ask" and report.id
              and not inbox_read.is_handoff({"text": report.text}) and not covering_answers(report, records)]
    if asking or last.kind not in ("info", "ask") or waits_explained(row, verdict, records, now.timestamp()):
        return []
    return [base_signal(row, act, records, "idle_after_report", f"{last.id[:8]}@{act.at:%Y%m%dT%H%M}",
                        f"最後の報告は {short(last.at)} の {last.kind}（「{' '.join(last.text.split())[:80]}」）。"
                        f"{short(act.at)} から動いておらず、待ち・hold・裏の作業・定期実行のどれも無い"
                        f"{recovery(row, records, now, None)}")]


@dataclass(frozen=True)
class Run:
    """The lane's current stretch of tool calls since its last progress, or since it started working without a break."""
    since: datetime
    calls: int
    source: str
    whole: bool
    agent: str
    failed: int


def lines_backwards(path: Path) -> Iterator[bytes]:
    """The file's lines from the newest, reading at most TAIL_LIMIT bytes; the last item is b"" when it stopped early."""
    with path.open("rb") as handle:
        size = end = handle.seek(0, os.SEEK_END)
        rest = b""
        while end > 0:
            if size - end >= TAIL_LIMIT:
                yield b""
                return
            start = max(0, end - TAIL_CHUNK)
            handle.seek(start)
            lines = (handle.read(end - start) + rest).split(b"\n")
            end = start
            rest = lines.pop(0) if start else b""
            yield from (line for line in reversed(lines) if line.strip())


def claude_steps(entry: Mapping, done: dict[str, datetime]) -> Iterator[tuple[str, datetime]]:
    """Steps of one transcript entry, read newest first: a tool result is seen before the call that made it."""
    at = parse(entry.get("timestamp"))
    content = (entry.get("message") or {}).get("content")
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    if at is None or entry.get("isSidechain"):
        return
    if entry.get("type") == "user":
        if scan.is_real_user_message(dict(entry)):
            yield "progress", at
        results = [block for block in blocks if block.get("type") == "tool_result"]
        done.update({str(block.get("tool_use_id")): at for block in results if not block.get("is_error")})
        yield from (("failed", at) for block in results if block.get("is_error"))
    elif entry.get("type") == "assistant":
        yield "turn", at
        for block in (block for block in blocks if block.get("type") == "tool_use"):
            yield "call", at
            command = str((block.get("input") or {}).get("command") or "")
            if (finished := done.get(str(block.get("id")))) and PROGRESS_COMMAND.search(command):
                yield "progress", finished


def codex_steps(entry: Mapping, done: dict[str, datetime]) -> Iterator[tuple[str, datetime]]:
    payload = entry.get("payload")
    at = parse(entry.get("timestamp"))
    if at is None or entry.get("type") != "response_item" or not isinstance(payload, dict):
        return
    kind = payload.get("type")
    if kind in ("function_call_output", "custom_tool_call_output"):
        codes = EXIT_CODE.findall(str(payload.get("output") or ""))
        if codes and all(code == "0" for code in codes):
            done[str(payload.get("call_id"))] = at
        elif codes:
            yield "failed", at
    elif kind in ("function_call", "custom_tool_call"):
        yield "turn", at
        yield "call", at
        command = str(payload.get("input") or payload.get("arguments") or "")
        if (finished := done.get(str(payload.get("call_id")))) and PROGRESS_COMMAND.search(command):
            yield "progress", finished
    elif kind == "message" and payload.get("role") == "assistant":
        yield "turn", at
    elif kind == "message" and payload.get("role") == "user":
        text = " ".join(str(part.get("text") or "") for part in payload.get("content") or [] if isinstance(part, dict))
        if text.strip() and not text.lstrip().startswith("<"):
            yield "progress", at


def current_run(path: Path, agent: str, now: datetime, floor: datetime | None) -> Run | None:
    """Walk back from the newest entry to the last progress, a break longer than BUSY_GAP, or `floor` (a report)."""
    steps = claude_steps if agent == "claude" else codex_steps
    done: dict[str, datetime] = {}
    calls, failed, newest, oldest = 0, 0, None, None
    try:
        for line in lines_backwards(path):
            if not line:
                return Run(oldest, calls, path.stem[-8:], False, agent, failed) if oldest else None
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or (at := parse(entry.get("timestamp"))) is None or at > now:
                continue
            for step, when in steps(entry, done):
                if step == "progress" or (floor and when <= floor):
                    return Run(max(when, floor or when), calls, path.stem[-8:], True, agent, failed) if newest else None
                if step == "turn":
                    if newest is None and now - when > GRACE:
                        return None
                    if oldest and oldest - when > BUSY_GAP:
                        return Run(oldest, calls, path.stem[-8:], True, agent, failed)
                    newest, oldest = newest or when, when
                elif step == "call":
                    calls += 1
                elif step == "failed":
                    failed += 1
    except OSError:
        return None
    return Run(oldest, calls, path.stem[-8:], True, agent, failed) if oldest else None


def busy_signals(row: Mapping, act: Activity, records: Records, now: datetime) -> list[Signal]:
    reports = [report for report in lane_reports(row, records) if report.at <= now]
    asking = [report for report in reports if report.kind == "ask" and report.id
              and not inbox_read.is_handoff({"text": report.text}) and not covering_answers(report, records)]
    if asking or row.get("automation_workspaces"):
        return []
    claude = row.get("claude") or {}
    rollouts = sorted((item for item in row.get("codex") or [] if item.get("rollout")),
                      key=lambda item: parse(item.get("last_assistant_at")) or datetime.min.replace(tzinfo=timezone.utc))
    sources = [(Path(claude["transcript"]), "claude")] if claude.get("transcript") else []
    sources += [(Path(rollouts[-1]["rollout"]), "codex")] if rollouts else []
    floor = reports[-1].at if reports else None
    runs = [run for path, agent in sources if path.is_file() and (run := current_run(path, agent, now, floor))]
    run = max(runs, key=lambda item: item.calls, default=None)
    if run is None or now - run.since < BUSY_AFTER:
        return []
    minutes = (now - run.since) / timedelta(minutes=1)
    if run.calls < BUSY_CALLS_PER_HOUR * minutes / 60:
        return []
    since = f"{run.since.astimezone():%Y%m%dT%H%M}" if run.whole else "tail"
    said = (f"{short(run.since)} から {minutes:.0f} 分、tool を {run.calls} 回使い続けているが、報告・commit・push・PR・"
            "人の入力が無い")
    if run.failed * 2 >= run.calls:
        other = "Codex" if run.agent == "claude" else "Claude"
        reason = f"{said}。うち {run.failed} 回は失敗した{recovery(row, records, now, STEPS['tool_error'].format(other=other))}"
    else:
        reason = f"{said}。担当に聞かず画面を読み、空回りなら止めるか立て直す{recovery(row, records, now, None)}"
    return [base_signal(row, act, records, "busy_without_progress", f"{run.source}@{since}", reason)]


def judge(row: Mapping, verdict: Mapping | None, records: Records, now: datetime, probe: Probe,
          live: bool = True) -> list[Signal]:
    if records.lead_path and row["path"] == records.lead_path:
        return []
    act = activity(row, records, live)
    return [*watcher_signals(row, act, records, now, probe), *answer_signals(row, act, records, now),
            *promise_signals(row, act, records, now), *wait_signals(row, verdict, act, records, now, probe),
            *error_signals(row, act, records, now), *idle_signals(row, verdict, act, records, now),
            *busy_signals(row, act, records, now)]


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [item for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            for item in [safe_json(line)] if isinstance(item, dict)]


def safe_json(line: str) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None


def load_records(now: datetime, rows: Sequence[Mapping]) -> Records:
    """The records as they stood at `now`; a later entry is left out so a replay sees that moment."""
    inbox, answers, holds, board, hourly_state, remotes = INBOX, ANSWERS, HOLDS, BOARD, HOURLY_STATE, REMOTES
    reports: dict[str, list[Report]] = {}
    for item in read_jsonl(inbox):
        at = parse(item.get("ts"))
        if at and at <= now and item.get("worktree") and item.get("kind") in ("ask", "done", "info"):
            reports.setdefault(item["worktree"], []).append(
                Report(str(item.get("id") or ""), item["kind"], at, str(item.get("text") or ""),
                       str(item.get("handle") or ""), len(inbox_read.questions_of(item))))
    given: dict[str, list[Answer]] = {}
    for item in read_jsonl(answers):
        at = parse(item.get("ts"))
        if at and at <= now and item.get("id"):
            given.setdefault(str(item["id"]), []).append(Answer(at, str(item.get("how") or ""), str(item.get("note") or "")))
    sends: dict[str, list[datetime]] = {}
    held: set[str] = set()
    due: set[str] = set()
    agent_done: dict[str, datetime] = {}
    if board.is_file():
        with sqlite3.connect(f"file:{board}?mode=ro", uri=True) as db:
            for to, ts in db.execute("select to_actor, ts from events where type = 'send'"):
                if (at := parse(ts)) and at <= now and to:
                    sends.setdefault(to, []).append(at)
            due = {key.rsplit(":", 1)[-1] for (key,) in db.execute("select node_key from work_due")}
            for worktree, state, since in db.execute("select worktree, state, state_started_at from agents"):
                if state == "done" and isinstance(since, (int, float)) and worktree:
                    agent_done[worktree] = datetime.fromtimestamp(since / 1000, timezone.utc)
    if holds.is_file():
        with sqlite3.connect(f"file:{holds}?mode=ro", uri=True) as db:
            stamp = now.timestamp()
            held = {handle for handle, created, released, status in db.execute(
                "select handle, created_at, released_at, status from holds")
                if created <= stamp and (status == "active" if released is None else released > stamp)}
    stops: dict[str, list[EarlierStop]] = {}
    for item in load_checks():
        if item.get("signal") in RECOVERED and (at := parse(item.get("checked_at"))) and at <= now:
            stops.setdefault(str(item.get("work")), []).append(EarlierStop(at, str(item.get("inbox_id") or "")))
    state = scan.read_json_file(hourly_state) if hourly_state.is_file() else {}
    state = state if isinstance(state, dict) else {}
    handoffs = state.get("handoffs") or {}
    open_names = {name for item in (handoffs.values() if isinstance(handoffs, dict) else handoffs)
                  if isinstance(item, dict) and not item.get("delivered_at") for name in (item.get("to"), item.get("from"))
                  if name}
    remote = scan.read_json_file(remotes) if remotes.is_file() else {}
    hosts = remote.get("hosts") if isinstance(remote, dict) else None
    aliases = {}
    for name, host in (hosts if isinstance(hosts, dict) else {}).items():
        target = str(host.get("ssh_target") or "").rpartition("@")[2].split(".")[0]
        if target:
            aliases[name] = target
    return Records(
        reports_by_worktree={name: tuple(sorted(items, key=lambda report: report.at)) for name, items in reports.items()},
        answers_by_ask={key: tuple(items) for key, items in given.items()},
        sends_by_handle={key: tuple(items) for key, items in sends.items()}, held_handles=frozenset(held),
        open_handoff_names=frozenset(open_names), due_worktrees=frozenset(due), done_at_by_worktree=agent_done,
        stops_by_work={work: tuple(items) for work, items in stops.items()},
        reached_environments=frozenset(row["environment"] for row in rows if row.get("environment")),
        environment_by_alias=aliases, lead_path=((state.get("lead") or {}).get("worktree")))


class LiveProbe:
    def __init__(self, cache: dict[str, str]) -> None:
        self.cache = cache
        self.reachable_by_host: dict[str, bool] = {}

    def command(self, pid: int, saved: str) -> str:
        try:
            out = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=PROBE_TIMEOUT).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        return out or saved

    def origin(self, path: str) -> str | None:
        try:
            result = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True,
                                    text=True, timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None if result.returncode == 0 else None

    def pr_state(self, slug: str, number: int) -> str | None:
        key = f"{slug}#{number}"
        if self.cache.get(key) in ("MERGED", "CLOSED"):
            return self.cache[key]
        try:
            result = subprocess.run(["gh", "pr", "view", str(number), "-R", slug, "--json", "state", "-q", ".state"],
                                    capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        state = result.stdout.strip() if result.returncode == 0 else None
        if state:
            self.cache[key] = state
        return state

    def forge_reachable(self, path: str) -> bool | None:
        host = origin_host(self.origin(path))
        if host not in self.reachable_by_host:
            try:
                self.reachable_by_host[host] = subprocess.run(
                    ["git", "-C", path, "ls-remote", "--exit-code", "origin", "HEAD"], capture_output=True, text=True,
                    timeout=PROBE_TIMEOUT, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode == 0
            except (OSError, subprocess.SubprocessError):
                self.reachable_by_host[host] = False
        return self.reachable_by_host[host]


class SavedProbe:
    def command(self, pid: int, saved: str) -> str:
        return saved

    def origin(self, path: str) -> str | None:
        return None

    def pr_state(self, slug: str, number: int) -> str | None:
        return None

    def forge_reachable(self, path: str) -> bool | None:
        return None


def newest_scan() -> Path | None:
    scans = list(READS.glob("*/scan.json"))
    return max(scans, key=lambda path: path.stat().st_mtime) if scans else None


def refreshed(rows: list[dict], now: datetime) -> list[dict]:
    """This Mac's rows with their transcripts, rollouts, background jobs, and API stop read again."""
    local = [row for row in rows if not row.get("environment") and row.get("path")]
    paths = [row["path"] for row in local]
    projects = HOME / ".claude/projects"
    live = scan.running_claude_transcripts(paths, scan.claude_session_dirs(), projects, scan.process_table(),
                                           scan.process_cwds)
    codex = scan.read_codex(scan.expand_roots(scan.DEFAULT_CODEX_SESSIONS),
                            (now - CODEX_SINCE).timestamp())
    jobs = scan.worktree_processes(paths, CPU_FILE, now.timestamp())
    fresh = []
    for row in rows:
        if row in local:
            row = {**row, "claude": scan.read_claude(projects, row["path"], live.get(row["path"], [])),
                   "codex": codex.get(row["path"]) or row.get("codex") or [],
                   "processes": jobs.get(row["path"], [])}
            row["stop"] = scan.stop_reason(row)
        fresh.append(row)
    return fresh


def verdict_of(row: Mapping, verdicts: Mapping[str, dict]) -> dict | None:
    return scan.current_verdict(dict(row), dict(verdicts))


def load_checks() -> list[dict]:
    data = scan.read_json_file(CHECKS) if CHECKS.is_file() else []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def key_of(item: Mapping) -> tuple[str, str, str]:
    return str(item.get("work")), str(item.get("signal")), str(item.get("fingerprint"))


def ask_text(signals: Sequence[Signal]) -> str:
    by_work: dict[str, list[Signal]] = {}
    for item in signals:
        by_work.setdefault(item.work, []).append(item)
    lines = [f"【止まった作業の合図】{len(by_work)} 件の作業に、手の要りそうな合図が出ました。"
             "指揮が、起こす・閉じる・待つを決めてください（ユーザーに回す問いではありません）。"]
    for number, (work, items) in enumerate(by_work.items(), 1):
        names = "・".join(SIGNALS[item.signal] for item in items)
        lines.append(f"{number}. {work}（{names}）")
        lines += [f"   - {item.reason}" for item in items]
    lines += ["起こす・立て直す: 合図の「立て直し」のとおり send <作業場> --ask <この ask の id> \"…\"。新しい担当に渡したら"
              " inbox_read.py answered <この ask の id> --note \"立て直し: <元の作業場> → <新しい作業場>\"（orca-session-triage の"
              " references/next-step.md の Recovering a stopped lane）。閉じる・待つ: inbox_read.py answered <この ask の id>"
              " --note \"理由\"。", "合図の記録: ~/.cache/orca-session-triage/work-checks.json"]
    return "\n".join(lines)


def notify(text: str) -> str | None:
    """Append the ask as the worktree stall-watch and return its inbox id."""
    SENDER.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in ("ORCA_WORKTREE_ID", "ORCA_TERMINAL_HANDLE")}
    before = len(read_jsonl(INBOX))
    result = subprocess.run([str(NOTIFY), "--kind", "ask", "--no-follow-up", text], cwd=SENDER, env=env,
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"triage-notify: {(result.stderr or result.stdout).strip()[:300]}")
    mine = [item for item in read_jsonl(INBOX)[before:] if item.get("worktree") == SENDER.name]
    return str(mine[-1].get("id")) if mine else None


def to_board(records: Sequence[dict]) -> str | None:
    """Each new signal into the board's work_check table; why not, when the table cannot take them."""
    if not BOARD_CHECK.is_file():
        return None
    for item in records:
        result = subprocess.run([str(BOARD_CHECK), "check"], input=json.dumps(item, ensure_ascii=False),
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            return (result.stderr or result.stdout).strip()[:300]
    return None


def run(args: argparse.Namespace) -> int:
    now = datetime.now().astimezone()
    source = Path(args.scan) if args.scan else newest_scan()
    if source is None or not source.is_file():
        print("stall_watch: 定時の scan.json が見つかりません（~/.cache/orca-session-triage/reads/*/scan.json）",
              file=sys.stderr)
        return 1
    try:
        rows = json.loads(source.read_text(encoding="utf-8"))
        records = load_records(now, rows)
        verdicts = scan.load_verdicts(VERDICTS)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"stall_watch: 記録を読めません: {exc}", file=sys.stderr)
        return 1
    fresh = refreshed(rows, now)
    checks = load_checks()
    cache = {item["watcher_pr"]: "MERGED" for item in checks if item.get("watcher_pr")}
    probe = LiveProbe(cache)
    seen = {key_of(item) for item in checks}
    found = [item for row in fresh for item in judge(row, verdict_of(row, verdicts), records, now, probe)]
    new = [item for item in found if key_of(asdict(item)) not in seen]
    if args.dry_run:
        print(ask_text(new) if new else "新しい合図はありません", f"(scan: {source})", sep="\n")
        return 0
    if not new:
        return 0
    try:
        inbox_id = notify(ask_text(new))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"stall_watch: 受信箱に入れられません: {exc}", file=sys.stderr)
        return 1
    added = [item.record(now, inbox_id) for item in new]
    scan.write_json_atomically(CHECKS, [*checks, *added])
    if why := to_board(added):
        print(f"stall_watch: work_check に入れられません（work-checks.json には記録済み）: {why}", file=sys.stderr)
    return 0


def replay(args: argparse.Namespace) -> int:
    now = parse(args.now)
    if now is None:
        print("stall_watch: --now はオフセット付きの ISO 8601 で書いてください", file=sys.stderr)
        return 2
    rows = json.loads(Path(args.scan).read_text(encoding="utf-8"))
    records = load_records(now, rows)
    verdicts = scan.load_verdicts(VERDICTS)
    for row in rows:
        for item in judge(row, verdict_of(row, verdicts), records, now, SavedProbe(), live=False):
            print(f"{item.work}\t{item.signal}\t{item.reason}")
    return 0


def last_activity(paths: Sequence[str], now: datetime) -> dict[str, Activity]:
    """When the agent of each of this Mac's worktrees last did anything, as the signals measure it."""
    records = load_records(now, [])
    projects = HOME / ".claude/projects"
    live = scan.running_claude_transcripts(paths, scan.claude_session_dirs(), projects, scan.process_table(),
                                           scan.process_cwds)
    codex = scan.read_codex(scan.expand_roots(scan.DEFAULT_CODEX_SESSIONS), (now - CODEX_SINCE).timestamp())
    transcripts = {path: scan.newest_claude_transcript(projects, path, live.get(path, [])) for path in paths}
    return {path: activity({"path": path, "claude": {"transcript": str(found)} if (found := transcripts[path]) else None,
                            "codex": codex.get(path, [])}, records, live=True) for path in paths}


def stopped_at(args: argparse.Namespace) -> int:
    for path, act in last_activity(args.paths, datetime.now().astimezone()).items():
        print(f"{path}\t{iso(act.at) or '-'}\t{act.by}")
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="judge the newest scan and send new signals")
    run_parser.add_argument("--scan", help="a saved scan.json instead of the newest one")
    run_parser.add_argument("--dry-run", action="store_true", help="print the ask and write nothing")
    replay_parser = commands.add_parser("replay", help="judge a saved scan at a past moment and print the signals")
    replay_parser.add_argument("--scan", required=True)
    replay_parser.add_argument("--now", required=True, help="the moment, as ISO 8601 with an offset")
    stopped = commands.add_parser("stopped-at", help="when each worktree's agent last did anything")
    stopped.add_argument("paths", nargs="+")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return {"run": run, "replay": replay, "stopped-at": stopped_at}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
