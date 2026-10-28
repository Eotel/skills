# Hourly run

An Orca automation starts `/orca-session-triage hourly` every hour in the
triage workspace. The automation's session leads triage unless another tab is
explicitly appointed lead. The unfiltered
`watch-inbox` runs as a macOS user service outside Orca tabs; owning a watch no
longer appoints a lead. A `--only` watch receives selected lanes' reports.
The triage workspace holds scheduled-run tabs only. Start requests outside the
hourly sweep in a different workspace, so they cannot hold the next run.
Read `references/hourly-state.md` for decisions shared across fresh sessions.
At the start of each run, use `scripts/inbox_read.py read --brief` to see new
inbox entries in append order. `--brief` keeps `ask` and `done` in full and
shows the first 160 characters of each `info`; use `read` without it when the
full context of an `info` is needed. Both modes leave the cursor unchanged.
For a large inbox, `read --summary` shows each entry on one line with its
`id=` and the first 160 characters of its text, and `read --id ID[,ID]` prints
only those entries in full; it fails when an id is not after the cursor. Read a
long inbox this way rather than cutting the output. `--kind ask,done` filters by type, and
`--source skills/triage-request-ledger` filters by the displayed
`repo/worktree` (older entries without a repo use their worktree). These options
only filter the display: `NEXT_OFFSET` still covers every complete inbox entry.
Read an unfiltered summary before acknowledging an offset from a filtered view
so hidden entries are not skipped.
After handling the entries, pass `NEXT_OFFSET` to
`scripts/inbox_read.py ack <NEXT_OFFSET>`. The scheduled run owns the default
`~/.cache/orca-session-triage/inbox-read.offset` cursor. Only the latest
scheduled triage tab may read or acknowledge that cursor; a run Orca marked
`dispatch_failed` on a restart still counts while its tab is connected. An older tab must
leave new inbox entries for the current run; it can finish without handling
them. The separately
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
The Stop hook keeps the current scheduled run open while an `ask` is past the acknowledged
cursor, including on a repeated stop. Read it, handle it or hand it to the
appointed lead, then ack the reader's `NEXT_OFFSET` before ending the turn.
After these checks permit the turn to end, the same hook queues this hourly
session's transcript. The external retrospective service skips a session the
run's own forked retrospective already recorded and reviews one it did not.
See `references/retro.md` for both and for reviewing their proposals.

## In the run

At the start, run
`python3 ~/.claude/skills/orca-session-triage/scripts/cleanup_hourly_tabs.py`.
This checks Orca's run, terminal and agent records before closing previous
scheduled tabs. It keeps a tab if its turn or background work is active, a
question from that tab (or one without a recorded owner) is unanswered, its
latest conversation may still ask the user, its transcript is missing or active
within five minutes, or its rendered screen shows a draft or a nonempty input
field. When Orca has no provider session ID, the script requires one transcript
whose scheduled prompt matches the run's start time. Inspect
`keep:` output and retry on a later hourly run; leave the workspace open.

In every hourly run, run the Asana 99 sweep as `asana-pr-review` describes and
read its `asana99_status.py` 「次の一手」,
including when `--judged` hides unchanged differences. A 「レビューの番」 row
contains the exact `review-pr` command with `--kind` and `--if-needed` when
the PR's source is known. If the row has no command, confirm who created the
PR before choosing `cloud` or `colleague`.

In every hourly run on a Mac that has `~/.cache/orca-briefs/bin/topic`, run
`topic check --all`. Each drift row names the record, what disagrees, and the
workspace that should fix it (its newest writer that still exists, with live
agents). The writer fixes its own drift: never route a row to the user. A row
without a live writer goes to the lead.

When another tab has explicitly been appointed lead, read the open requests in
the shared hourly state, then scan and review the rows. Run
`requests-needing-owner` as described in `references/hourly-state.md` and
include every returned request in the handoff to the lead on every run. A
lane's `done` leaves the user's request open until the lead verifies the
original symptom is gone. Then do only the
cleanup allowed before a user choice by the Boundary and `cleanup.md`. Write the
questions and instructions for other lanes, including any cleanup done, in one
file and send it with `python3 ~/.claude/skills/orca-session-triage/scripts/handoff.py
send --body-file FILE --since <the NEXT_OFFSET of the run's first inbox read>
--decision KEY (once per question) --lead-worktree <the lead's worktree>`
(`--kind info` with nothing to hand off but cleanup). At the moment it sends,
it adds every inbox report that arrived after the scan began and the lead's
latest message and unanswered questions read again from its transcript, so the
handoff never rests on the scan's start. Ack the NEXT_OFFSET it prints. The
lead asks the user and directs the lanes. End without
starting a watch or changing the workspace comment.
Include each 「レビューの番」 PR URL and its command, or the missing source to
confirm, in this run's handoff to the lead. Do not start its review in the
hourly tab while another lead is appointed.
Name any Orca environment the scan could not reach in the handoff. Review
rows from the reachable Macs in the same run.

The lead does not send terminal input to a scheduled tab, so its answers reach
later runs through `scripts/lead_decisions.py`. Give each question in the
handoff a stable key in the form `hourly_state.py asked --decision` uses, such
as `remove:ENVIRONMENT::PATH`. `handoff.py` refuses to send while any
`--decision` key already has an answer and prints those answers, and records
each key it sends. Key a question about an Asana 99 value as
`asana:<item>:<owner/repo>#<number>`, item one of `complete`, `priority`,
`assignee`, `review-state`, `pr-state` (`asana:complete:example/app#12`):
until the key has an answer, `asana99_status.py` marks that difference
答え待ち and `asana_pr.py ensure` refuses to write it.
`lead_decisions.py show` gives the answers: follow each as
the lead's direction for its row, and name what you did in the handoff. When a
row's situation changes so the answer may no longer fit, ask under a new key.
The lead records an answer with
`lead_decisions.py record --decision KEY --answer-file FILE`; records are only
appended, and a later record for a key replaces its earlier answer.

When this run leads, read the shared hourly state before the scan. Match the
saved left rows to their newest final messages, retain pending questions, and
check promised handoffs and open requests. After the scan, run
`requests-needing-owner` and assign a live lane to each returned request or
raise the needed decision. Keep every request open until the lead observes
that its recorded symptom is gone. Then:

- Run the `backlog-watch` skill and include new notices and issues awaiting your reply in the hourly report.
- Send each `topic check --all` drift row to a live agent of the workspace it
  names with `send`, quoting the row and the fix it prints. When another lead
  is appointed, put the rows in the handoff instead.
- Every run, process the "次の一手" rows in asana99_status.py output with the
  asana-pr-review skill, even when its value differences were already judged.
  For 「レビューの番」, run the row's `review-pr ... --if-needed` command in this
  run. It skips a completed review of the same head and kind or an active
  review started with this option. If the row has no command, confirm the PR's
  source before starting. The independent review is a draft; do not submit a
  GitHub review from this step.
- Record each new question as `references/hourly-state.md` says, ask with
  AskUserQuestion, and set the triage workspace's Orca comment to「要判断: …」.
  Keep the tab with that unanswered question open. A later run works through
  other rows and never repeats the recorded question. Once answered, record
  the answer before directing a lane. Other rows' verdicts are acted on in
  every run, changed or not, so a finished or stalled row is handled again
  until it is closed; `cleanup.md` decides which removals need no question.
- When nothing needs the user, end with one line and no question.
- Before the comment, run the retrospective in a forked Agent as
  `references/retro.md` describes and wait for its one-line result.
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

## Confirmations approved in advance

Some lane confirmations were approved by the user in advance. They are listed
in one local file, `~/.cache/orca-briefs/standing-approvals.json`, with the
limits each entry checks and the user's original words, time, and where they
were given. Add an entry only with all three. The docstring of
`~/.cache/orca-briefs/bin/standing_approvals.py` describes what each kind
compares before answering.

The external watch answers a listed confirmation after it has waited the
file's `grace_seconds` without the lead's answer, and reports the answer as
`info`. Before then, a confirmation on a lane whose brief was delivered is
reported as `ask` once. A confirmation that differs from the list is left
for the lead. When the scan finds a lane stopped on such a confirmation and
the watch is not running, run
`~/.cache/orca-briefs/bin/standing-approve answer HANDLE --worktree PATH`.
Exit 0 means it answered; 3 means the lead still has time, and it recorded
when it first saw the prompt; 2 means the lead must answer. Hand anything it
leaves to the lead as before.

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

With terminal-notifier 3.x (nix-config installs it through zerobrew), pressing
the notification runs `orca terminal switch` for the sender's agent tab, adding
`--environment` when the ask came from a paired Mac. Without it, or when the tab
is gone, the notification only shows the ask. Activation records the PATH of
`orca` and `terminal-notifier`, so after installing terminal-notifier run
`launchctl bootout gui/$(id -u)/com.eotel.orca-session-triage.alert`, remove
`~/Library/LaunchAgents/com.eotel.orca-session-triage.alert.plist`, and run
`activate-alerts` again. On a Mac where `terminal-notifier -diagnose` reports
that permission was never requested, run
`open -a /opt/zerobrew/opt/terminal-notifier/terminal-notifier.app --args -message test`
once and allow notifications; a request from a terminal tab is refused.

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
- `scripts/precheck.py` skips while a scheduled turn is still dispatched and
  working, or its completed turn still owns a background job. A later human
  turn in that tab does not hold the next run.
  `python3 ~/.claude/skills/orca-session-triage/scripts/hourly_runs.py` lists
  the workspace's scheduled runs, newest first: scheduled and start time,
  status, the precheck's reason for a skipped run, the tab pane, and the
  Claude transcript (`--json` for a list). Read runs and their transcripts
  through it rather than from the automation's JSON or a shell loop over
  transcript paths, which contain spaces. A manual `orca automations run`
  does not run the precheck.
- Orca runs the precheck through `/bin/sh` with the login `PATH`, but in the
  repo's main checkout rather than the workspace, so the command names the
  workspace.
- The precheck reads this Mac's worktrees only, so keep the triage workspace
  on the local host.
