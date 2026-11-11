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
only those entries in full, wherever the cursor stands (an acknowledged one is
marked 既読). An id may be its first 8 or more characters, as handoffs and
`~/.cache/orca-briefs/bin/inbox` print it; one naming several entries lists them
and prints nothing. Read a long inbox this way rather than cutting the output,
and read past entries by time or worktree with `inbox --since`. `--kind ask,done` filters by type, and
`--source skills/triage-request-ledger` filters by the displayed
`repo/worktree` (older entries without a repo use their worktree). A worktree
name alone, as backlog-watch prints it, also reads its `repo/worktree` when one
repo has it; a source no entry has names the near ones on stderr. A read with
`--kind`, `--source`, `--id`, or `--since` prints no `NEXT_OFFSET`, so an entry the filter
hid is never acknowledged: acknowledge from an unfiltered read.
After handling the entries, pass `NEXT_OFFSET` to
`scripts/inbox_read.py ack <NEXT_OFFSET>`. The scheduled run owns the default
`~/.cache/orca-session-triage/inbox-read.offset` cursor. Only the latest
scheduled triage tab may acknowledge that cursor; a run Orca marked
`dispatch_failed` on a restart still counts while its tab is connected. An older tab must
leave new inbox entries unacknowledged for the current run; it can finish without handling
them. After the Mac sleeps, several scheduled tabs run at once: a refused `ack` names
the run that owns the cursor (scheduled and start times, status, pane, and session). The separately
appointed lead in a separate worktree (currently `triage-lead` on the local
Mac) reads with
`TRIAGE_READ_OFFSET="$HOME/.cache/orca-session-triage/lead-inbox-read.offset"`
on both `read` and `ack`. When appointing a lead, hand over the unresolved
decisions first, then initialize that cursor once with the same environment
variable and `scripts/inbox_read.py initialize`. The command refuses to replace
an existing cursor; never reset it to skip unread entries. Record the appointed
worktree with `~/.claude/skills/orca-session-triage/scripts/hourly_state.py
appoint-lead --worktree <the lead's worktree>`.
Neither tab advances the other's cursor. Both see every
entry, but the lead owns answers to `ask` and directions to lanes. The hourly
run uses `info` and `done` for its sweep and sends unanswered `ask` entries
from other lanes to the lead in one handoff. It does not reforward its own
handoff entry. The lead checks inbox IDs and shared hourly state
before answering so a later hourly handoff cannot repeat an answered question.
The lead may read `info` and `done` for context but leaves the scheduled sweep
to the hourly run. The lead watches new reports with a Monitor running
`TRIAGE_READ_OFFSET="$HOME/.cache/orca-session-triage/lead-watch.offset" scripts/inbox_read.py watch`,
a cursor of its own that `initialize` starts once (watch refuses a missing one,
and refuses to run without `TRIAGE_READ_OFFSET`, so it never skips `info` on the
scheduled run's cursor).
It prints every new `ask` and `done`, whatever its source, as a summary line,
and `unanswered <n> min: <line>` once per ask left unanswered for 15 minutes.
Being read is not being answered. `send --decision <answer> --ask <id>[:<n>]`
records the ask (or one of its questions) answered once the send lands (the
id's first 8 or more characters will do, as for `read --id`);
`scripts/inbox_read.py answered <id> --note <why>` records one answered another
way (on another Mac, by the user in the lane, or no longer asked), and
`open-asks` lists the rest apart from handoffs, oldest first (`--json` for other tools; the board
reads it). It first records answered each board adoption notice whose proposals
already have a lane in the ledger, naming the lane on stderr; `~/.agents/skills/adoption-triage/scripts/ledger.py owner
<proposal id>` shows a proposal's lane, brief, and whether it is done.
`scripts/retro.py status --pending` lists undecided proposal IDs, titles, and
waiting minutes. `~/.agents/skills/adoption-triage/scripts/ledger.py list` prints one proposal per
line; `list --json` returns `{"items": [...]}`.
Before either tab asks the user an inbox ask itself, it records that with
`scripts/inbox_read.py claim <id>[:<n>],… --by <its worktree name>` (in
`~/.cache/orca-briefs/ask-claims.jsonl`, apart from the answers). If another
worktree already claims one of them, `claim` names that claim, writes nothing,
and exits 1: do not ask it (`--take-over` only once that tab is known not to be
asking). `open-asks`
then marks the ask 伺い中 with who and since when, the lead's `watch` prints the
claim once and stops reminding of it, `questions` leaves the claimed questions
out of its batch (listing them under `claimed`), and `stale-asks` names the
claim on the ask, so the other tab does not ask it again. `open-asks` and
`stale-asks` read that owner's current lead-state: with no unanswered AskUserQuestion
and a turn that ended over fifteen minutes ago, they show
「伺っていない（claim のみ、N 分）」. Inspect that state before taking over.
Asks from before the record began stay open until the lead marks
them: check `open-asks`, then `answered --before <ISO time> --note <why>`.
It marks only the asks an answer after them proves answered: a lead decision
or an AskUserQuestion answer (in the transcripts of the current directory's
worktree and each `--asked-in` worktree) that names the ask's id or quotes each
open question, or a delivered answer. It names the rest with the answers that
mention only the lane or its PR numbers: confirm each one, then mark it by id.
A report pulled late from another Mac is still shown even
if its original timestamp is old. The watch and notification services each
keep their own cursor. The watch's output is in
`~/.cache/orca-briefs/triage-watch.log` for diagnosis.
The Stop hook keeps the current scheduled run open while an `ask` is past the acknowledged
cursor, including on a repeated stop. Read it, handle it or hand it to the
appointed lead, then ack the reader's `NEXT_OFFSET` before ending the turn.
An ask the triage workspace sent from this Mac (the run's own handoff) does not
count.
After these checks permit the turn to end, the same hook queues this hourly
session's transcript. The external retrospective service skips a session the
run's own forked retrospective already recorded and reviews one it did not.
See `references/retro.md` for both and for reviewing their proposals.
Before AskUserQuestion in the hourly workspace, a PreToolUse hook in the same
script refuses the call when any question's text, options and descriptions hold
no kana or kanji. While another tab is appointed lead (`hourly_state.py
appoint-lead`), it also refuses a question asked before this run sent its
handoff (`handoff.py last` older than the run's start): a question stops the
turn, so send the handoff first. With no lead, the run asks the user itself.
After AskUserQuestion, a PostToolUse hook in the same script names each answer
whose chosen option sends something out (a post, merge, send, push, or
production change) while the same question carries a note. Check how to read
the choice and the note with the user in one question before handing it to a
lane as permission; the hook does not judge whether they disagree. Record the
checked answer with `hourly_state.py answered --replace-reason-file`.
While the Mac's 1-minute load is above 300, a PreToolUse hook
(`scripts/bash_timeout.py`) raises each Bash call's timeout to 600000 when it
has none or a shorter one, so a tool that runs past the default 120 seconds
stays in the foreground and its output is read in place. Do not add the timeout
by hand.

## In the run

At the start, run
`~/.claude/skills/orca-session-triage/scripts/cleanup_hourly_tabs.py`.
This checks Orca's run, terminal and agent records before closing previous
scheduled tabs. It keeps a tab if its turn or background work is active, a
question from that tab (or one without a recorded owner) is unanswered, its
latest conversation has an unanswered AskUserQuestion or no final message, its
transcript is missing or active within five minutes, or its rendered screen
shows a draft or a nonempty input field. Before closing a tab, it prints each
line of its final message that asks the user something as
`carry: HANDLE: LINE`; add each to this run's list of what awaits the user
(the handoff, when another tab leads). When Orca has no provider session ID,
the script requires one transcript whose scheduled prompt matches the run's
start time. A tab Orca detached from
the window (`orphaned`) whose run recorded no pane is matched by the provider
session Orca's agent hooks saved for its recorded pane. Each tab gets one
`closed:` or `keep:` line with the reason. Inspect `keep:` output and retry on
a later hourly run; leave the workspace open.

In every hourly run, run the Asana 99 sweep as `asana-pr-review` describes and
read its `asana99_status.py` 「次の一手」,
including when `--judged` hides unchanged differences. A 「レビューの番」 row
contains the exact `review-pr` command with `--kind` and `--if-needed` when
the PR's source is known. If the row has no command, confirm who created the
PR before choosing `cloud` or `colleague`. A 「指摘対応担当」 row is an Open PR
of ours with requested changes not yet addressed and no live agent in a
workspace linked to it: it needs a lane, started with
`spawn-lane --topic <PR URL>` so later sweeps see the lane as 「指摘対応確認」.
A 「指摘対応確認」 row needs a check that the linked lane is working on the
changes. A 「再依頼確認」 row is an Open PR that Asana marks 指摘対応済み（未承認）
while its task is still assigned to the PR author: check that the reviewer was
requested again, and assign the task to that reviewer. A 「答えと違う」 row is a
lead answer whose recorded value Asana does not hold: write the answer with
`asana_pr.py ensure`.

In every hourly run on a Mac that has `~/.cache/orca-briefs/bin/topic`, run
`topic check --all`. Each drift row names the record, what disagrees, and the
workspace that should fix it (its newest writer that still exists, with live
agents). The writer fixes its own drift: never route a row to the user. A row
without a live writer goes to the lead.

When another tab has explicitly been appointed lead, read the open requests in
the shared hourly state, then scan and review the rows. Run
`requests-needing-owner --from SCAN_JSON` and `stale-asks --from SCAN_JSON` as
described in `references/hourly-state.md` and include every `needs_owner`
request, each `closed_issues` request to close, and each `stale-asks` entry, in
the handoff to the lead on every run. A
lane's `done` leaves the user's request open until the lead verifies the
original symptom is gone. Then do only the
cleanup allowed before a user choice by the Boundary and `cleanup.md`. Write the
questions and instructions for other lanes, including any cleanup done, in one
file and send it with `~/.claude/skills/orca-session-triage/scripts/handoff.py
send --body-file FILE --since <the NEXT_OFFSET of the run's first inbox read>
--decision KEY (once per question)`
(`--kind info` with nothing to hand off but cleanup). At the moment it sends,
it adds every inbox report that arrived after the scan began and the lead's
latest message, unanswered question count, and undelivered queued instructions
with their enqueue times and elapsed minutes, read again from its transcript.
`--decision-ask KEY=ID` binds a decision to its inbox ask (repeat per key).
The lead defaults to the shared state's appointed worktree; `--lead-worktree`
overrides it, and `--state-file` selects the shared state for both `send` and
`lead-state`. `handoff.py lead-state` prints JSON with `worktree` (path),
`agents` (states and stop times), `final` (the lead's newest message of its
own and its time, also while a turn runs or after an API error such as a usage
limit ended it), `api_error` (that error's text, or null),
`pending_questions` (unanswered questions, choices, and ask times), `answered`
(questions answered since the newest kept handoff, with the choice and note per
question and ask and answer times), `rejected`
(questions closed without an answer since the newest kept handoff, with ask and
close times), `queued_input` (undelivered instructions and enqueue times), and
`terminals` (handles and screen tails);
`--orca` selects its CLI.
`--claude-projects` selects the transcript root for both commands.
Do not sign the body: it ends the
text with `<session name>（HH:MM の定時）`, the name ListAgents prints first and
the session's start time, and replaces a signature the body ends with. When it
says on stderr that it sent without one, the handoff went unsigned. Ack the
NEXT_OFFSET it prints: it is
past the run's own handoff entry, unless another report reached the inbox
first, which then stays unread for this run. Each handoff it sends is kept in
`~/.cache/orca-session-triage/handoffs/` with its kind, keys, and time;
`handoff.py last` prints the newest, so read the previous run's handoff with it
rather than from transcripts. The
lead asks the user and directs the lanes. End without
starting a watch or changing the workspace comment.
Include each 「レビューの番」 PR URL and its command, or the missing source to
confirm, in this run's handoff to the lead. Do not start its review in the
hourly tab while another lead is appointed. Hand each 「指摘対応担当」 PR to the
lead the same way, as a lane for the lead to start with `spawn-lane --topic`.
Name any Orca environment the scan could not reach in the handoff, and each
`hook errors` line of `scan_sessions.py --from SCAN_JSON --summary` (the Mac
and the hook file it could not find: that Mac's checks do not run). First check
on that Mac that the file is still missing: a tab that has not run since keeps
the old error on its screen. Review rows from the reachable Macs in the same
run.

The lead does not send terminal input to a scheduled tab, so its answers reach
later runs through `scripts/lead_decisions.py`. Give each question in the
handoff a stable key in the form `hourly_state.py asked --decision` uses, such
as `remove:ENVIRONMENT::PATH`. `handoff.py` refuses to send while any
`--decision` key already has an answer and prints those answers, and records
each key it sends. It also refuses, and prints what it found, while a report
from another tab of this workspace (a parallel scheduled run) or an answer to an
ask the body names reached the inbox or the answer records after `--since`:
fix the body from them, or pass `--force` to send it as written. Before writing the handoff, run `lead_decisions.py
unanswered` without arguments: it prints `{"unanswered": [...], "settled": [...]}`.
Each unanswered key includes `first_handed_off_at`, `handed_off_at`, and
`handoff_count`. Settled keys include the decision or bound ask's answer record;
old handoffs without an ask binding still read. For a
key naming PRs or issues (`merge:owner/repo#136`, `merge:owner/repo#12+#14`,
`spawn:owner/repo#6303::review-fix`, `spawn:owner/repo#159`) it adds each one's
`type` (`pr` or `issue`), `state`, `updatedAt`, and `updated_after_handoff`, read
with one GitHub query per repository, plus inbox reports naming the key, PR
reference, or URL, split before and after the last handoff. An issue also has
`worktrees`: the Orca worktrees linked to it or to a PR that closes it (`[]` for
none, null when Orca could not be read). `--asana-status OUT_JSON` adds each PR's current `asana` object from the
run's saved status output; without it, no Asana values are attached. A
repository gh cannot read leaves them null and is named on stderr. Carry each
key still open into this handoff. When its PR changed since (merged, closed,
or updated), check whether the question still stands: drop it, or ask under a
new key. Do not collect keys from earlier handoffs or read PRs one at a time.
Key a question about an Asana 99 value as
`asana:<item>:<owner/repo>#<number>`, item one of `complete`, `priority`,
`assignee`, `review-state`, `pr-state` (`asana:complete:example/app#12`):
until the key has an answer, `asana99_status.py` marks that difference
答え待ち and `asana_pr.py ensure` refuses to write it.
`lead_decisions.py show` gives the answers: follow each as
the lead's direction for its row, and name what you did in the handoff. When a
row's situation changes so the answer may no longer fit, ask under a new key.
`show --match WORD` (repeat per word) matches any word in the key or answer,
ignoring case, and prints one line per answer. `--since-last-handoff` selects
answers since the newest saved handoff; `--handoffs-dir` selects that directory.
These views show newest answers first in JST and mark a `remove:` key 未実行
while its path still exists. `--history` adds the answers a later record of
the same key replaced, marked 置き換え済み (under `replaced` in the JSON): read
it when a key's latest record, such as 「中継済み」, does not say the answer.
The lead records an answer with
`lead_decisions.py record --decision KEY --answer-file FILE`, and several with
`lead_decisions.py record-many --file FILE` (or on standard input), one JSON
object per line: `{"decision": KEY, "answer": TEXT}`, adding `"decided_at"`
(ISO 8601 with its offset) for an earlier answer. An answer on an Asana 99
item also takes `"value"`, what it puts there (`record --value`): `true` or
`false` for `complete`, the Asana user gid for `assignee`, the option name for
the others. `asana99_status.py` shows a 「答えと違う」 row while Asana holds
another value; without `"value"` it cannot compare. `record-many` checks every line first,
names each wrong one, and records nothing until all are right; do not call
`record` in a shell loop. When the lead relays the answer to a lane, it uses
`send --decision <answer> --key KEY` instead (repeat per key, with `--ask <id>`
for the ask it answers): the key and the ask are recorded answered once the
send lands. Records are only appended, and a later record for
a key replaces its earlier answer. `record` and `record-many` warn, and still
record, when a text that only says the answer was passed on (「中継済み（…）」)
replaces an answer.

When this run leads, read the shared hourly state before the scan. Match the
saved left rows to their newest final messages, retain pending questions, and
check promised handoffs and open requests. After the scan, run
`requests-needing-owner --from SCAN_JSON`, assign a live lane to each
`needs_owner` request or raise the needed decision, and close each
`closed_issues` request with `request-close`. Run `stale-asks --from
SCAN_JSON` and handle each entry as `references/hourly-state.md` says. Keep every other request open
until the lead observes that its recorded symptom is gone. Then:

- Run the `backlog-watch` skill and include new notices and issues awaiting your reply in the hourly report.
- Send each `topic check --all` drift row to a live agent of the workspace it
  names with `send`, quoting the row and the fix it prints. When another lead
  is appointed, put the rows in the handoff instead.
- Every run, process the "次の一手" rows in asana99_status.py output with the
  asana-pr-review skill, even when its value differences were already judged.
  For 「レビューの番」, run the row's `review-pr ... --if-needed` command in this
  run. It skips a completed review of the same head and kind or an active
  review started with this option. To wait for an active review, run
  `review-pr <repo> <N> --wait`, which prints the result's path; the `.lock`
  file stays after the review ends, so never wait for it to disappear. If the
  row has no command, confirm the PR's source before starting. The independent
  review is a draft; do not submit a GitHub review from this step. For
  「指摘対応担当」, start the lane in this run with `spawn-lane --topic <PR URL>`.
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
`~/.claude/skills/orca-session-triage/scripts/inbox_watch_service.py status`.
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
   `~/.claude/skills/orca-session-triage/scripts/inbox_read.py initialize`
   once while the old Monitor is still running. The new reader starts at that
   byte position; later arrivals are retained.
2. Ask the lead to stop the old `watch-inbox` Monitor in its tab and end its
   turn. Do not send terminal input to that tab. New inbox lines remain in the
   inbox while the watch is stopped.
3. Run `~/.claude/skills/orca-session-triage/scripts/inbox_watch_service.py activate`.
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
`~/.claude/skills/orca-session-triage/scripts/inbox_watch_service.py activate-alerts`
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
  --precheck "~/.claude/skills/orca-session-triage/scripts/precheck.py --workspace <triage workspace path>" \
  --json
```

- An existing workspace with `--fresh-session` gets a new tab and session on
  each allowed run, without making a new worktree.
- `scripts/precheck.py` skips while a scheduled turn is still dispatched and
  working, or its completed turn still owns a background job. A later human
  turn in that tab does not hold the next run.
  `~/.claude/skills/orca-session-triage/scripts/hourly_runs.py` lists
  the workspace's scheduled runs, newest first: scheduled and start time,
  status, the precheck's reason for a skipped run, the tab pane, and the
  Claude transcript (`--json` for a list). `--tail` adds each run's last
  utterance and whether an AskUserQuestion still has no result; `--questions`
  adds each AskUserQuestion with its answer and times, so a run cut short
  leaves its answers to the next. Read runs and their transcripts
  through it rather than from the automation's JSON or a shell loop over
  transcript paths, which contain spaces. A manual `orca automations run`
  does not run the precheck.
- Orca runs the precheck through `/bin/sh` with the login `PATH`, but in the
  repo's main checkout rather than the workspace, so the command names the
  workspace.
- The precheck reads this Mac's worktrees only, so keep the triage workspace
  on the local host.
