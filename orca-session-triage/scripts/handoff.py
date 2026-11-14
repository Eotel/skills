#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Send an hourly run's handoff to the appointed lead with what changed after the scan.

`send` appends, at the moment it sends, every inbox report that arrived after the
byte offset the run read at its start (`--since`, its first NEXT_OFFSET), and the
lead's latest message, unanswered question count, and undelivered queued input read again from its transcript. It
refuses to send while a handed-off decision key already has the lead's answer in
lead_decisions.py: follow that answer and drop the question instead. Just before sending it
records each --decision key as handed off (lead_decisions.handed_off reads them), so tools can
hold a write until the key's answer is recorded. After sending,
ack the NEXT_OFFSET it prints: every inbox entry before it is in the handoff, and it is past
this run's own handoff entry unless another report reached the inbox first.
Each sent handoff is kept in --handoffs-dir, one file per send named by its UTC time, with
the exact text, kind, keys, time, and NEXT_OFFSET; `last` prints the newest.
`send` does not send, and prints why, while a report another tab of this workspace sent (another
scheduled run: after the Mac sleeps, several run at once) or an answer to an ask the body names
(by its first 8 characters or more) reached the inbox or the answer records after --since; read them
and fix the body, then pass --seen ID for each one the body now reflects (a report's id, an answer's ask id, or
their first 8 characters or more). It does not send either while a scheduled run of this workspace due after this
tab's run began, or the precheck skipped one because this run was still working, and prints that run's time and
pane: the later run owns the read position and the handoff. --force sends past every check as written.
`search WORD...` prints each kept handoff whose text or decision keys hold any of the words (case ignored): its
time, kind, and file, then the matching lines and keys.
`--decision-ask KEY=ID` binds a key to an inbox ask in the handed-off log.
`lead-state` reads the lead's agents and rendered screens through Orca and prints JSON with its
latest message of its own (also while a turn runs or after an API error such as a usage limit ended it),
unanswered questions and ask times, the questions answered (`answered`) and closed unanswered (`rejected`,
with ask and close times) since the newest kept handoff, and undelivered queued input. It includes the
current transcript path and timestamped user_messages since that handoff (excluding task notifications).
`lead-state --grep WORD [--since HH:MM|ISO]` adds matching timestamped transcript events in `matches`:
lead and user speech, AskUserQuestion calls, and tool results (literal WORD, case ignored).
Pending questions answered in hourly state or ask-answers carry answered_elsewhere (the answer's time).
A fully answered selector visible on a tab gets a heading-checked close-selector command; this read sends nothing.
Both commands
default to the lead appointed in the shared hourly state when --lead-worktree is omitted.
`send` ends the text with the run's signature, <session name>（HH:MM の定時）: the name ListAgents
prints first and the local time the session started, from $CLAUDE_CONFIG_DIR/sessions/$CLAUDE_PID.json
(~/.claude by default). A signature the body ends with is replaced. When the record cannot be read, it
sends the body as written and says so on stderr.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import NamedTuple, Protocol

import answer_evidence
import inbox_read
import hourly_state
import json_output
import lead_decisions
import scan_sessions

NOTIFY = "~/.cache/orca-briefs/bin/triage-notify"
INBOX = "~/.cache/orca-briefs/triage-inbox.jsonl"
HANDOFFS = Path("~/.cache/orca-session-triage/handoffs")
EXCERPT = 160
SIGNATURE_LINE = re.compile(r"\n[^\n]+（\d{1,2}:\d{2} の定時）$")


class Deliver(Protocol):
    """Delivers a handoff of one kind to the lead's inbox; raises when it was not delivered."""

    def __call__(self, kind: str, text: str, /) -> None: ...


@dataclass(frozen=True)
class SavedHandoff:
    """A handoff as it was sent."""
    text: str
    kind: str
    decisions: list[str]
    sent_at: str
    next_offset: int

    def save(self, directory: Path, sent: datetime) -> Path:
        """Write it atomically to a file named by the UTC send time, so names sort by time."""
        path = directory / sent.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ.json")
        scan_sessions.write_json_atomically(path, asdict(self))
        return path

    @classmethod
    def newest(cls, directory: Path) -> tuple[Path, SavedHandoff]:
        path = max(directory.glob("*.json"), key=lambda found: found.name, default=None)
        if path is None:
            raise LookupError(f"no handoff has been saved in {directory}")
        try:
            return path, cls(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as error:
            raise ValueError(f"damaged saved handoff {path}: {error}") from error

    def shown(self, path: Path) -> str:
        keys = "".join(f"\n  {key}" for key in self.decisions) or " (none)"
        return (f"sent_at: {self.sent_at}\nkind: {self.kind}\nnext_offset: {self.next_offset}\nfile: {path}\n"
                f"decisions:{keys}\n\n{self.text}")


def last_sent_at(directory: Path) -> datetime | None:
    """When the newest kept handoff was sent; None before the first."""
    try:
        _, saved = SavedHandoff.newest(directory)
    except LookupError:
        return None
    sent = scan_sessions.parse_time(saved.sent_at)
    if sent is None:
        raise ValueError(f"invalid handoff sent_at: {saved.sent_at}")
    return sent


def triage_notify(executable: Path) -> Deliver:
    def deliver(kind: str, text: str) -> None:
        subprocess.run([str(executable), "--kind", kind, text], check=True, capture_output=True, text=True,
                       timeout=60)
    return deliver


def workspace() -> Path:
    """This run's worktree as triage-notify names it: Orca's for this tab, else the current directory."""
    return Path(os.environ.get("ORCA_WORKTREE_ID", "").partition("::")[2] or Path.cwd())


def inbox_around(inbox: Path, since: int) -> tuple[list[dict], list[dict], int]:
    """The reports before and after `since`, and the complete end offset."""
    data = inbox.read_bytes()
    end = data.rfind(b"\n") + 1
    if not 0 <= since <= end or (since and data[since - 1] != 10):
        raise ValueError("--since must be a NEXT_OFFSET that inbox_read.py printed for this inbox")
    return [item for item in map(inbox_read.entry, data[:since].splitlines()) if item], \
        [item for item in map(inbox_read.entry, data[since:end].splitlines()) if item], end


def report_head(item: dict) -> str:
    worktree = str(item.get("worktree") or "-")
    where = f"{item['repo']}/{worktree}" if item.get("repo") else worktree
    return f"- {item.get('ts') or '-'} {item.get('kind') or '-'} {where} id={item.get('id') or '-'}"


def excerpt(text: object) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:EXCERPT] + ("…" if len(compact) > EXCERPT else "")


def late_reports(reports: list[dict]) -> list[str]:
    """One entry per report after `since` (asks in full, others shortened)."""
    return [f"{report_head(item)}\n{item.get('text') or ''}" if item.get("kind") == "ask" else
            f"{report_head(item)}  {excerpt(item.get('text'))}" for item in reports]


class Reason(NamedTuple):
    """Something `send` will not send past: the id `--seen` names it by, and the line it prints."""
    id: str
    line: str


def overtaken(body: str, before: list[dict], after: list[dict], sender: inbox_read.Sender, handle: str,
              answers: Path) -> list[Reason]:
    """What reached the inbox or the answer records after `since` that the handoff may repeat or contradict:
    each report another tab of this workspace sent (`handle` is this tab's), and each answer recorded since then
    to an ask the body names by its first 8 characters or more."""
    found = [Reason(str(item.get("id") or ""), f"{report_head(item)} (another tab of this workspace: "
                    f"{item.get('handle') or '-'})  {excerpt(item.get('text'))}")
             for item in after if sender.sent(item) and item.get("handle", "") != handle]
    named = {item["id"] for item in [*before, *after] if item.get("kind") == "ask" and isinstance(item.get("id"), str)
             and len(item["id"]) >= 8 and re.search(rf"(?<![0-9a-f]){re.escape(item['id'][:8])}", body)}
    read_at = max(filter(None, (scan_sessions.parse_time(item.get("ts")) for item in before)), default=None)
    try:
        records = answers.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        records = []
    for record in filter(None, map(inbox_read.entry, (line.encode() for line in records))):
        ask, at = str(record.get("id") or "").partition(":")[0], scan_sessions.parse_time(record.get("ts"))
        if ask in named and (read_at is None or at is None or at > read_at):
            found.append(Reason(str(record["id"]), f"- answered {record['id']} at {record.get('ts') or '-'} "
                                f"({record.get('how') or '-'})  {excerpt(record.get('note'))}"))
    return found


def unseen(reasons: list[Reason], seen: list[str]) -> list[str]:
    """The lines of the reasons no --seen value names (inbox_read.names: an exact id or an 8+ character prefix)."""
    return [reason.line for reason in reasons if not any(inbox_read.names(reason.id, wanted) for wanted in seen)]


def search(directory: Path, words: list[str], since: datetime | None) -> int:
    """Print each kept handoff sent at or after `since` whose text or decision keys hold a word, oldest first."""
    folded = [word.casefold() for word in words]
    found = 0
    for path in sorted(directory.glob("*.json")):
        try:
            saved = SavedHandoff(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as error:
            print(f"handoff: skipped a damaged saved handoff {path}: {error}", file=sys.stderr)
            continue
        sent = scan_sessions.parse_time(saved.sent_at)
        if since is not None and (sent is None or sent < since):
            continue
        hits = [*(f"  {line}" for line in saved.text.splitlines() if any(word in line.casefold() for word in folded)),
                *(f"  決定キー: {key}" for key in saved.decisions if any(word in key.casefold() for word in folded))]
        if hits:
            found += 1
            print(f"{saved.sent_at} {saved.kind} {path}", *hits, sep="\n")
    if not found:
        print(f"handoff: no kept handoff in {directory} holds {' or '.join(words)}", file=sys.stderr)
    return 0


def lead_claude(projects: Path, worktree: str) -> dict | None:
    """The lead's newest Claude transcript, including one a session running under it writes after moving its cwd."""
    live = scan_sessions.running_claude_transcripts([worktree], scan_sessions.claude_session_dirs(), projects,
                                                    scan_sessions.process_table(), scan_sessions.process_cwds)
    return scan_sessions.read_claude(projects, worktree, live.get(worktree, []))


def lead_state(projects: Path, worktree: str) -> str:
    claude = lead_claude(projects, worktree)
    name = Path(worktree).name
    if claude is None:
        return f"指揮（{name}）の会話記録が見つかりません。"
    said = " ".join((claude.get("final_text") or "").split())
    queued = scan_sessions.queued_input(Path(claude["transcript"]))
    return (f"指揮の今（{name}）: 未回答の質問 {len(claude['pending_questions'])} 件。"
            f"最後の発言 {claude.get('final_at') or '-'}: {said[:200]}" + ("…" if len(said) > 200 else "")
            + "".join(f"\n未配信の指示（{item['enqueued_at'] or '-'}、{item['minutes']} 分経過）: {item['text']}"
                      for item in queued))


def last_said(entries: list[dict]) -> dict[str, str | None]:
    """The newest text the lead itself wrote, also while a turn runs or after an API error (a usage limit) ended
    it: Claude Code's error and placeholder messages (model <synthetic>) are not the lead's words."""
    for entry in reversed(entries):
        message = scan_sessions.message_of(entry)
        if entry.get("type") != "assistant" or entry.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
            continue
        content = message.get("content")
        said = [block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text"
                and str(block.get("text") or "").strip()] if isinstance(content, list) else []
        if said:
            return {"at": entry.get("timestamp"), "text": said[-1]}
    return {"at": None, "text": None}


def transcript_events(entries: list[dict]) -> list[dict]:
    """Readable, timestamped speech, questions, and tool results from the current transcript."""
    events = []
    for entry in entries:
        at = entry.get("timestamp")
        if scan_sessions.is_real_user_message(entry):
            events.append({"at": at, "kind": "user", "text": scan_sessions.user_text(entry)})
        message = scan_sessions.message_of(entry)
        if entry.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
            continue
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if entry.get("type") == "assistant" and block.get("type") == "text":
                events.append({"at": at, "kind": "assistant", "text": block.get("text") or ""})
            elif (entry.get("type") == "assistant" and block.get("type") == "tool_use"
                  and block.get("name") == "AskUserQuestion"):
                events.append({"at": at, "kind": "AskUserQuestion", "id": block.get("id"),
                               "text": json.dumps(block.get("input") or {}, ensure_ascii=False)})
            elif block.get("type") == "tool_result":
                result = block.get("content")
                text = result if isinstance(result, str) else "\n".join(
                    part["text"] for part in result or []
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
                events.append({"at": at, "kind": "tool_result", "id": block.get("tool_use_id"), "text": text})
    return events


def question_text(value: str) -> str:
    """Compare a recorded question without the choice list hourly `asked` appends."""
    return " ".join(re.split(r"選択肢[:：]|\n\s*- ", value, maxsplit=1)[0].split())


def annotate_elsewhere(pending: list[dict], state: dict, tabs: list[dict]) -> None:
    """Attach timestamped shared answers, and commands only for completely answered selectors on screen."""
    entries = hourly_state.inbox_entries()
    records = inbox_read.answer_records(inbox_read.answers_path())
    answered = [dict(item, source=f"hourly_state {key}") for key, item in state["questions"].items()
                if item.get("answer") is not None and not item.get("withdrawn_at")]
    for call in pending:
        asked_at = scan_sessions.parse_time(call.get("asked_at"))
        for question in call["questions"]:
            wanted = question_text(str(question.get("question") or ""))
            if not wanted or asked_at is None:
                continue
            candidates = [(item.get("answered_at"), item["source"]) for item in answered
                          if wanted == question_text(str(item.get("question") or ""))]
            matching = [(ask, number) for ask in entries if ask.get("kind") == "ask"
                        for number, original in enumerate(inbox_read.questions_of(ask), 1)
                        if isinstance(original, dict)
                        and wanted in {question_text(str(original.get("question") or "")),
                                       question_text(answer_evidence.shown_question(original))}
                        and (not question.get("header") or not original.get("header")
                             or question["header"] == original["header"])]
            # A repeated question alone cannot identify which inbox ask this selector puts.
            if len(matching) == 1:
                ask, number = matching[0]
                for key in (str(ask["id"]), f"{ask['id']}:{number}"):
                    record = records.get(key)
                    if record and "withdraw" not in str(record.get("how") or ""):
                        candidates.append((record.get("ts"), f"ask-answers {key}"))
            later = [(at, str(stamp), source) for stamp, source in candidates
                     if (at := scan_sessions.parse_time(stamp)) is not None and at >= asked_at]
            if later:
                _, stamp, source = min(later)
                question.update(answered_elsewhere=stamp, answer_source=source)
        if not call["questions"] or not all(question.get("answered_elsewhere") for question in call["questions"]):
            continue
        call["answered_elsewhere"] = max((at, question["answered_elsewhere"]) for question in call["questions"]
                                         if (at := scan_sessions.parse_time(question["answered_elsewhere"])) is not None)[1]
        headings = [str(question.get("header") or question.get("question") or "") for question in call["questions"]]
        call["close_selector"] = [shlex.join([str(Path(NOTIFY).with_name("close-selector").expanduser()),
                                              tab["handle"], *headings]) for tab in tabs
                                  if all(heading in "\n".join(tab["screen_tail"]) for heading in headings)
                                  and any(hint in "\n".join(tab["screen_tail"])
                                          for hint in ("Enter to select", "enter select"))]


def lead_details(projects: Path, worktree: str, orca: str, handoffs: Path, *,
                 state: dict | None = None, grep: str | None = None, since: datetime | None = None) -> dict[str, object]:
    """The lead's Orca state, newest transcript, unanswered questions, questions closed unanswered since the
    newest handoff kept in `handoffs`, and rendered tab tails."""
    page = scan_sessions.orca_payload(orca, ["worktree", "ps"])
    row = next((row for row in page["worktrees"] if Path(row["path"]) == Path(worktree)), None)
    if row is None:
        raise LookupError(f"lead worktree is missing from Orca: {worktree}")
    terminals = scan_sessions.orca_payload(orca, ["terminal", "list"])["terminals"]
    tabs = []
    for term in terminals:
        if term.get("worktreeId") != row["worktreeId"] or not term.get("handle"):
            continue
        result = scan_sessions.orca_payload(orca, ["terminal", "read", "--terminal", term["handle"], "--screen"])
        tabs.append({"handle": term["handle"], "screen_tail": scan_sessions.screen_tail(result.get("terminal") or result)})
    claude = lead_claude(projects, worktree)
    final: dict[str, str | None] = {"at": None, "text": None}
    questions = {}
    queued = []
    transcript = None
    events = []
    if claude:
        transcript = Path(claude["transcript"])
        entries = scan_sessions.load_jsonl(transcript)
        events = transcript_events(entries)
        final = last_said(entries)
        # As the scan reads it: only the turn since the last real user message, so a question left behind is gone.
        last = max((i for i, entry in enumerate(entries) if scan_sessions.is_real_user_message(entry)), default=-1)
        for entry in entries[last + 1:]:
            content = scan_sessions.message_of(entry).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if (entry.get("type") == "assistant" and block.get("type") == "tool_use"
                        and block.get("name") == "AskUserQuestion"):
                    questions[block.get("id")] = {"id": block.get("id"), "asked_at": entry.get("timestamp"),
                                                   "questions": [dict(question) for question in
                                                                 (block.get("input") or {}).get("questions", [])]}
                elif block.get("type") == "tool_result":
                    questions.pop(block.get("tool_use_id"), None)
        queued = scan_sessions.queued_input(transcript)
    # Every transcript of the worktree: a session the lead started after closing a question is the newest one.
    transcripts = list(dict.fromkeys([*answer_evidence.transcripts_of(projects, [Path(worktree)]),
                                      *([Path(claude["transcript"])] if claude else [])]))
    handoff_since = last_sent_at(handoffs)
    rejected = answer_evidence.rejected_questions(transcripts, handoff_since)
    if questions:
        annotate_elsewhere(list(questions.values()),
                           state if state is not None else hourly_state.read(Path(hourly_state.DEFAULT_STATE).expanduser()), tabs)
    agents = [{"type": agent.get("agentType"), "state": agent.get("state"),
               "state_started_at": scan_sessions.iso_time(agent.get("stateStartedAt")),
               "stopped_at": scan_sessions.iso_time(agent.get("stateStartedAt"))
               if agent.get("state") in ("done", "waiting") else None,
               "main_agent": agent.get("mainAgent")} for agent in row.get("agents") or []]
    result: dict[str, object] = {"worktree": worktree, "agents": agents, "final": final,
            "transcript": str(transcript) if transcript else None,
            "user_messages": [{"at": event["at"], "text": event["text"]} for event in events
                              if event["kind"] == "user" and (handoff_since is None
                                  or (at := scan_sessions.parse_time(event["at"])) is not None and at >= handoff_since)],
            "api_error": claude.get("api_error") if claude else None,
            "pending_questions": list(questions.values()),
            "answered": answer_evidence.answered_questions(transcripts, handoff_since), "rejected": rejected,
            "terminals": tabs, "queued_input": queued}
    if grep is not None:
        result["matches"] = [event for event in events if grep.casefold() in event["text"].casefold()
                             and (since is None or (at := scan_sessions.parse_time(event["at"])) is not None
                                  and at >= since)]
    return result


def decision_asks(values: list[str], keys: list[str]) -> dict[str, str]:
    """Bindings to handed-off keys; reject a malformed or conflicting binding before sending."""
    bound = {}
    for value in values:
        key, separator, ask_id = value.rpartition("=")
        if not separator or not ask_id.strip() or key not in keys:
            raise ValueError("--decision-ask needs KEY=ID with a key also given as --decision")
        if key in bound and bound[key] != ask_id:
            raise ValueError(f"conflicting --decision-ask ids for {key}")
        bound[key] = ask_id
    return bound


def session_signature() -> str | None:
    """<session name>（HH:MM の定時） from this Claude Code session's record; None, said on stderr, when it
    cannot be read: the name is never guessed."""
    record = hourly_state.session_record()
    try:
        session = json.loads(record.read_text(encoding="utf-8"))
        name, started = session["name"].strip(), datetime.fromtimestamp(session["startedAt"] / 1000)
        if not name:
            raise ValueError("the session has no name")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        print(f"handoff: sending without the run's signature: {record}: {error!r}", file=sys.stderr)
        return None
    return f"{name}（{started:%H:%M} の定時）"


def send(args: argparse.Namespace, deliver: Deliver, sender: inbox_read.Sender) -> int:
    answered = lead_decisions.latest(lead_decisions.records(args.decisions_file.expanduser()))
    keys = list(dict.fromkeys(args.decision))
    bound = decision_asks(args.decision_ask, keys)
    settled = [answered[key] for key in keys if key in answered]
    if settled:
        print("handoff: the lead already answered; follow each answer and drop it from the handoff:",
              *(f"{item.decision}: {item.answer}" for item in settled), sep="\n", file=sys.stderr)
        return 1
    body = args.body_file.read_text(encoding="utf-8").strip()
    if not body:
        raise ValueError(f"empty handoff: {args.body_file}")
    inbox = args.inbox.expanduser()
    before, after, end = inbox_around(inbox, args.since)
    if not args.force and (later := inbox_read.later_run_notes(args.orca, "handoff")):
        print("handoff: not sent: a later scheduled run of this workspace began, or was skipped because this run was "
              "still working. Leave the handoff to the run that owns the read position; --force sends it as written:",
              *later, sep="\n", file=sys.stderr)
        return 1
    found = overtaken(body, before, after, sender, os.environ.get("ORCA_TERMINAL_HANDLE", ""),
                      inbox_read.answers_path())
    if not args.force and (left := unseen(found, args.seen)):
        print("handoff: not sent: after --since, another tab of this workspace reported, or an ask the body names "
              "was answered. Read each one in full (inbox_read.py read --id ID) and fix the body; then pass --seen ID "
              "for each one the body reflects, or --force to send it as written:",
              *left, sep="\n", file=sys.stderr)
        return 1
    reports = late_reports(after)
    facts = ["—— 送る直前に道具が足した事実 ——",
             f"走査の開始後に届いた報告: {len(reports)} 件" + ("" if reports else "（なし）"), *reports]
    if args.lead_worktree:
        facts.append(lead_state(args.claude_projects.expanduser(), args.lead_worktree))
    if keys:
        lead_decisions.append(args.handed_off_file.expanduser(),
                              *(lead_decisions.HandOff.now(key, bound.get(key)) for key in keys))
    signature = session_signature()
    text = "\n\n".join([SIGNATURE_LINE.sub("", body).rstrip() if signature else body, "\n".join(facts),
                         *([signature] if signature else [])])
    deliver(args.kind, text)
    sent = datetime.now(timezone.utc)
    try:
        # triage-notify records the text stripped, under this workspace's name and this Mac's host.
        end = inbox_read.end_past(inbox, end, lambda item: item.get("kind") == args.kind
                                  and item.get("text") == text.strip() and sender.sent(item))
    except OSError as error:
        print(f"handoff: sent, but the inbox could not be read again, so NEXT_OFFSET stays before "
              f"this handoff: {error}", file=sys.stderr)
    try:
        SavedHandoff(text=text, kind=args.kind, decisions=keys, sent_at=sent.isoformat(timespec="seconds"),
                     next_offset=end).save(args.handoffs_dir.expanduser(), sent)
    except OSError as error:
        print(f"handoff: sent, but it could not be kept for `last`: {error}", file=sys.stderr)
    print(f"NEXT_OFFSET={end}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    kept = argparse.ArgumentParser(add_help=False)
    kept.add_argument("--handoffs-dir", type=Path, default=HANDOFFS,
                      help="where each sent handoff is kept (default: %(default)s)")
    commands = parser.add_subparsers(dest="action", required=True)
    lead = argparse.ArgumentParser(add_help=False)
    lead.add_argument("--lead-worktree", help="the lead's worktree (default: the shared state's appointed lead)")
    lead.add_argument("--state-file", type=Path, default=Path(hourly_state.DEFAULT_STATE),
                      help="shared hourly state JSON (default: %(default)s)")
    lead.add_argument("--claude-projects", type=Path, default=Path("~/.claude/projects"),
                      help="Claude Code transcript root (default: %(default)s)")
    state_command = commands.add_parser(
        "lead-state", parents=[kept, lead],
        description="JSON keys: worktree (path), agents (type, state, state_started_at, stopped_at, main_agent), "
                    "final (at, text: the lead's newest message of its own, past a running turn or an API error), "
                    "api_error (the error text that ended the last turn, or null), "
                    "transcript (current transcript path, or null), "
                    "user_messages (at, text: user speech since the newest kept handoff, no task notifications), "
                    "pending_questions (id, asked_at, questions with choices; answered_elsewhere is the answer time, "
                    "answer_source names its shared record; a fully answered call has close_selector commands), "
                    "answered (questions answered since the newest kept handoff: id, asked_at, answered_at, "
                    "answers by question, notes by question), "
                    "rejected (questions closed unanswered since the newest kept handoff, with ask/close times), "
                    "terminals (handle, screen_tail: 画面の末尾の行の配列（文字列ではない）), "
                    "queued_input (undelivered instructions and enqueue times). "
                    "With --grep: matches (at, kind, text, optional tool id), filtered by --since.",
        help="print the appointed lead's current state as JSON, with the questions answered (answered) and closed "
             "unanswered (rejected) since the newest kept handoff")
    state_command.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                               help="Orca CLI executable (default: %(default)s)")
    state_command.add_argument("--out", type=Path, metavar="FILE",
                               help="atomically write JSON to FILE instead of stdout")
    state_command.add_argument("--grep", metavar="WORD", help="add transcript events containing literal WORD (case ignored)")
    state_command.add_argument("--since", type=inbox_read.since_time, metavar="HH:MM|ISO",
                               help="only --grep events at or after this ISO time or most recent local HH:MM")
    send_command = commands.add_parser("send", parents=[kept, lead],
                                       help="add the reports and lead state of the moment and the run's signature, "
                                            "then send")
    send_command.add_argument("--body-file", type=Path, required=True, help="UTF-8 file with the handoff the run wrote")
    send_command.add_argument("--since", type=int, required=True,
                              help="the NEXT_OFFSET of the inbox read at the start of the run")
    send_command.add_argument("--decision", action="append", default=[],
                              help="a handed-off question's key, as given to lead_decisions.py; repeat per question")
    send_command.add_argument("--decision-ask", action="append", default=[], metavar="KEY=ID",
                              help="bind a --decision key to an inbox ask id; repeat per key")
    send_command.add_argument("--seen", action="append", default=[], metavar="ID",
                              help="a report or answered ask the body already reflects, by its id or first 8+ "
                                   "characters, taken off the reasons not to send; repeat per id")
    send_command.add_argument("--force", action="store_true",
                              help="send past every check: a later scheduled run, another tab's reports, and "
                                   "answers to the asks the body names")
    send_command.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                              help="Orca CLI executable that lists the later scheduled runs (default: %(default)s)")
    send_command.add_argument("--kind", choices=("ask", "info"), default="ask",
                              help="triage-notify kind (default: ask)")
    send_command.add_argument("--inbox", type=Path, default=Path(os.environ.get("TRIAGE_INBOX") or INBOX),
                              help="triage inbox (default: $TRIAGE_INBOX or %(default)s)")
    send_command.add_argument("--decisions-file", type=Path, default=lead_decisions.DEFAULT_LOG,
                              help="the lead's decision log (default: %(default)s)")
    send_command.add_argument("--handed-off-file", type=Path, default=lead_decisions.HANDED_OFF_LOG,
                              help="where the handed-off --decision keys are appended (default: %(default)s)")
    send_command.add_argument("--notify", default=NOTIFY, help="triage-notify executable (default: %(default)s)")
    commands.add_parser("last", parents=[kept],
                        help="print the newest kept handoff: its time, kind, NEXT_OFFSET, keys, then its text")
    search_command = commands.add_parser(
        "search", parents=[kept],
        help="print each kept handoff whose text or decision keys hold any WORD (case ignored): its time, kind, and "
             "file, then the matching lines and keys")
    search_command.add_argument("words", nargs="+", metavar="WORD")
    search_command.add_argument("--since", type=inbox_read.since_time,
                                help="only handoffs sent at or after this ISO time or most recent local HH:MM")
    args = parser.parse_args(argv)
    try:
        if args.action == "search":
            return search(args.handoffs_dir.expanduser(), args.words, args.since)
        if args.action == "last":
            path, handoff = SavedHandoff.newest(args.handoffs_dir.expanduser())
            print(handoff.shown(path))
            return 0
        if args.lead_worktree is None:
            appointed = hourly_state.appointed_lead(hourly_state.read(args.state_file.expanduser()))
            args.lead_worktree = str(appointed) if appointed else None
        if args.action == "lead-state":
            if not args.lead_worktree:
                raise ValueError("--lead-worktree or an appointed lead in --state-file is required")
            if args.since is not None and args.grep is None:
                raise ValueError("lead-state --since requires --grep WORD")
            json_output.emit(lead_details(args.claude_projects.expanduser(), args.lead_worktree, args.orca,
                                          args.handoffs_dir.expanduser(), state=hourly_state.read(args.state_file.expanduser()),
                                          grep=args.grep, since=args.since), args.out)
            return 0
        return send(args, triage_notify(Path(args.notify).expanduser()), inbox_read.Sender.of(workspace()))
    except subprocess.CalledProcessError as error:
        command = "Orca" if args.action == "lead-state" else "triage-notify"
        print(f"handoff: {command} failed: {(error.stderr or '').strip() or error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, LookupError, subprocess.TimeoutExpired) as error:
        print(f"handoff: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
