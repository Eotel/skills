#!/usr/bin/env python3
"""Send an hourly run's handoff to the appointed lead with what changed after the scan.

`send` appends, at the moment it sends, every inbox report that arrived after the
byte offset the run read at its start (`--since`, its first NEXT_OFFSET), and the
lead's latest message and unanswered questions read again from its transcript. It
refuses to send while a handed-off decision key already has the lead's answer in
lead_decisions.py: follow that answer and drop the question instead. Just before sending it
records each --decision key as handed off (lead_decisions.handed_off reads them), so tools can
hold a write until the key's answer is recorded. After sending,
ack the NEXT_OFFSET it prints: every inbox entry before it is in the handoff, and it is past
this run's own handoff entry unless another report reached the inbox first.
Each sent handoff is kept in --handoffs-dir, one file per send named by its UTC time, with
the exact text, kind, keys, time, and NEXT_OFFSET; `last` prints the newest.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Protocol

import inbox_read
import lead_decisions
import scan_sessions

NOTIFY = "~/.cache/orca-briefs/bin/triage-notify"
INBOX = "~/.cache/orca-briefs/triage-inbox.jsonl"
HANDOFFS = Path("~/.cache/orca-session-triage/handoffs")
EXCERPT = 160


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


def lead_state(projects: Path, worktree: str) -> str:
    claude = scan_sessions.read_claude(projects, worktree)
    name = Path(worktree).name
    if claude is None:
        return f"指揮（{name}）の会話記録が見つかりません。"
    said = " ".join((claude.get("final_text") or "").split())
    return (f"指揮の今（{name}）: 未回答の質問 {len(claude['pending_questions'])} 件。"
            f"最後の発言 {claude.get('final_at') or '-'}: {said[:200]}" + ("…" if len(said) > 200 else ""))


def send(args: argparse.Namespace, deliver: Deliver, sender: inbox_read.Sender) -> int:
    answered = lead_decisions.latest(lead_decisions.records(args.decisions_file.expanduser()))
    keys = list(dict.fromkeys(args.decision))
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
        lead_decisions.append(args.handed_off_file.expanduser(), *map(lead_decisions.HandOff.now, keys))
    text = "\n\n".join([body, "\n".join(facts)])
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
    send_command = commands.add_parser("send", parents=[kept],
                                       help="add the reports and lead state of the moment, then send")
    send_command.add_argument("--body-file", type=Path, required=True, help="UTF-8 file with the handoff the run wrote")
    send_command.add_argument("--since", type=int, required=True,
                              help="the NEXT_OFFSET of the inbox read at the start of the run")
    send_command.add_argument("--decision", action="append", default=[],
                              help="a handed-off question's key, as given to lead_decisions.py; repeat per question")
    send_command.add_argument("--lead-worktree", help="the appointed lead's worktree, whose transcript is read again")
    send_command.add_argument("--kind", choices=("ask", "info"), default="ask",
                              help="triage-notify kind (default: ask)")
    send_command.add_argument("--inbox", type=Path, default=Path(os.environ.get("TRIAGE_INBOX") or INBOX),
                              help="triage inbox (default: $TRIAGE_INBOX or %(default)s)")
    send_command.add_argument("--decisions-file", type=Path, default=lead_decisions.DEFAULT_LOG,
                              help="the lead's decision log (default: %(default)s)")
    send_command.add_argument("--handed-off-file", type=Path, default=lead_decisions.HANDED_OFF_LOG,
                              help="where the handed-off --decision keys are appended (default: %(default)s)")
    send_command.add_argument("--claude-projects", type=Path, default=Path("~/.claude/projects"),
                              help="Claude Code transcript root (default: %(default)s)")
    send_command.add_argument("--notify", default=NOTIFY, help="triage-notify executable (default: %(default)s)")
    commands.add_parser("last", parents=[kept],
                        help="print the newest kept handoff: its time, kind, NEXT_OFFSET, keys, then its text")
    args = parser.parse_args(argv)
    try:
        if args.action == "last":
            path, handoff = SavedHandoff.newest(args.handoffs_dir.expanduser())
            print(handoff.shown(path))
            return 0
        return send(args, triage_notify(Path(args.notify).expanduser()), inbox_read.Sender.of(workspace()))
    except subprocess.CalledProcessError as error:
        print(f"handoff: triage-notify failed: {(error.stderr or '').strip() or error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, LookupError, subprocess.TimeoutExpired) as error:
        print(f"handoff: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
