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
marked 既読). Every read marks answered asks and questions with
「回答済み（MM/DD HH:MM・how）」: do not hand them off as open. Answers use
`~/.cache/orca-briefs/ask-answers.jsonl` through the board component; its storage
modes are in `references/hourly-state.md`. When a scheduled
run of this workspace due after this tab's run began, or the precheck skipped
one because this run was still working, `read` first prints a 「注意:」 line
with that run's time, status, and pane: the later run owns the read position
and the handoff, so leave them to it. An id may be its first 8 or more characters, as handoffs and
`~/.local/share/eotel-harness/current/bin/inbox` print it; one naming several entries lists them
and prints nothing. `read --text WORD` searches every entry's whole text and
structured questions, ignoring case and wherever the cursor stands, and prints
each match's header (time, kind, host, source, `id=`; 既読 when acknowledged)
with the lines holding WORD: search with it rather than in the 160-character
summary, which cuts words. Read a long inbox this way rather than cutting the output,
and read past entries by time or worktree with `inbox --since`. `--kind ask,done` filters by type, and
`--source skills/triage-request-ledger` filters by the displayed
`repo/worktree` (older entries without a repo use their worktree). A worktree
name alone, as backlog-watch prints it, also reads its `repo/worktree` when one
repo has it; a source no entry has names the near ones on stderr. A read with
`--kind`, `--source`, `--id`, or `--since` prints no `NEXT_OFFSET`, so an entry the filter
hid is never acknowledged: acknowledge from an unfiltered read. `--text`
prints none either.
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
to the hourly run. The lead watches new reports with a background Bash command
(`run_in_background`) running
`TRIAGE_READ_OFFSET="$HOME/.cache/orca-session-triage/lead-watch.offset" scripts/inbox_read.py watch --exit-on-event`,
and starts it again each time it ends, after reading the output file its
completion notice names (the notice carries no output). A Monitor stops after
30 minutes at most, `persistent` included; in a local session a background
command has no deadline and wakes the tab when it exits. With `--exit-on-event`
the watch lets a done, a waited info, a reminder or 伺い中 wait up to 10 minutes for the
next notice, so one wake covers several; a new ask ends the wait at once. One watch reads the
cursor at a time: a second one is refused while the first runs. The cursor is
the lead's own, which `initialize` starts once (watch
refuses a missing one, and refuses to run without `TRIAGE_READ_OFFSET`, so it
never skips `info` on the scheduled run's cursor).
It prints every new `ask` and `done`, whatever its source, as a summary line;
each `info` the lead waits on, with why: from a lane held until the lead's release
signal (`send --hold-until signal:<the lead's handle>`), or a lane's first report
since the lead sent to it; and `unanswered <n> min: <line>` once per ask left
unanswered for 15 minutes. The lines that come within 30 seconds go out as one
line, and the command ends there.
Being read is not being answered. `send --decision <answer> --ask <id>[:<n>]`
records the ask (or one of its questions) answered once the send lands (the
id's first 8 or more characters will do, as for `read --id`). When a later ask
of the same lane withdrew every question given (`triage-notify --withdraws`),
it sends nothing and names that ask: answer the newer ask instead;
`scripts/inbox_read.py answered <id> --note <why>` records one answered another
way (on another Mac, by the user in the lane, or no longer asked), and
`open-asks` lists the rest apart from handoffs, oldest first (`--json` for other tools; the board
reads it). It first records answered each board adoption notice whose proposals
already have a lane in the ledger, naming the lane on stderr; `~/.agents/skills/adoption-triage/scripts/ledger.py owner
<proposal id>` shows a proposal's lane, brief, and whether it is done.
`scripts/retro.py status --pending` lists undecided proposal IDs, titles, and
waiting minutes. `~/.agents/skills/adoption-triage/scripts/ledger.py list` prints one proposal per
line; `list --json` returns `{"items": [...]}`, each keyed by `proposal_id` (its `id` is the
proposing row's UUID). `list --json --id <proposal id>[,…]` (8 or more characters each) returns
only those proposals, with their decision times.
Before either tab asks the user an inbox ask itself, it records that with
`scripts/inbox_read.py claim <id>[:<n>],… --by <its worktree name>`.
Claims use `~/.cache/orca-briefs/ask-claims.jsonl` through the same component,
separately from answers. If another
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
When rephrasing a lane's question for AskUserQuestion, include its ask ID in the
question. Use `<ask id>:<number>` for a specific question in a multi-question ask.
When `send --ask` delivers an answer, it records the same answer and time in
each hourly question whose `covers` names that ask and each known
`answer:<lane>::<ask id>` decision key. Numbered answers settle only the
questions they cover; a whole-ask question waits for every part. A correction to one part
updates that part even after the whole ask was delivered. The inbox keeps delivery history
and reuses the recorded wording and time when the same session relays it.
For a handoff with decision keys, `inbox_read.py answered` requires
`--decision KEY=ANSWER` for every key and writes those answers to
`lead_decisions.py`. New decision text is checked against its recording time.
A replay of an identical decision uses its existing time. Missing answers write nothing and print the keys.

For a decision from before delivery provenance was recorded, a matching `send --ask` entry
with the same answer and timestamp identifies an automatic decision. If the logs do not establish
that match, correct the decision explicitly with `lead_decisions.py record --decision KEY --answer-file FILE`.
Check the original reply and the current decision before recording the correction.
A note alone remains a candidate in `lead_decisions.py unanswered`; it does
not settle a decision. Use `--handed-off-file`, `--handoffs-dir`, and
`--state-file` when working with isolated records.
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
The chezmoi-managed Claude Code settings register `scripts/stop_check.py` as
an AskUserQuestion PreToolUse hook. It applies in the hourly workspace and the
appointed lead's workspace even when this skill is not active. It matches inbox
question headers and the recorded questions' `covers` keys. An answered ask,
another workspace's claim, or another tab's unanswered hourly question blocks
the call, naming the question, workspace, and time. Direct input in the asking
lane's tab on this Mac after an open ask blocks once per ask and prompt timestamp.
The hook treats a direct choice as an answer when the input matches one option
of exactly one pending single-select question in that tab. It accepts a bare
choice or `/ship A` when the choice is `A`. The Board digest and `inbox open-asks`
record choices from local transcripts. The controller imports choices from
reporter Macs when it pulls their inboxes. Keep the original header,
options, and a shared source link or the ask ID when rephrasing a question.
For other replies, have the lane record the answer with
`triage-notify --answered <id>[:<n>]` as the hook reason indicates.
Its own claim and the current tab's question record permit the call. Keep the
appointed lead current with `hourly_state.py appoint-lead`.

The same settings register `scripts/lead_compact.py` for PreCompact,
SessionStart (startup and compact) and PostCompact, so the appointed lead keeps
its commitments through a summary. In the lead's session only, PreCompact asks
the summary to keep what the board records by id and everything else verbatim,
SessionStart prints `orca_board_commitments.py digest --max-tokens 3000`, and
PostCompact saves the summary with `orca_board_commitments.py save-summary`.
Other sessions get nothing. When the board cannot be read, the lead gets one
line naming the command to run by hand.

Before AskUserQuestion in the hourly workspace, the same hook refuses the call when any question's text, options and descriptions hold
no kana or kanji. While another tab is appointed lead (`hourly_state.py
appoint-lead`), it also refuses a question asked before this run sent its
handoff (`handoff.py last` older than the run's start): a question stops the
turn, so send the handoff first. With no lead, the run asks the user itself.
After AskUserQuestion, a PostToolUse hook in the same script names each answer
whose chosen option sends something out (a post, merge, send, push, or
production change) while the same question carries a note. Check how to read
the choice and the note with the user in one question before handing it to a
lane as permission; the hook does not judge whether they disagree. Record the
checked answer with `hourly_state.py answered --replace-reason-file`. The
same hook names each answered question whose inbox ask (the one showing it, or
the ask of an unanswered record with the same wording or made in this session
just before this call) a later ask of the same lane withdrew or names. Read that ask's text
and choices and check the answer still fits before recording and relaying it.
While the Mac's 1-minute load is above 300, a PreToolUse hook
(`scripts/bash_timeout.py`) raises each Bash call's timeout to 600000 when it
has none or a shorter one, so a tool that runs past the default 120 seconds
stays in the foreground and its output is read in place. Do not add the timeout
by hand.

## In the run

At the start, run
`~/.claude/skills/orca-session-triage/scripts/hourly_read.py`.
It runs cleanup, lead-state, open-asks, unanswered decisions, pending hourly
state, the last handoff, scan, backlog-watch, Asana status, topic checks, and
tracker gaps in parallel. After a successful scan it runs stale-asks against that scan. Each
invocation prints its unique directory under `~/.cache/orca-session-triage/reads`
and writes `results.json` there; its last line prints that path and shape.
`results.json` is a JSON array (not an object) with one record per command,
each with the keys `name`, `command`, `exit_code`, `stdout` and `stderr` (paths
of the command's output files), `result` (the path of its JSON result, or
null), `error`, `retry`, `compared_to`, `changes`, and `read_at` (Unix seconds). Standard output lists
the exit codes, one line per open ask, the stale-asks summary, and the read
directories used for comparison. Both ask results retain their JSON files.
At a one-minute load above 300, each command gets 2.5 times its normal timeout.
A command that exits 124 at that load runs once more with that timeout.
Backlog Watch keeps exit 124 when its notification cursor advanced or could
not be checked. Its initial output remains available.
The record retains the original and retry output and exit codes. A successful
scan, including a successful retry, runs stale-asks. Inspect every nonzero or
skipped exit status before using the results.
If Claude Code moves the command to the background, wait for its final
`results:` line before consuming the read.

For an individual read, `inbox_read.py open-asks --out FILE`,
`handoff.py lead-state --out FILE`, and `hourly_state.py stale-asks --from
SCAN_JSON --out FILE` publish complete JSON atomically without stdout.
`open-asks --out` implies JSON. `stale-asks --summary` is a separate text mode.

`cleanup_hourly_tabs.py` checks Orca's run, terminal and agent records before closing previous
scheduled tabs. Each per-tab `keep` or `closed` line includes the run's scheduled
and start times in local time (`scheduled`, `started`; `-` when unknown).
Workspace tabs without a matching run stay open with `keep: …: run の記録に無い`,
except one whose agents are done, whose rendered screen ends on a usage-limit
notice followed by the turn-end line with no reply between, whose composer is
empty, and that has printed nothing for `--min-idle-minutes`: a run the limit
stopped at once leaves no run record, and that tab is closed.
It keeps a tab if its turn or background work is active, a
question from that tab (or one without a recorded owner) is unanswered, its
latest conversation has an unanswered AskUserQuestion or no final message, its
transcript is missing or active within five minutes, or its rendered screen
shows a draft or a nonempty input field. A rendered `composer_empty: true`
overrides Orca's stale draft; a tab kept after a screen read shows `draft_text`
and `composer_empty`. A failed `worktree ps` is retried once after two seconds;
a second failure keeps the tabs and prints Orca's stderr.
When the only blockers are an old AskUserQuestion selector and its missing
final message, cleanup checks the entire batch against hourly state. Only when
every question is recorded and answered or withdrawn does it send Esc and
「別のタブで答え済み」. It then requires a started and ended response, idle agent
state, settled questions, and a fresh empty composer before closing. A missing
record or unconfirmed response keeps the tab.
Before closing a tab, it prints each
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

In every hourly run on a Mac that has `~/.local/share/eotel-harness/current/bin/topic`, run
`topic check --all`. Each drift row names the record, what disagrees, and the
workspace that should fix it (its newest writer that still exists, with live
agents). The writer fixes its own drift: never route a row to the user. A row
without a live writer goes to the lead.

In every hourly run, read `tracker-gaps.json` (`project-status --gaps --json`).
Each row in `gaps` is an item of a registered project assigned to the user (a
Backlog issue, a GitHub issue, an Asana task) that a workspace started but left
未対応, or left without the fields the shared rule asks for at start and finish
(`missing`: start date, due date, estimated or actual hours, or the issue's
「予定」 and assignee). `workspace` and `host` name the workspace that worked on
it; `fix` is the command that fills it. The workspace fixes its own item,
counting the user's own time as the shared rule says: never route a row to the
user. A row whose `workspace` is null, or whose send fails because the
workspace has no agent, goes to the lead.

`new` and `gone` compare gap keys with the most recent successful tracker-gaps
read under the same output root. A current `tracker:<key>` decision starting
with `放置:` marks a gap `ignored` and preserves the decision text and time.
Leave marked items with their recorded decision.
Asana status and Backlog Watch compare with the most recent successful read of
their own command in the same directory history, ordered by command read time.
Backlog compares issue rows without elapsed wait times. New notifications
remain in the command's output. Asana skips comparisons when review
requests were only partially retrieved.

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
with their enqueue times and elapsed minutes, read again from its transcript,
and under 「指揮が未読の ask」 each `unseen_asks` entry of `lead-state` below.
`--decision-ask KEY=ID` binds a decision to its inbox ask (repeat per key).
The lead defaults to the shared state's appointed worktree; `--lead-worktree`
overrides it, and `--state-file` selects the shared state for both `send` and
`lead-state`. `handoff.py lead-state` prints JSON with `worktree` (path),
`agents` (states and stop times), `final` (the lead's newest message of its
own and its time, also while a turn runs or after an API error such as a usage
limit ended it), `api_error` (that error's text, or null), `transcript` (the
current conversation's path, or null), `user_messages` (the user's ordinary
messages since the newest kept handoff, with `at` and `text`, excluding task
notifications), `pending_questions` (unanswered questions, choices, and ask times), `answered`
(questions answered since the newest kept handoff, with the choice and note per
question and ask and answer times), `rejected`
(questions closed without an answer since the newest kept handoff, with ask and
close times), `unseen_asks` (`id`, `ts`, `source`, `text`: the open asks that
reached the inbox before `final`, whose id's first 8 characters none of the
lead's transcripts holds outside queued input it was never given), `queued_input`
(undelivered instructions and enqueue times), and
`terminals` (handles and `screen_tail`, an array of the screen's last lines,
not a string);
`--orca` selects its CLI.
`lead-state --grep WORD [--since HH:MM|ISO]` adds `matches`: timestamped lead
and user speech, AskUserQuestion calls, and text tool results containing the
literal word, ignoring case. `--since` applies to the search; `user_messages`
still starts at the newest handoff. HH:MM means its most recent occurrence in
local time. Search reads the current transcript.

The scan and lead-state treat a connection error as past when the rendered
Claude screen shows a later reply/tool call (⏺) and a running status line,
with no tab still ending at the error. The old notice may have scrolled off.
Then `api_error` is null and `final` comes from the screen, with an unknown
time (`at: null`). A usage limit also accepts a completed later turn.

Pending questions answered through shared hourly state or delivered answers in ask-answers carry
`answered_elsewhere` (the answer time) and `answer_source`. A call whose every
question is answered also carries the last answer time and `close_selector`
commands, listing every heading, for tabs showing the matching selector. Read
the answer evidence before running one; the command checks the screen again.
A partial batch has annotations only on its answered questions and no close
command. Administrative answered marks are not answer evidence. Identical
hourly question wording under different lanes or decisions stays unannotated;
inbox questions must also identify one ask uniquely. Reading lead-state never
closes a selector.
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
rather than from transcripts. `handoff.py search WORD [WORD...] [--since
ISO|HH:MM]` prints each kept handoff whose text or decision keys hold any word
(case ignored), with its time, kind, and file, then the matching lines and
keys: find whether an earlier run handed a line off with it, not by grepping
the JSON. The
lead asks the user and directs the lanes. End without
starting a watch or changing the workspace comment (`hourly_state.py comment`).
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
ask the body names reached the inbox or the answer records after `--since`,
while a worktree a `remove:` key names sent an ask or info after it, and while
the lead handed work after it (`send` or `spawn-lane`, read from the board's
event log, `--board-events`) to a worktree the body names or a `remove:` key
names, printing each send's time, target, and opening text: a close candidate
or verdict written before that work is stale. Fix the body from them, then
pass `--seen ID` (repeat per id: a report's, an answered ask's, or a send's id,
or their first 8 or more characters) for each one the body now reflects. A delivered answer whose recorded sender worktree and session
both match this run is labelled 「（この回の送信）」 and needs no `--seen`.
Answer records from `send` and `inbox_read.py answered` now include that source;
an older record or an unknown session keeps the usual check. It also refuses
while a later scheduled run began or was skipped for this run, as `read`
prints it. `--force` sends past every check as
written, so use `--seen` for what the body already holds. Before writing the handoff, run `lead_decisions.py
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
ignoring case, and prints one line per answer. A line cuts its answer at 160
characters and then says 「…（続きは --json）」: before acting on a cut answer,
read it whole with `--json`, which prints the matching records with their full
answer, key, time, and the line's marks. `--since-last-handoff` selects
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
another value; without `"value"` it cannot compare. Write `{answered_at}` in
the answer where its time goes: both commands replace it with the recorded
time in JST (`17:18`), as `hourly_state.py answered` does, and refuse an
answer that still holds another `{snake_case}` placeholder, naming it.
`record-many` checks every line first,
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
- Send the `tracker-gaps` rows to their `workspace` with `send` (on its
  `host`), one message per workspace quoting each row's `text` and `fix`. When
  another lead is appointed, put the rows in the handoff instead.
- Every run, process the "次の一手" rows in asana99_status.py output with the
  asana-pr-review skill, even when its value differences were already judged.
  For 「レビューの番」, run the row's `review-pr ... --if-needed` command in this
  run. It skips a completed review of the same head and kind or an active
  review started with this option. To wait for an active review, run
  `review-pr <repo> <N> --wait`, which prints the result's path; the `.lock`
  file stays after the review ends, so never wait for it to disappear. If the
  row has no command, confirm the PR's source before starting. The independent
  review is a draft; do not submit a GitHub review from this step. When
  relaying findings, include the review status from the Asana sync. Keep
  「レビュー待ち」 until a person other than the PR author submits a review or posts
  a comment in the PR conversation or diff. The author's replies, independent
  or bot findings, and unsubmitted reviews leave that state in place. When the author has
  addressed all human findings and requested re-review, use 「指摘対応済み（未承認）」
  if the sync confirms it. For 「要確認」, read the review and comment bodies
  and check for outstanding findings before relaying the status.
  For 「指摘対応担当」, start the lane in this run with `spawn-lane --topic <PR URL>`.
- Record each new question as `references/hourly-state.md` says and ask with
  AskUserQuestion. Recording it sets the triage workspace's Orca comment to
  「要判断: …」 (「The triage workspace's comment」 there).
  Keep the tab with that unanswered question open. A later run works through
  other rows and never repeats the recorded question. Once answered, record
  the answer before directing a lane. Other rows' verdicts are acted on in
  every run, changed or not, so a finished or stalled row is handled again
  until it is closed; `cleanup.md` decides which removals need no question.
- Record each item the report lists for the user without an AskUserQuestion
  (a decision shown in its text, a wait carried from an earlier run) with
  `hourly_state.py presented`, so the next run reads it from `pending` and
  `stale-asks` as 提示済み・未回答 instead of from this run's transcript.
- When nothing needs the user, end with one line and no question.
- Before the comment, run the retrospective in a forked Agent as
  `references/retro.md` describes and wait for its one-line result.
- Last, run `~/.claude/skills/orca-session-triage/scripts/hourly_state.py
  comment --result "<what the run did>"`, so the board shows the last run
  below the open questions.
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

## Actions の未起動

GitHub Actions がジョブを起動しなかった理由は `gh api repos/O/R/check-runs/JOB_ID/annotations` で確認する。

組織の予算額と、上限に達した際に停止するかは
`gh api 'organizations/ORG/settings/billing/budgets?per_page=100&page=1'` で確認する。
`has_next_page` が `true` の間は `page` を進める。`budget_scope` が `organization` で、
`budget_product_skus` に `actions` を含む予算を確認する。単数形の `budget_product_sku` で返る場合は
値が `actions` の予算を確認する。
当月の純利用額は `gh api 'organizations/ORG/settings/billing/usage?year=Y&month=M'` の
`usageItems` から Actions の `netAmount` を集計する。Billing API へのアクセス権限は組織ごとに確認する。

## Permission and trust prompts

When a lane waits for permission to run a tool or asks whether to trust a
folder or hook, report the worktree, terminal handle, and prompt to the
operator. Leave the prompt open for the operator to answer in that terminal.
Check the lane again before retrying any pending delivery.

## External inbox service and cutover

`scripts/inbox_watch_service.py render` prints the launchd user agent for
review. It runs the existing `~/.local/share/eotel-harness/current/bin/watch-inbox` with the
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
- To change the scheduled prompt, print it with `hourly_runs.py --workspace
  <triage workspace path> --prompt > FILE`, edit FILE, and replace it with
  `hourly_runs.py --workspace <triage workspace path> --set-prompt FILE`. It
  replaces the prompt with the file's whole text (less its final newline),
  reads it back, and exits 1 printing the difference when Orca keeps another.
  The text must start with `/orca-session-triage hourly`, by which the tools
  find the automation. Outside the triage workspace, a missing automation names
  the workspaces that have one.
- Orca runs the precheck through `/bin/sh` with the login `PATH`, but in the
  repo's main checkout rather than the workspace, so the command names the
  workspace.
- The precheck reads this Mac's worktrees only, so keep the triage workspace
  on the local host.
