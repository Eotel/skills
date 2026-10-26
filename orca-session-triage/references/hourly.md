# Hourly run

An Orca automation starts `/orca-session-triage hourly` every hour in the
triage workspace. The automation's session leads triage unless another tab is
explicitly appointed lead. The unfiltered
`watch-inbox` runs as a macOS user service outside Orca tabs; owning a watch no
longer appoints a lead. A `--only` watch receives selected lanes' reports.
The triage workspace holds scheduled-run tabs only. Start requests outside the
hourly sweep in a different workspace, so they cannot hold the next run.
Read `references/hourly-state.md` for decisions shared across fresh sessions.
At the start of each run, use `scripts/inbox_read.py read` to see new inbox
entries in append order. After handling them, pass its `NEXT_OFFSET` to
`scripts/inbox_read.py ack <NEXT_OFFSET>`. The scheduled run owns the default
`~/.cache/orca-session-triage/inbox-read.offset` cursor. The separately
appointed lead in a separate worktree (currently `triage-lead` on the local
Mac) reads with
`TRIAGE_READ_OFFSET="$HOME/.cache/orca-session-triage/lead-inbox-read.offset"`
on both `read` and `ack`. When appointing a lead, hand over the unresolved
decisions first, then initialize that cursor once with the same environment
variable and `scripts/inbox_read.py initialize`. The command refuses to replace
an existing cursor; never reset it to skip unread entries. Neither tab
advances the other's cursor. Both see every
entry, but the lead owns answers to `ask` and directions to lanes. The hourly
run uses `info` and `done` for its sweep and sends unanswered `ask` entries
from other lanes to the lead in one handoff. It does not reforward its own
handoff entry. The lead checks inbox IDs and shared hourly state
before answering so a later hourly handoff cannot repeat an answered question.
The lead may read `info` and `done` for context but leaves the scheduled sweep
to the hourly run. A report pulled late from another Mac is still shown even
if its original timestamp is old. The watch and notification services each
keep their own cursor. The watch's output is in
`~/.cache/orca-briefs/triage-watch.log` for diagnosis.
The Stop hook keeps a scheduled run open while an `ask` is past the acknowledged
cursor, including on a repeated stop. Read it, handle it or hand it to the
appointed lead, then ack the reader's `NEXT_OFFSET` before ending the turn.

## In the run

When another tab has explicitly been appointed lead, scan and review the rows,
then do only the
cleanup allowed before a user choice by the Boundary and `cleanup.md`. Send questions
and instructions for other lanes to the lead in one `triage-notify --kind ask`
message, including any cleanup done. With nothing to hand off, report cleanup
with `--kind info`. The lead asks the user and directs the lanes. End without
starting a watch or changing the workspace comment.
Name any Orca environment the scan could not reach in the handoff. Review
rows from the reachable Macs in the same run.

When this run leads, read the shared hourly state before the scan. Match the
saved left rows to their newest final messages, retain pending questions, and
check promised handoffs. Then:

- Close previous scheduled-run tabs whose agents are done and whose questions
  have answers or no question. Check Orca's run history and terminal ownership
  before closing a tab; leave tabs with unanswered questions open. Apply
  `cleanup.md` to any done cards in them.
- Run the `backlog-watch` skill and include new notices and issues awaiting your reply in the hourly report.
- Every run, process the "次の一手" rows in asana99_status.py output with the
  asana-pr-review skill, even when its value differences were already judged.
- Record each new question as `references/hourly-state.md` says, ask with
  AskUserQuestion, and set the triage workspace's Orca comment to「要判断: …」.
  Keep the tab with that unanswered question open. A later run works through
  other rows and never repeats the recorded question. Once answered, record
  the answer before directing a lane. Other rows' verdicts are acted on in
  every run, changed or not, so a finished or stalled row is offered again
  until it is closed.
- When nothing needs the user, end with one line and no question.
- Last, set the triage workspace's comment to the run time and what the run
  did, so the board shows the last run.
- Leave no Monitor or other background job in this tab after the turn. A
  background job makes Orca report the tab as working and delays the next
  hourly run. Publishing an Artifact starts a live
  watch (its result says `Live subscription: arming`): stop it right after
  publishing with ArtifactComments `action: "watch"`, `on: false`, and the
  artifact's `url`. The Stop hook names a watch still running.

Check the external watch with
`python3 ~/.claude/skills/orca-session-triage/scripts/inbox_watch_service.py status`.
The Stop hook names a stopped service. Do not start a replacement Monitor in
the tab.

## External inbox service and cutover

`scripts/inbox_watch_service.py render` prints the launchd user agent for
review. It runs the existing `~/.cache/orca-briefs/bin/watch-inbox` with the
existing `~/.cache/orca-briefs/offsets/close-done-triage.offset`, restarts it
after exits, and appends its output to `~/.cache/orca-briefs/triage-watch.log`.
The generated service pins the local `python3` and `orca` paths so launchd can
run the watch and its Orca Task reconciliation without a login shell.
The inbox itself remains the source for hourly reads. Only one process may read
this offset at a time.

After the PR is merged and this skill is installed, get approval for this
cutover before changing the live watch:

1. Have the lead handle the reports already in the inbox. Then run
   `python3 ~/.claude/skills/orca-session-triage/scripts/inbox_read.py initialize`
   once while the old Monitor is still running. The new reader starts at that
   byte position; later arrivals are retained.
2. Ask the lead to stop the old `watch-inbox` Monitor in its tab and end its
   turn. Do not send terminal input to that tab. New inbox lines remain in the
   inbox while the watch is stopped.
3. Run `python3 ~/.claude/skills/orca-session-triage/scripts/inbox_watch_service.py activate`.
   It refuses to start if any process still reads the existing offset. Do not
   reset or copy the offset.
4. Run the same script with `status`, inspect `triage-watch.log`, and use
   `inbox_read.py read` to check delivery. The lead tab remains free of
   Monitors. Check the next scheduled run's new tab before claiming delivery works.

The ask alert service has its own cursor and shows a Mac notification with the
lane name and first line of each new `ask`. It alerts even while the lead tab is
active: tab-state checks can race with a new report, and the notification does
not acknowledge or answer it. The main watch and hourly reader keep their own
cursors. After this change is merged and installed, run
`python3 ~/.claude/skills/orca-session-triage/scripts/inbox_watch_service.py activate-alerts`
once, then `status-alerts` and inspect `~/.cache/orca-briefs/triage-alert.log`.
Activation starts at the current complete inbox line; later arrivals are
notified. Existing reports remain available to the hourly reader.

## The automation

```sh
orca automations create --name "session triage" --trigger hourly \
  --prompt "/orca-session-triage hourly 報告と質問は日本語で" --provider claude \
  --workspace "id:<repoId>::<triage workspace path>" --fresh-session \
  --precheck "python3 ~/.claude/skills/orca-session-triage/scripts/precheck.py --workspace <triage workspace path>" \
  --json
```

- An existing workspace with `--fresh-session` gets a new tab and session on
  each allowed run, without making a new worktree.
- `scripts/precheck.py` skips only while an agent in the triage workspace is
  working, including a background job. A waiting question can stay open while
  the next run starts. A skipped run is visible in
  `orca automations runs --id <id> --json`. A manual `orca automations run`
  does not run the precheck.
- Orca runs the precheck through `/bin/sh` with the login `PATH`, but in the
  repo's main checkout rather than the workspace, so the command names the
  workspace.
- The precheck reads this Mac's worktrees only, so keep the triage workspace
  on the local host.
