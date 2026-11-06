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
`--decision-ask KEY=ID` binds a key to an inbox ask in the handed-off log.
`lead-state` reads the lead's agents and rendered screens through Orca and prints JSON with its
latest message, unanswered questions and ask times, the questions closed unanswered (`rejected`, with
ask and close times) since the newest kept handoff, and undelivered queued input. Both commands
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
import subprocess
import sys
from typing import Protocol

import answer_evidence
import inbox_read
import hourly_state
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


def late_reports(inbox: Path, since: int) -> tuple[list[str], int]:
    """One entry per report after `since` (asks in full, others shortened) and the complete end offset."""
    data = inbox.read_bytes()
    end = data.rfind(b"\n") + 1
    if not 0 <= since <= end or (since and data[since - 1] != 10):
        raise ValueError("--since must be a NEXT_OFFSET that inbox_read.py printed for this inbox")
    entries = []
    for raw in data[since:end].splitlines():
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        worktree = str(item.get("worktree") or "-")
        where = f"{item['repo']}/{worktree}" if item.get("repo") else worktree
        head = f"- {item.get('ts') or '-'} {item.get('kind') or '-'} {where} id={item.get('id') or '-'}"
        text = str(item.get("text") or "")
        compact = " ".join(text.split())
        entries.append(f"{head}\n{text}" if item.get("kind") == "ask" else
                       f"{head}  {compact[:EXCERPT]}" + ("…" if len(compact) > EXCERPT else ""))
    return entries, end


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


def lead_details(projects: Path, worktree: str, orca: str, handoffs: Path) -> dict[str, object]:
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
    if claude:
        transcript = Path(claude["transcript"])
        entries = scan_sessions.load_jsonl(transcript)
        # As the scan reads it: only the turn since the last real user message, so a question left behind is gone.
        last = max((i for i, entry in enumerate(entries) if scan_sessions.is_real_user_message(entry)), default=-1)
        for entry in entries[last + 1:]:
            content = scan_sessions.message_of(entry).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if entry.get("type") == "assistant" and block.get("type") == "text" and block.get("text", "").strip():
                    final = {"at": entry.get("timestamp"), "text": block["text"]}
                elif (entry.get("type") == "assistant" and block.get("type") == "tool_use"
                      and block.get("name") == "AskUserQuestion"):
                    questions[block.get("id")] = {"id": block.get("id"), "asked_at": entry.get("timestamp"),
                                                   "questions": (block.get("input") or {}).get("questions", [])}
                elif block.get("type") == "tool_result":
                    questions.pop(block.get("tool_use_id"), None)
        queued = scan_sessions.queued_input(transcript)
    # Every transcript of the worktree: a session the lead started after closing a question is the newest one.
    rejected = answer_evidence.rejected_questions(
        dict.fromkeys([*answer_evidence.transcripts_of(projects, [Path(worktree)]),
                       *([Path(claude["transcript"])] if claude else [])]), last_sent_at(handoffs))
    agents = [{"type": agent.get("agentType"), "state": agent.get("state"),
               "state_started_at": scan_sessions.iso_time(agent.get("stateStartedAt")),
               "stopped_at": scan_sessions.iso_time(agent.get("stateStartedAt"))
               if agent.get("state") in ("done", "waiting") else None,
               "main_agent": agent.get("mainAgent")} for agent in row.get("agents") or []]
    return {"worktree": worktree, "agents": agents, "final": final,
            "pending_questions": list(questions.values()), "rejected": rejected, "terminals": tabs,
            "queued_input": queued}


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
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
    record = config / "sessions" / f"{os.environ.get('CLAUDE_PID', '')}.json"
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
    reports, end = late_reports(inbox, args.since)
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
        help="print the appointed lead's current state as JSON, with the questions closed unanswered (rejected) "
             "since the newest kept handoff")
    state_command.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                               help="Orca CLI executable (default: %(default)s)")
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
    args = parser.parse_args(argv)
    try:
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
            print(json.dumps(lead_details(args.claude_projects.expanduser(), args.lead_worktree, args.orca,
                                          args.handoffs_dir.expanduser()), ensure_ascii=False, indent=2))
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
