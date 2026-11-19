#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
"""Stop hook for orca-session-triage: keep the turn open while the review queue is not classed.

`scan_sessions.py --review-out` writes the rows a reader must class (every
unsure row, and a stalled row whose own PR merged) to
`<state dir>/<session id>/review-queue.json`; `orca-row-classify` writes its
verdicts to `review-result.json` beside it. While a queued row has no verdict
for that same queue, the hook blocks the stop once and names the rows. It also
blocks once while an artifact this session published is still watched live:
Orca counts the tab as working while the watch runs. A missing external inbox service
is named when the triage automation is installed. A repeated stop
(`stop_hook_active`) goes through for those checks, so a broken classifier cannot
trap the session. An unread ask blocks only the latest scheduled tab until its
inbox cursor is acked; an ask the tab's own workspace sent from this Mac (its
handoff) does not count.
After the checks pass, an hourly transcript path is queued for the external
retrospective worker.
Before AskUserQuestion in the hourly workspace, the PreToolUse hook checks for
kana or kanji in the questions, options and descriptions, and a handoff sent during this run.
After AskUserQuestion, the PostToolUse hook tells Claude to check, in one question, how to read
each answer whose chosen option sends something out (a post, merge, send, push, or production
change) while the same question carries a note. It does not judge whether they disagree. It also
names each answered question whose inbox ask a later ask of the same lane withdrew or names
(inbox_read.reissues): the lane may have asked it again with other choices.
"""

from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import cache
import json
import os
from pathlib import Path
import re
import runpy
import socket
import subprocess
import sys
from typing import Any
import unicodedata

import answer_evidence
from commitment_store import read_document
import ask_state
import handoff
import hourly_state
import inbox_read
import hourly_runtime
import lane_input
import lead_decisions
import retro

STATE_DIR = os.environ.get("TRIAGE_STATE_DIR", "~/.cache/orca-session-triage")
HOURLY_STATE = hourly_state.DEFAULT_STATE
TRIAGE_PROMPT = "/orca-session-triage"
# Kept in the session's state directory: how far the transcript was read and which artifacts are still watched.
WATCH_STATE = "artifact-watches.json"
# `liveSubscription` of an Artifact publish result that leaves a live watch running.
LIVE_SUBSCRIPTIONS = ("arming", "connected")
# Claude Code's notice that a watch ended on its own (connection lost, artifact not found).
WATCH_LIFECYCLE = "<task-type>artifact-watch-lifecycle</task-type>"


def read_json(path: Path) -> object:
    try:
        document = read_document(path)
        return json.loads(document) if document is not None else None
    except (OSError, ValueError):
        return None


VERDICT_CLASSES = ("waiting", "blocked", "in_progress", "done_unmerged", "finished")


JAPANESE = ("HIRAGANA LETTER", "KATAKANA LETTER", "HALFWIDTH KATAKANA LETTER",
            "CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH")
QUESTION_URL = re.compile(r"https?://[^\s<>()（）「」、。]+")
KANJI = re.compile(r"[\u3400-\u9fff]")
ALPHANUMERIC = re.compile(r"[a-z0-9]+")


def in_japanese(question: dict[str, Any]) -> bool:
    """Whether the question, its option labels, or their descriptions hold kana or kanji."""
    texts = [question.get("question", ""), *(value for option in question.get("options") or []
                                             for value in (option.get("label", ""), option.get("description", "")))]
    return any(unicodedata.name(char, "").startswith(JAPANESE) for text in texts for char in text)


def question_decision(hook_input: dict[str, Any], orca: str, state_dir: Path,
                      handoffs_dir: Path | None = None) -> dict[str, Any] | None:
    """Deny an hourly workspace's question until each one is in Japanese and, while another tab is appointed
    lead (hourly_state.py appoint-lead), until this run sent its handoff; with no lead the run asks itself."""
    if hook_input.get("tool_name") != "AskUserQuestion" or not hook_input.get("cwd"):
        return None
    workspace = hook_input["cwd"]
    state = read_json(state_dir / "hourly-state.json")
    lead = (state.get("lead") or {}).get("worktree") if isinstance(state, dict) else None
    if isinstance(state, dict) and lead and ask_state.same_workspace(lead, workspace):
        return duplicate_question(hook_input, state, state_dir, orca)
    try:
        if not any((automation.get("prompt") or "").startswith("/orca-session-triage hourly")
                   for automation in triage_automations(orca, workspace)):
            return None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
    try:
        runs = hourly_runtime.scheduled_runs(orca, workspace)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        runs = []
    questions = (hook_input.get("tool_input") or {}).get("questions") or []
    reasons = []
    if duplicated := duplicate_question(hook_input, state if isinstance(state, dict) else {}, state_dir, orca):
        reasons.append(duplicated["hookSpecificOutput"]["permissionDecisionReason"])
    if not all(in_japanese(question) for question in questions):
        reasons.append("問いごとに、問い・選択肢・説明は、仮名か漢字を含む日本語で書いてください。")
    if not (isinstance(state, dict) and (state.get("lead") or {}).get("worktree")):
        return deny(reasons)
    pane = os.environ.get("ORCA_PANE_KEY")
    started = next((run.get("startedAt") for run in runs
                    if pane and run.get("terminalPaneKey") == pane), None)
    if not isinstance(started, (int, float)):
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
        session = read_json(config / "sessions" / f"{os.environ.get('CLAUDE_PID', '')}.json")
        started = session.get("startedAt") if (isinstance(session, dict) and
                  session.get("sessionId") == hook_input.get("session_id")) else None
    try:
        if not isinstance(started, (int, float)):
            raise ValueError("この回の開始時刻を確認できません")
        _, sent = handoff.SavedHandoff.newest((handoffs_dir or handoff.HANDOFFS).expanduser())
        sent_at = datetime.fromisoformat(sent.sent_at.replace("Z", "+00:00"))
        if sent_at < datetime.fromtimestamp(started / 1000, tz=timezone.utc):
            reasons.append("最後の引き継ぎはこの回の開始前です。質問の前に handoff.py send で引き継ぎを送ってください。")
    except (OSError, ValueError, LookupError, TypeError, OverflowError) as error:
        reasons.append(f"この回の引き継ぎを確認できません（{error}）。質問の前に handoff.py send で送ってください。")
    return deny(reasons)


def direct_input_reasons(asks: dict[str, dict], labels: dict[str, list[str]], numbers: dict[str, set[int]],
                         unmatched: set[str], state_dir: Path, orca: str) -> list[str]:
    prompts = lane_input.direct_prompts(asks, orca)
    if not prompts:
        return []
    path = state_dir / "ask-lane-input-shown.json"
    reasons = []
    try:
        with retro.locked(path.with_suffix(".lock")):
            try:
                shown = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                shown = {}
            for ask_id, inputs in prompts.items():
                has_parts = bool(inbox_read.questions_of(asks[ask_id]))
                refs = [f"{ask_id}:{number}" for number in sorted(numbers[ask_id])]
                keys = refs + ([f"{ask_id}:?"] if ask_id in unmatched else []) or [ask_id]
                seen = [shown.setdefault(key, []) for key in keys]
                new_inputs = [(prompt, at) for prompt, at in inputs if any(at.isoformat() not in times for times in seen)]
                if not new_inputs:
                    continue
                for times in seen:
                    times.extend(at.isoformat() for _, at in new_inputs if at.isoformat() not in times)
                questions = "・".join(f"「{label}」" for label in dict.fromkeys(labels[ask_id]))
                lane = asks[ask_id].get("worktree") or asks[ask_id]["handle"]
                ref = f"{ask_id}:<質問番号>" if has_parts else ask_id
                excerpts = "、".join(f"{at.astimezone():%m/%d %H:%M}「{prompt[:80]}」" for prompt, at in new_inputs)
                reasons.append(f"{questions}（ask {ask_id}）の後、{lane} のタブに入力がありました（{excerpts}）。"
                               "三浦さんの回答なら、伺わずに担当へ "
                               f"`triage-notify --answered {ref} <答えの要約>` での記録を頼んでください。"
                               "元の ask で入力が答えている問いを確認し、対応する番号だけを指定してください。"
                               "回答でなければ、同じ AskUserQuestion をもう一度呼ぶと通ります。")
            if reasons:
                retro.atomic_text(path, json.dumps(shown, separators=(",", ":")))
    except (OSError, ValueError, AttributeError, TypeError):
        return []
    return reasons


QUESTION_CONDITIONS = re.compile(r"今週|来週|先週|今月|来月|先月|今日|本日|明日|昨日|[月火水木金土日]曜"
                                 r"|午前|午後|承認前|承認後|\d+(?:年|月|日|時|分|週)"
                                 r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}:\d{2}|\d{1,2}/\d{1,2}"
                                 r"|承認を得(?:てから|た後|る前|た場合|なかった場合)|承認取得[前後]"
                                 r"|以降|以前|(?<=[0-9日時分曜])\s*まで(?:に)?|jst|utc|再起動|リスタート|稼働を続け"
                                 r"|本番|検証環境|ステージング|(?<![a-z])(?:production|prod|staging|stg)(?![a-z])"
                                 r"|(?:ある|ない|する|しない|できる|できない)(?=場合|とき)")
ASK_REFERENCE = r"[0-9a-f-]{8,36}(?::[1-9]\d*)?"
ASK_CITATION = re.compile(rf"\bask(?:\s+id)?[\s:=]+({ASK_REFERENCE})(?![a-z0-9_-])", re.I)
BARE_ASK_CITATION = re.compile(r"(?<![a-z0-9_-])[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
                               r"(?::[1-9]\d*)?(?![a-z0-9_-])", re.I)
SAME_CONDITION = {"本日": "今日", "リスタート": "再起動", "production": "本番", "prod": "本番",
                  "staging": "ステージング", "stg": "ステージング"}
DEPLOYMENT_TARGETS = frozenset(("本番", "検証環境", "ステージング"))


def question_conditions(value: str) -> list[str]:
    return [SAME_CONDITION.get(term, term)
            for raw in QUESTION_CONDITIONS.findall(lane_input.normalized(QUESTION_URL.sub("", value)))
            if (term := raw.strip())]


def question_scope_agrees(original: dict, current: dict) -> bool:
    text = current.get("question") or ""
    original_text = original.get("question") or ""
    annotations = "".join(f"{original[key]} " for key in ("context", "impact") if original.get(key))
    if annotations:
        text = text.removeprefix(annotations)
    text = BARE_ASK_CITATION.sub("", ASK_CITATION.sub("", text))
    old_conditions, new_conditions = question_conditions(original_text), question_conditions(text)
    described_targets = {term for option in current.get("options") or [] for term in question_conditions(
        f"{option.get('label', '')} {option.get('description', '')}") if term in DEPLOYMENT_TARGETS}
    old_targets = DEPLOYMENT_TARGETS.intersection(old_conditions)
    if "この環境" in text and len(old_targets) == 1 and described_targets == old_targets:
        old_conditions = [term for term in old_conditions if term not in old_targets]
    return old_conditions == new_conditions and all(
        not current.get(key) or question_conditions(original.get(key) or "") == question_conditions(current[key])
        for key in ("context", "impact"))


def question_options_agree(left: dict, right: dict) -> bool:
    def choices(question: dict) -> dict[str, tuple[str, str]]:
        return {lane_input.option_key(option["label"]): (
                    lane_input.normalized(option["label"]).removesuffix("(推奨)").removesuffix("(recommended)").strip(),
                    lane_input.normalized(str(option.get("description") or "")))
                for option in question.get("options") or [] if isinstance(option, dict) and option.get("label")}

    first, second = choices(left), choices(right)
    if not first or not second:
        return True
    if first.keys() != second.keys() or bool(left.get("multiSelect")) != bool(right.get("multiSelect")):
        return False
    for key, (label, description) in first.items():
        other_label, other_description = second[key]
        if label == other_label:
            if not description or not other_description or description == other_description:
                continue
        elif key not in (label, other_label) or not description or not other_description:
            return False
        elif description == other_description:
            continue
        if question_conditions(description) != question_conditions(other_description):
            return False
        wording = SequenceMatcher(None, description, other_description, autojunk=False)
        if ALPHANUMERIC.findall(description) != ALPHANUMERIC.findall(other_description):
            return False
        if (wording.ratio() < 0.6 if label == other_label else wording.find_longest_match().size < 24):
            return False
        if any(
                action == "replace" and end - start <= 8 and other_end - other_start <= 8
                and KANJI.search(description[start:end]) and KANJI.search(other_description[other_start:other_end])
                for action, start, end, other_start, other_end in wording.get_opcodes()):
            return False
    return True


@cache
def workspace_repo_matches(location: Path, environment: str, repo: str, orca: str) -> bool:
    if location.parent.name == repo:
        return True
    try:
        if not environment:
            origin = subprocess.check_output(["git", "-C", str(location), "remote", "get-url", "origin"],
                                             text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
            return origin.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git") == repo
        worktrees = orca_result(orca, "worktree", "list", "--environment", environment, "--repo", f"name:{repo}")["worktrees"]
        return any(item.get("path") == str(location) for item in worktrees)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def worker_scope(entry: dict, rows: set[str], remote_hosts: dict, orca: str) -> bool:
    if not rows:
        return True
    local = os.environ.get("TRIAGE_HOST") or socket.gethostname()
    for row in rows:
        environment, _, workspace = row.rpartition("::")
        location = Path(workspace)
        if location.name != entry.get("worktree"):
            continue
        host = (remote_hosts.get(environment) or {}).get("report_host") if environment else local
        repo = entry.get("repo")
        if host and entry.get("host") in (None, host) and (not repo or workspace_repo_matches(location, environment, repo, orca)):
            return True
    return False


def registered_scope(question: dict, entries: list[dict],
                     handoff_ids: dict[str, ask_state.HandoffBinding]) -> list[tuple[str, frozenset[int]]]:
    scoped = []
    for ask_id, numbers in ask_state.references(question, entries, bindings=handoff_ids):
        if not numbers:
            original = next(entry for entry in entries if entry.get("id") == ask_id)
            matched = [number for number, part in enumerate(inbox_read.questions_of(original), 1)
                       if isinstance(part, dict) and question.get("question")
                       in (part.get("question"), answer_evidence.shown_question(part))]
            if len(matched) == 1:
                numbers = frozenset(matched)
        scoped.append((ask_id, numbers))
    return scoped


def duplicate_question(hook_input: dict, state: dict, state_dir: Path, orca: str) -> dict | None:
    entries = hourly_state.inbox_entries()
    path = inbox_read.answers_path()
    answers = inbox_read.answer_records(path)
    answers.update(lane_input.inferred_answers(inbox_read.paths()[0], path, orca))
    claims = inbox_read.read_claims(inbox_read.claims_path())
    withdrawn = inbox_read.withdrawal_times(entries)
    handoff_ids = ask_state.handoff_bindings((item.decision, item.ask_id) for item in
                                             lead_decisions.handed_off(lead_decisions.HANDED_OFF_LOG.expanduser()) if item.ask_id)
    remote_config = Path(os.environ.get("TRIAGE_PULL_CONFIG") or "~/.cache/orca-briefs/remote-inboxes.json").expanduser()
    remote_data = read_json(remote_config)
    remote_hosts = remote_data.get("hosts", {}) if isinstance(remote_data, dict) else {}
    questions = (hook_input.get("tool_input") or {}).get("questions") or []
    cwd, session = hook_input["cwd"], hook_input.get("session_id")
    reasons = []
    uncovered: dict[str, dict] = {}
    labels: dict[str, list[str]] = {}
    asked_numbers: dict[str, set[int]] = {}
    unmatched: set[str] = set()
    for question in questions:
        text, header = question.get("question") or "", question.get("header") or ""
        recorded = list(state.get("questions", {}).values())
        mine = [item for item in recorded if item.get("session") == session
                and ask_state.same_workspace(item.get("asked_in") or "", cwd)
               and item.get("answer") is None and not item.get("withdrawn_at")]
        own = [item for item in mine if item.get("question") == text]
        if not own and header:
            own = [item for item in mine if item.get("header") == header
                   and question_conditions(item.get("question") or "") == question_conditions(text)]
        if own:
            matched = [max(own, key=lambda item: item.get("asked_at") or "")]
        else:
            matched = [item for item in recorded if item.get("question") == text]
            placements = {(item.get("row"), item.get("decision"), tuple(item.get("covers") or [])) for item in matched}
            if len(placements) > 1:
                matched = []
        refs = [ref for item in matched for ref in ask_state.references(item, entries, bindings=handoff_ids)]
        for ask_id, numbers in list(refs):
            original = inbox_read.questions_of(next(item for item in entries if item.get("id") == ask_id))
            if numbers and original and all(0 < n <= len(original) and isinstance(original[n - 1], dict)
                                           and not question_options_agree(original[n - 1], question) for n in numbers):
                refs.remove((ask_id, numbers))
                matched = [item for item in matched
                           if (ask_id, numbers) not in ask_state.references(item, entries, bindings=handoff_ids)]
        presented = answer_evidence.shown_question(question)
        without_links = QUESTION_URL.sub("", presented)
        cited = [token.lower() for token in dict.fromkeys([
            *ASK_CITATION.findall(without_links), *BARE_ASK_CITATION.findall(without_links)])]
        tokens = cited or re.findall(rf"(?<![\w-]){ASK_REFERENCE}(?![\w-])", presented)
        named = [ref for token in tokens if (ref := ask_state.reference(token, entries))]
        if cited and len(cited) != len(named):
            continue
        numbered = [ref for ref in named if ref[1] and (entry := next(e for e in entries if e.get("id") == ref[0]))
                    and max(ref[1]) <= len(inbox_read.questions_of(entry))]
        if len(set(numbered)) == 1:
            ask_id, numbers = numbered[0]
            original_parts = inbox_read.questions_of(next(e for e in entries if e.get("id") == ask_id))
            if any(isinstance(part := original_parts[number - 1], dict)
                   and (not question_options_agree(part, question)
                        or not question_scope_agrees(part, question)) for number in numbers):
                continue
            refs = numbered[:1]
        elif named or not refs or any(not numbers for _, numbers in refs):
            workers = {row for item in matched if (row := item.get("row"))}
            named_ids = {ref[0] for ref in named}
            originals = [(str(entry["id"]), number, original)
                         for entry in entries if entry.get("kind") == "ask" and entry.get("id")
                         and (entry["id"] in named_ids if named_ids else worker_scope(entry, workers, remote_hosts, orca))
                         for number, original in enumerate(inbox_read.questions_of(entry), start=1)
                         if isinstance(original, dict) and (named_ids or not header or original.get("header") == header)
                         and question_options_agree(original, question)
                         and question_scope_agrees(original, question)
                         and (named_ids or entry["id"] not in withdrawn and f"{entry['id']}:{number}" not in withdrawn)]
            shown = [(ask_id, frozenset((number,))) for ask_id, number, original in originals
                     if text in (original.get("question"), answer_evidence.shown_question(original))]
            if not shown and header:
                links = set(QUESTION_URL.findall(presented))
                shown = [(ask_id, frozenset((number,))) for ask_id, number, original in originals
                         if named_ids or links.intersection(QUESTION_URL.findall(answer_evidence.shown_question(original)))]
            if named_ids and len(shown) == 1:
                refs = shown
            elif len(named_ids) == 1:
                if not originals and any(inbox_read.questions_of(entry) for entry in entries if entry.get("id") in named_ids):
                    continue
                refs = [(next(iter(named_ids)), frozenset())]
            elif refs:
                for index, (ask_id, numbers) in enumerate(refs):
                    candidates = [parts for original, parts in shown if original == ask_id]
                    if not numbers and len(candidates) == 1:
                        refs[index] = ask_id, candidates[0]
            elif len(shown) == 1:
                refs.extend(shown)
        if named:
            matched = [item for item in matched if any(ask_state.overlaps(ref, other) for ref in refs
                       for other in registered_scope(item, entries, handoff_ids))]
        for ref in dict.fromkeys(refs):
            ask_id, numbers = ref
            ask = next(entry for entry in entries if entry.get("id") == ask_id)
            if ask_state.covered(ref, list(answers), entries):
                parts = numbers or range(1, len(inbox_read.questions_of(ask)) + 1)
                covered = inbox_read.answer_parts(ask_id, parts, answers)
                facts = [f"{ask.get('worktree') or ask_id} の回答「{record['note']}」"
                         if record.get("how") in inbox_read.LANE_ANSWERS | {inbox_read.HOURLY_CORRECTION} and record.get("note")
                         else record.get("ts") or "時刻不明" for record in covered]
                facts = list(dict.fromkeys(facts))
                reasons.append(f"「{header or text}」は ask {ask_id} の回答済みの問いです（{'、'.join(facts)}）。"
                               "記録された回答を確認してください。")
            else:
                uncovered[ask_id] = ask
                labels.setdefault(ask_id, []).append(text or header)
                asked_numbers.setdefault(ask_id, set()).update(numbers)
                if not numbers and inbox_read.questions_of(ask):
                    unmatched.add(ask_id)
            parts = numbers or range(1, len(inbox_read.questions_of(ask)) + 1)
            candidates = ([inbox_read.question_claim(claims, ask_id, number) for number in parts]
                          if parts else [claims.get(ask_id)])
            for claim in dict.fromkeys(claim for claim in candidates if claim):
                if not ask_state.same_workspace(claim.by, cwd):
                    reasons.append(f"「{header or text}」の ask {ask_id} は {claim.by} が claim 中です（{claim.at}）。重ねて伺わないでください。")
        for item in matched:
            if item.get("answer") is not None:
                reasons.append(f"「{header or text}」は hourly_state の回答済みの問いです（{item.get('answered_at')}）。答えを読んでください。")
        related = {value for item in matched for value in [item.get("decision"), *(item.get("covers") or [])]
                   if value and ask_state.same_bound_ask(item, handoff_ids.get(value), value)}
        for item in ask_state.active(state):
            same = ask_state.same_workspace(item["by"], cwd)
            if same and (not item["session"] or item["session"] == session):
                continue
            held = (registered_scope(item["record"], entries, handoff_ids) if named else
                    ask_state.references(item["record"], entries, bindings=handoff_ids))
            overlap = any(ask_state.overlaps(ref, other) for ref in refs for other in held)
            keys = ask_state.active_keys(item, handoff_ids)
            if item["record"] in matched or related.intersection(keys) or overlap:
                reasons.append(f"「{header or text}」は {item['by']} が伺い中です（{item['at']}、行 {item['row']}、鍵 {', '.join(keys)}、問い {item['id']}）。重ねて伺わないでください。")
    reasons.extend(direct_input_reasons(uncovered, labels, asked_numbers, unmatched, state_dir, orca))
    return deny(list(dict.fromkeys(reasons)))


# An option that sends something out of this session: a post, merge, send, push, or production change.
OUTWARD = re.compile(r"投稿|merge|マージ|送(?:る|り|っ|信)|push|本番", re.IGNORECASE)


def noted_outward_answers(hook_input: dict[str, Any]) -> list[str]:
    """Each answered question, as `header: chosen labels (メモ: notes)`, whose chosen option sends
    something out while the same question carries a note."""
    if hook_input.get("tool_name") != "AskUserQuestion":
        return []
    response = hook_input.get("tool_response")
    if not isinstance(response, dict):
        return []
    answers, annotations = response.get("answers") or {}, response.get("annotations") or {}
    found = []
    for question in response.get("questions") or (hook_input.get("tool_input") or {}).get("questions") or []:
        text = question.get("question", "")
        note = (annotations.get(text) or {}).get("notes") if isinstance(annotations.get(text), dict) else None
        answer = answers.get(text)
        if not (isinstance(note, str) and note.strip() and isinstance(answer, str)):
            continue
        picked = set(answer.split(", ")) | {answer}
        chosen = [option for option in question.get("options") or [] if option.get("label") in picked]
        if any(OUTWARD.search(f"{option.get('label', '')} {option.get('description', '')}") for option in chosen):
            labels = "・".join(option["label"] for option in chosen)
            found.append(f"{question.get('header') or text}: 「{labels}」（メモ: {note.strip()}）")
    return found


def reissued_answers(hook_input: dict[str, Any], state: Path, entries: list[dict]) -> list[str]:
    """Each answered question's ask that later asks of the same lane withdrew or name, as `ask X（question）→ Y`.
    Its ask is the inbox ask that shows the question (as `inbox_read.py questions` prints it), or the ask of a
    question hourly_state.py recorded unanswered with the same wording or for this very call (call_records: a
    record of this session made just before it, which no earlier call took)."""
    response = hook_input.get("tool_response")
    if hook_input.get("tool_name") != "AskUserQuestion" or not isinstance(response, dict) or not isinstance(
            response.get("answers"), dict):
        return []
    answered = set(response["answers"])
    asks = {str(entry["id"]): shown for entry in entries if entry.get("kind") == "ask" and entry.get("id")
            for shown in answer_evidence.questions_of(entry) if shown in answered}
    transcript, call = hook_input.get("transcript_path"), hook_input.get("tool_use_id")
    try:
        recorded = list(hourly_state.read(state)["questions"].values())
        mine = [item for item in recorded if item.get("session") == hook_input.get("session_id")]
        this_call = (hourly_state.call_records(mine, Path(transcript), call)
                     if mine and transcript and call and Path(transcript).is_file() else [])
    except (OSError, ValueError):
        recorded, this_call = [], []
    for item in [*this_call, *(item for item in recorded if item.get("question") in answered)]:
        if (item.get("answer") is None and not item.get("withdrawn_at")
                and (ask := hourly_state.question_ask(item, entries))):
            asks.setdefault(ask[0], item["question"])
    return [f"ask {ask_id[:8]}（{hourly_state.excerpt(question)}）→ ask {reissue.id[:8]}（{reissue.ts}、"
            + ("取り下げて出し直した" if reissue.withdrawn else "名指しした") + "）"
            for ask_id, question in asks.items() for reissue in inbox_read.reissues(ask_id, entries)]


def answer_decision(hook_input: dict[str, Any], state: Path | None = None,
                    entries: list[dict] | None = None) -> dict[str, Any] | None:
    """A PostToolUse decision that puts the checks in front of Claude, or None."""
    reasons = []
    if noted := noted_outward_answers(hook_input):
        reasons.append("外へ出す操作の選択肢に、メモが付いた答えがあります。" + " / ".join(noted) +
                       "。担当へ許可として渡す前に、選択肢とメモの読みを 1 問で確かめてください。"
                       "確かめた答えは hourly_state.py answered --replace-reason-file で記録を置き換えます。")
    if state is not None and (reissued := reissued_answers(hook_input, state, entries or [])):
        reasons.append("答えた問いの ask を、後から同じ担当の ask が取り下げたか名指ししています。" + " / ".join(reissued) +
                       "。新しい ask の本文と選択肢を読み、答えが今の問いに合うかを確かめてから記録し、担当へ送ってください。")
    return {"decision": "block", "reason": " ".join(reasons)} if reasons else None


def deny(reasons: list[str]) -> dict[str, Any] | None:
    if not reasons:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": " ".join(reasons)}}


def decide(hook_input: dict, state_dir: str | Path, orca: str = "orca") -> dict[str, str] | None:
    """A Stop hook decision ({"decision": "block", "reason": ...}), or None to let the turn end.

    Damaged or unexpected files, or an Orca CLI that fails, let the turn end: the hook must
    never trap the session.
    """
    if not hook_input.get("session_id"):
        return None
    pending_asks = unread_ask_reason(hook_input.get("cwd"), orca)
    if hook_input.get("stop_hook_active"):
        try:
            runpy.run_path(str(Path.home() / ".agents/hooks/rule_events.py"))["record_exception"](
                "triage-stop-repeat", "stop_hook_active=true", hook_input,
                ["Stop", "stop_hook_active=true"])
        except Exception:
            pass
        return {"decision": "block", "reason": pending_asks} if pending_asks else None
    session = Path(state_dir) / hook_input["session_id"]
    # Review and live-watch notices block once; unread asks remain a gate until acked.
    reasons = [pending_asks, watched(session, hook_input.get("transcript_path"))]
    queue = read_json(session / "review-queue.json")
    if isinstance(queue, dict):
        reasons += [unclassed(session, queue), missing_inbox_watch(hook_input.get("cwd"), orca)]
    reasons = [r for r in reasons if r]
    return {"decision": "block", "reason": " ".join(reasons)} if reasons else None


def unread_ask_reason(workspace: str | None, orca: str) -> str | None:
    if not workspace:
        return None
    try:
        if not hourly_runtime.current_pane(orca):
            return None
        asks = inbox_read.unread_asks(*inbox_read.paths(), mine=inbox_read.Sender.of(Path(workspace)))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
    if not asks:
        return None
    lanes = ", ".join(dict.fromkeys(asks))
    return (f"orca-session-triage: unread ask from {lanes}. Run inbox_read.py read, "
            "handle the question, then ack its NEXT_OFFSET before ending this turn.")


def watched(session: Path, transcript: str | None) -> str | None:
    """Why to stay while an artifact this session published is still watched live, or None.

    Reads only the transcript lines written since the last stop. An Artifact publish whose result has a live
    subscription starts a watch; an ArtifactComments watch-stop result for the same URL, or Claude Code's notice
    that the watch ended on its own, ends it.
    """
    if not transcript:
        return None
    state_file = session / WATCH_STATE
    state = read_json(state_file)
    if not isinstance(state, dict) or state.get("transcript") != transcript:
        state = {}
    stored_offset = state.get("offset")
    offset = stored_offset if isinstance(stored_offset, int) else 0
    stored_watched = state.get("watched")
    kept = stored_watched if isinstance(stored_watched, list) else []
    urls = dict.fromkeys(url for url in kept if isinstance(url, str))
    try:
        with Path(transcript).open("rb") as lines:
            if os.fstat(lines.fileno()).st_size < offset:
                offset, urls = 0, {}
            lines.seek(offset)
            for line in lines:
                if not line.endswith(b"\n"):
                    break  # still being written; read it at the next stop
                offset += len(line)
                if b'"liveSubscription"' in line or b'"unwatch"' in line or b"artifact-watch-lifecycle" in line:
                    follow_watch(line, urls)
    except OSError:
        return None
    try:
        session.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"transcript": transcript, "offset": offset, "watched": list(urls)}))
    except OSError:
        pass  # the next stop reads the same lines again
    if not urls:
        return None
    return ("orca-session-triage: a live watch still runs on " + ", ".join(urls) + ". Stop each with the "
            "ArtifactComments tool (action:\"watch\", on:false, url:<that URL>) before ending the turn: a live "
            "watch keeps Orca counting this tab as working, so the hourly run is skipped.")


def follow_watch(line: bytes, urls: dict[str, None]) -> None:
    """Add the URL a publish result starts watching to urls, or drop the one a watch-stop result or notice names."""
    try:
        entry = json.loads(line)
    except ValueError:
        return
    if not isinstance(entry, dict):
        return
    notice = entry.get("attachment")
    if isinstance(notice, dict) and isinstance(notice.get("prompt"), str) and WATCH_LIFECYCLE in notice["prompt"]:
        for url in re.findall(r"Watch on (\S+) ended", notice["prompt"]):
            urls.pop(url, None)
        return
    result = entry.get("toolUseResult")
    if not isinstance(result, dict):
        return
    stopped = result.get("unwatch")
    if isinstance(stopped, dict) and isinstance(stopped.get("url"), str):
        urls.pop(stopped["url"], None)
    elif result.get("liveSubscription") in LIVE_SUBSCRIPTIONS and isinstance(result.get("url"), str):
        urls[result["url"]] = None


def unclassed(session: Path, queue: dict) -> str | None:
    """Why to stay while a row of this scan's review queue has no verdict, or None."""
    queued = [row["path"] for row in queue.get("rows") or [] if isinstance(row, dict) and row.get("path")]
    if not queued:
        return None
    result = read_json(session / "review-result.json")
    classed = set()
    if isinstance(result, dict) and result.get("queue_generated_at") == queue.get("generated_at"):
        classed = {row.get("path") for row in result.get("rows") or []
                   if isinstance(row, dict) and row.get("class") in VERDICT_CLASSES}
    missing = [path for path in queued if path not in classed]
    if not missing:
        return None
    return (f"orca-session-triage: {len(missing)} row(s) in this scan's review queue have no verdict. "
            f"Invoke the orca-row-classify skill with {session} and act on its verdicts before ending "
            "the turn: " + ", ".join(missing) + ".")


def orca_result(orca: str, *args: str) -> dict:
    out = subprocess.run([orca, *args, "--json"], capture_output=True, text=True, check=True, timeout=15).stdout
    data = json.loads(out)
    return data.get("result", data)


def triage_automations(orca: str, workspace: str) -> list[dict]:
    here = Path(os.path.realpath(workspace))
    return [a for a in orca_result(orca, "automations", "list")["automations"]
            if (a.get("workspaceId") or "").split("::", 1)[-1]
            and here.is_relative_to(os.path.realpath(a["workspaceId"].split("::", 1)[-1]))
            and (a.get("prompt") or "").startswith(TRIAGE_PROMPT)]


def inbox_service_running() -> bool:
    script = Path(__file__).with_name("inbox_watch_service.py")
    result = subprocess.run([sys.executable, str(script), "status"], capture_output=True,
                            text=True, timeout=15)
    return result.returncode == 0


def missing_inbox_watch(workspace: str | None, orca: str) -> str | None:
    if not workspace:
        return None
    try:
        if not triage_automations(orca, workspace) or inbox_service_running():
            return None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
    return ("orca-session-triage: the external triage inbox service is not running. "
            "See references/hourly.md for the watch-inbox service status and cutover steps; "
            "do not start another reader of ~/.cache/orca-briefs/offsets/close-done-triage.offset.")


def main(handoffs_dir: Path | None = None) -> int:
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        return 0
    orca = os.environ.get("ORCA_CLI_COMMAND", "orca")
    if hook_input.get("hook_event_name") == "PostToolUse":
        try:
            entries = (hourly_state.inbox_entries() if hook_input.get("tool_name") == "AskUserQuestion"
                       and (hook_input.get("tool_response") or {}).get("answers") else [])
        except (OSError, AttributeError):
            entries = []
        decision = answer_decision(hook_input, Path(HOURLY_STATE).expanduser(), entries)
        if decision:
            json.dump(decision, sys.stdout, ensure_ascii=False)
        return 0
    if hook_input.get("hook_event_name") == "PreToolUse":
        try:
            decision = question_decision(hook_input, orca, Path(STATE_DIR).expanduser(), handoffs_dir)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            decision = None
        if decision:
            json.dump(decision, sys.stdout, ensure_ascii=False)
        return 0
    decision = decide(hook_input, str(Path(STATE_DIR).expanduser()), orca)
    if decision:
        json.dump(decision, sys.stdout)
    else:
        try:
            retro.enqueue(hook_input)
        except OSError:
            pass  # The queue is best-effort; a write failure must not trap the triage tab.
    return 0


if __name__ == "__main__":
    sys.exit(main())
