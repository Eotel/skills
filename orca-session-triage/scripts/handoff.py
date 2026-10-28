#!/usr/bin/env python3
"""Send an hourly run's handoff to the appointed lead with what changed after the scan.

`send` appends, at the moment it sends, every inbox report that arrived after the
byte offset the run read at its start (`--since`, its first NEXT_OFFSET), and the
lead's latest message and unanswered questions read again from its transcript. It
refuses to send while a handed-off decision key already has the lead's answer in
lead_decisions.py: follow that answer and drop the question instead. Just before sending it
records each --decision key as handed off (lead_decisions.handed_off reads them), so tools can
hold a write until the key's answer is recorded. After sending,
ack the NEXT_OFFSET it prints: every inbox entry before it is in the handoff.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import lead_decisions
import scan_sessions

NOTIFY = "~/.cache/orca-briefs/bin/triage-notify"
INBOX = "~/.cache/orca-briefs/triage-inbox.jsonl"
EXCERPT = 160


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="action", required=True)
    send = commands.add_parser("send", help="add the reports and lead state of the moment, then send")
    send.add_argument("--body-file", type=Path, required=True, help="UTF-8 file with the handoff the run wrote")
    send.add_argument("--since", type=int, required=True,
                      help="the NEXT_OFFSET of the inbox read at the start of the run")
    send.add_argument("--decision", action="append", default=[],
                      help="a handed-off question's key, as given to lead_decisions.py; repeat per question")
    send.add_argument("--lead-worktree", help="the appointed lead's worktree, whose transcript is read again")
    send.add_argument("--kind", choices=("ask", "info"), default="ask", help="triage-notify kind (default: ask)")
    send.add_argument("--inbox", type=Path, default=Path(os.environ.get("TRIAGE_INBOX") or INBOX),
                      help="triage inbox (default: $TRIAGE_INBOX or %(default)s)")
    send.add_argument("--decisions-file", type=Path, default=lead_decisions.DEFAULT_LOG,
                      help="the lead's decision log (default: %(default)s)")
    send.add_argument("--handed-off-file", type=Path, default=lead_decisions.HANDED_OFF_LOG,
                      help="where the handed-off --decision keys are appended (default: %(default)s)")
    send.add_argument("--claude-projects", type=Path, default=Path("~/.claude/projects"),
                      help="Claude Code transcript root (default: %(default)s)")
    send.add_argument("--notify", default=NOTIFY, help="triage-notify executable (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        answered = lead_decisions.latest(lead_decisions.records(args.decisions_file.expanduser()))
        settled = [answered[key] for key in dict.fromkeys(args.decision) if key in answered]
        if settled:
            print("handoff: the lead already answered; follow each answer and drop it from the handoff:",
                  *(f"{item.decision}: {item.answer}" for item in settled), sep="\n", file=sys.stderr)
            return 1
        body = args.body_file.read_text(encoding="utf-8").strip()
        if not body:
            raise ValueError(f"empty handoff: {args.body_file}")
        reports, end = late_reports(args.inbox.expanduser(), args.since)
        facts = ["—— 送る直前に道具が足した事実 ——",
                 f"走査の開始後に届いた報告: {len(reports)} 件" + ("" if reports else "（なし）"), *reports]
        if args.lead_worktree:
            facts.append(lead_state(args.claude_projects.expanduser(), args.lead_worktree))
        if args.decision:
            lead_decisions.append(args.handed_off_file.expanduser(),
                                  *map(lead_decisions.HandOff.now, dict.fromkeys(args.decision)))
        subprocess.run([str(Path(args.notify).expanduser()), "--kind", args.kind, "\n\n".join([body, "\n".join(facts)])],
                       check=True, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as error:
        print(f"handoff: triage-notify failed: {(error.stderr or '').strip() or error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"handoff: {error}", file=sys.stderr)
        return 1
    print(f"NEXT_OFFSET={end}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
