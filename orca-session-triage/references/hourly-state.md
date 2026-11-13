# State shared by hourly runs

The hourly automation starts a fresh session each time. Read the single state
file with `~/.claude/skills/orca-session-triage/scripts/hourly_state.py show` at the start of the run and
again immediately before asking or acting on a saved decision. The default is
`~/.cache/orca-session-triage/hourly-state.json`. The script locks and replaces
it on writes. Keep the file when changing the automation; it carries decisions
across sessions. If it is malformed, repair it from its contents before running
another triage: the script refuses to overwrite it.

The commands below use that script path. They accept `--state-file PATH` before
the subcommand for an isolated copy. Use a local scratch file for each exact
final message, question, answer, original request, symptom, and closure evidence.
The CLI reads UTF-8 text from it.
When carrying a question or answer over from an older session, pass its actual
ISO 8601 time with `asked --asked-at` or `answered --answered-at`.
For `asked --decision`, use the same subject and action across runs, such as
`merge:PR#10`. A later PR in the same row gets its own identifier, such as
`merge:PR#11`. Inspect the source of the decision before assigning it.

| Decision | Before moving on |
| --- | --- |
| User says to leave a row | `leave --row ROW --final-message-file MESSAGE_FILE`. On later runs, `left-now --row ROW --final-message-file MESSAGE_FILE` exits 0 while the stored final message exactly matches; exit 1 makes it eligible again. |
| Ask a question | Check `show` for the same decision, including a differently worded version. If already recorded and not withdrawn, do not ask it again; check whether its answer still applies. After withdrawal, the same row and decision create a fresh id and retain the old record. Otherwise record with `asked --row ROW --decision SUBJECT_AND_ACTION --final-message-file MESSAGE_FILE --question-file QUESTION_FILE` **immediately before** AskUserQuestion and keep its returned `id`. Add `--covers KEY` per lead decision key the question settles. The script records the Orca pane that asked, so another run can leave that tab open while closing unrelated idle tabs. When every issue or PR the question names is CLOSED, it records nothing and prints the evidence; how it reads them, and the merged PRs it names, are under 「Issues and PRs a question names」. If presenting it fails, resolve that failed delivery before another run treats it as asked. For several questions, `asked-many --file JSONL --from SCAN_JSON` takes one object per line: `decision`, `question`, optional `asked_at`, `covers`, and `session`, and either `ask` (an inbox ask id; the scan row of the worktree that sent it, told apart by its tab when two Macs share the path, and the ask's text become the row and final message) or `row` and `final_message`. Every line is checked first; a wrong one records nothing and is named with its line number. A line whose question is settled by CLOSED references is not recorded either: after the JSON, stderr names each such line and its references, and the command exits 2. Pass the AskUserQuestion's own time as `asked_at` when recording after the call, so `pending` pairs the record with that call. |
| User answers | Record the exact answer with `answered --id ID --answer-file ANSWER_FILE` before relaying it or acting on it; `--decision KEY` in place of `--id` finds the one open question asked under that key and refuses several, naming their ids. `answered-many --file JSONL` records several from one object per line (`answer`, `id` or `decision`, optional `answered_at` and `replace_reason`), checking every line first as `asked-many` does. Write `{answered_at}` in the answer where its time goes: it becomes the recorded time in JST (`13:03`). An answer without it that gives a time of day (`13:03`, or `12:1x` for 12:10–12:19) 10 minutes or more from the recorded time is refused, naming the gap; with it, other times in the text stay as written. It records the tab (or worktree) that recorded it; a different answer already recorded is refused with that answer, its time, and the tab. When the user's checked reading replaces a recorded answer (a note on an outward choice), pass `--replace-reason-file`: the earlier answer, its time and tab stay under `replaced` with the reason. A pending question has `answer: null`; the old tab stays open, but the next hourly run continues with other rows and does not repeat that question. If the answer arrived in another tab, relay it to the waiting tab and mark it answered before closing that tab. When a later ask of the same lane withdrew the question's inbox ask (`triage-notify --withdraws`) or names its id, `answered` still records, adds `reissued_by` (each such ask's `id`, `ts`, `withdrew`), and names it on stderr: read that ask's text and choices before relaying the answer. |
| The user must act outside the chat (sign a CLA, register a secret) | `user-wait --text-file FILE [--row ROW] --pr-check OWNER/NAME#N CHECK --secret NAME OWNER/NAME...` records what the user is to do with at least one way to check it: a check run on a PR (repeat per check), or a secret that each listed repository must hold (repeat per secret). `--check-command COMMAND` adds a trusted read-only shell check (repeatable); exit 0 means done. It runs in the workspace where it was recorded, with a 60-second timeout. `stale-asks` runs every check again and omits waits whose checks all pass; failed or unreadable checks remain visible with their exit code or reason. Build the current user-action list from this read, rather than copying the previous report or treating the raw `pending.user_waits` records as current waits. Once its result is seen, `user-wait-close --id ID --evidence-file FILE` closes it. |
| Nothing will answer it | `withdraw --id ID --reason-file FILE` closes a question no answer will settle, keeping the reason and time. Add `--replaced-by ID` when an existing question replaces it. `answered` and `answered-many` leave withdrawn questions unanswered, name the replacement on stderr and in `replaced_by`, and exit 2; valid answers in the same batch are still recorded. Inspect the replacement before answering it. |
| User promises “when A settles, give B the task” | Record `promise --from-row A --to-row B --condition CONDITION --task TASK`. Every run checks undelivered promises against the latest A result. When the condition holds, send B the task as the Boundary permits. Record `delivered --id ID --delivery queued` or `--delivery turn_started` once that delivery is confirmed. |

## The triage workspace's comment

The script writes the triage workspace's Orca comment from the state with
`orca worktree set --worktree path:<workspace> --comment`; do not write it by
hand. Its first line is 「要判断: 」 and the first line of each question asked
in that workspace still open, joined with 「／」; its second line is the last
run's 「HH:MM の定時: <result>」. A line with nothing to show is left out, and
with neither the comment is cleared.

- `asked` and `asked-many` write it in the workspace of the hourly triage
  automation, read from Orca in an Orca tab. `--comment` writes it in another
  workspace, and `--no-comment` skips it.
- `answered`, `answered-many`, and `withdraw` rewrite the comment of the
  workspace the question was asked in, once the script has written that
  comment, so an answer the lead records takes the question off.
- `comment --result "<what the run did>"` sets the run's line, HH:MM being
  when this Claude Code session started, and writes the comment. `comment`
  without `--result` writes it again with the saved line.

A comment Orca did not take is named on stderr; the record stands. `comment`
then exits 1.

## User requests and their symptoms

The lead records each new user request with `request-open --original-file FILE
--symptom-file FILE` when receiving it. Use the user's exact words for the
original request and a separately observable symptom. Pass `--owner
ROW_IDENTITY` when a lane is already assigned; otherwise the request remains
unassigned and appears in `requests-needing-owner`. The entry records its
opening time; use `--opened-at ISO_TIME` when importing an older request.
Check `show` before registering a request so a fresh session does not duplicate
it. `request-assign --id ID --owner ROW_IDENTITY` assigns or replaces the
responsible lane when work passes to another lane. Use the scan's
`row_identity`: the absolute worktree path on the local Mac, or
`ENVIRONMENT::PATH` for another Mac. A repository and worktree name alone can
identify more than one lane across Macs.

`requests-needing-owner` also records open GitHub issues as requests. List
the repositories in `issue-repos.json` beside the state file
(`{"repos": ["owner/name", ...]}`, kept on this Mac only). Each open issue is
recorded once, with its title and URL as the original request and its
`source.url`. Its owner is the worktree Orca links to that issue or to a
PR that closes it; an ownerless one gets that worktree when it appears. The
lead assigns the rest like any request. A failed `gh issue list` is read once
more; a repository gh still cannot read is skipped with a warning, and nothing
is imported while the worktrees are unreadable.

Every hourly run reads the open requests in `show` and compares each assigned
lane with the scan. After the scan, run `requests-needing-owner --from
SCAN_JSON` with the JSON the scan printed, saved to a file. The rows take the
verdicts in `~/.cache/orca-session-triage/verdicts.json` as `scan_sessions.py
--from` does, including those `orca-row-classify` wrote after the scan was
saved. Each row with an agent tab whose class (the verdict's, else the scan's)
is not `finished` is a live owner, and the issue links come from the same rows,
so Orca is not read again. Add `--live-owner ROW_IDENTITY` for a lane the scan did not show that is
still handling its assigned request. Without `--from`, the command reads Orca
itself, skips a paired Mac that does not answer within 15 seconds, and treats
only the `--live-owner` lanes as live.

It prints `{"needs_owner": [...], "closed_issues": [...]}`, each entry with its
`id`. Send every `needs_owner` request to the lead on every run, even when that
lane reported `done` earlier. The lead assigns a live lane or asks for the
needed direction. If the hourly run is the lead, it handles these requests
itself. A lane's `done` only changes its lane status; it does not close the
user's request. `closed_issues` lists the requests whose GitHub issue is
missing from its repository's open issues read in this run; `decided_by` names
that read. Their recorded symptom, the issue being open, is gone, so they are
candidates to close: the lead closes each with `request-close`, citing that
read, without opening the issues one by one. A repository gh could not read,
or whose open list reached gh's 1,000-issue limit, decides nothing, and its
requests stay in `needs_owner`.

The lead checks the original symptom in the real environment before closing
the request. Record the observation in a UTF-8 file and run `request-close
--id ID --evidence-file FILE`; `--closed-at ISO_TIME` preserves the actual
time when importing a prior verified closure. An implementation report, passing
test, or merged PR alone is not evidence that the symptom disappeared.

## Questions left in front of no one

After the scan, run `stale-asks --from SCAN_JSON --asked-in <the lead's
worktree> --asked-in <the triage workspace>`; without `--asked-in` it reads the
worktree `appoint-lead` recorded. It leaves out the calling tab's own row and a
left row whose final message is unchanged. It reads shared state and sets each
eligible `waiting` row (the verdict's class, else the scan's), each row blocked on Miura,
each `in_progress` row with a recorded question, each row with a shared unanswered
question, and each row named by an unanswered handed-off ask key against its lane's inbox
reports and the answers `scripts/answer_evidence.py` finds (the lead's latest
decision per key, AskUserQuestion answers and ordinary user messages in those
worktrees' transcripts, and the answers `send` delivered with `--ask`, with or without `--decision`), and prints them longest waiting first with `since` and
`age_minutes`. An answer proves only the ask named by its decision key, or,
without such a key, one whose id it names, whose structured questions it quotes
in full, or to which it was delivered; one naming only the
lane or a PR number of the ask, or a user message containing a word from the
question, is listed in `candidates` for you to check. Each item also carries
the row PR's completed independent review (`review`: head, verdict, path,
`head_matches`, `changes_requested`, as `scan_sessions.py --row` gives it) and
whether its question asks about a merge (`merge_question`); `--summary` prints
both on the item's line. Every entry also carries `pr` (`state`, `head`,
`updatedAt`), read in one batch per repository. An item with the lane's ask
also carries `related_issues`, each 「関わる issue」 its questions name (`ref`,
`state`, `updatedAt`, read in one batch per repository; a Backlog key has no
state), and `related_issues_closed`, true when every one is closed or merged;
`--summary` then adds 関わる issue が閉じた. Its question may no longer stand:
check that before putting it to the user. A screen question's `asked_at`,
from the live session's transcript including a fork, takes priority over the
parent's last message when reporting its waiting time.

- `unanswered`: an ask of the lane that is open, or was sent in its last turn,
  has no proof, or its handed-off key is still unanswered. `ask.marked: true`
  means it was marked answered anyway and
  left `open-asks`: check `candidates`, then ask it again or record the answer.
  `ask.claimed` (`by`, `at`) means another tab claimed it and is asking the
  user now (`inbox_read.py claim`); `--summary` adds 伺い中. If lead-state shows
  no unanswered AskUserQuestion and its turn ended over fifteen minutes ago,
  `inactive_minutes` and 「伺っていない（claim のみ、N 分）」 identify the old
  claim. Check that state before `claim --take-over`.
- `stopped_after_answer`: an answer came after the lane's last message and the
  lane has not spoken since. A `done` or `info` from the same repository and worktree after the newest answer also proves progress, even when the transcript still shows an older final message; that row is omitted. Relay `answer` (the newest) to a lane still listed.
- `settled`: no ask of the lane is open, and a lead decision key naming the
  row (`ask:<name>::…`, the item's `name`) has a record after the lane's last
  message, with no prompt on its screen. `evidence` gives each such key's
  newest record that decides something, past a later 「中継済み」 that only says
  it was passed on. Do not ask it again.
- `unasked`: those asks were answered before the lane's last message, or it
  has none, so `open-asks` does not show its question. `choices_in_info` lists
  choices the lane sent as `info`, and `candidates` answers that may have
  settled it. Put the question in front of the user or the lead.
- `rejected`: an AskUserQuestion in the `--asked-in` transcripts (and the
  current directory's) closed without an answer since the newest saved
  handoff (`--handoffs-dir`). `name` is the worktree that asked it, `questions`
  the questions with their choices, `since` and `closed_at` when it was asked
  and closed. Ask it again, or record why it no longer stands.
- `user_wait`: an open `user-wait`, with its `checks` read in this run.
  `--summary` adds 確かめ: and each check's current state. A wait disappears
  from this read when every check passes. Its record remains available for
  inspection and explicit closure with `user-wait-close`.

An `unanswered` or `stopped_after_answer` item, and a row's shared unanswered
question, carries `reissued` when a later ask of the same lane withdrew its ask
or one of its questions (each such ask's `id`, `ts`, `withdrew`); `--summary`
adds 出し直しあり. Put the newer ask in front of the user, not the old one.

An unfulfilled one-shot CronCreate whose session no longer runs appears as
`lost_schedule`, with its schedule and prompt opening. A plain `in_progress`
verdict without a question is work to resume, not a user question.

`show` retains full final messages, asked times, answers, handoff terms, and
requests with their owners and closure evidence. `pending` lists what is
still open (unanswered questions, undelivered handoffs, open requests, and left
rows), plus settled questions with evidence, unrecorded transcript answers,
open user waits (`user_waits`), and `unrecorded_questions`, each as a list entry carrying its `id` (`row` for a left row); read the
open items with it rather than walking `show`'s dictionaries by hand, and
`get --id ID` for one entry. A question is settled when it was withdrawn, every
`--covers` key has a lead answer after it was asked, or every issue and PR it
names is CLOSED; each unanswered question's `prs` lists them with their
`type`, current state, head, and update time, read in one batch per repository.
An unanswered question whose inbox ask a later ask withdrew carries `reissued`,
as in `stale-asks`. `unrecorded_questions` lists the questions of an
AskUserQuestion still waiting for its answer in the transcripts of the
current workspace, each `--asked-in` worktree, and the workspaces questions
were recorded in, beyond the records that account for
it: the records of that workspace asked in the 30 minutes up to the call (of
the call's own session when they name one, and not paired with an earlier
call) pair with its questions, and each
question left over is listed with its `workspace`, `session`, `tool_use_id`,
and `since`. Record it with `asked --asked-at` set to that `since` and
`--session` set to its `session` (`asked_at` and `session` in an `asked-many`
line), from any workspace; it is recorded even when every issue or PR it names
is CLOSED. `withdraw`
that record when no answer will come (its session ended). An
unanswered one lists the answers to the same question found in the
recorded worktrees' transcripts as `unrecorded_answers`. `pending --summary` and
`stale-asks --summary` print one line per item: name, state, since,
age_minutes, the ask id (or the entry's id), and the question's start. It
does not infer a user's answer from a tab closing or from a later verdict.
Consult the actual answer and update the state. A waiting tab is not an
invitation to ask the same question in the new session.

## Issues and PRs a question names

`asked`, `asked-many`, `pending`, and `stale-asks` read the issues and PRs a
question names by the rule `scan_sessions.py` reads a verdict's `waits_on`
with: `owner/name#N` and GitHub URLs as written, `name#N` as the repository of
the row's organization with that name (or the only one whose name ends in
`-name`), and a bare `#N` in the repository named before it in the question,
else the row's origin. A word of letters written before ` #N` that no
repository of the organization has as a hyphen-separated part of its name
(`merge #10`) is prose, and that `#N` is bare. A name no repository of the organization has, and a
bare `#N` of a row on another Mac, stay unresolved: `repo` is null, no state is
read, and `--summary` prints （リポジトリ不明）#N. Name the repository in the
question (`owner/name#N`) when it matters.

A newly recorded question that names an issue also gets `merged_after_basis`:
for each such issue, its newest merged PRs that close it or name it, merged
after the ask the question came from (`asked-many --ask`), or every one when
that time is not known. stderr names them too. The question may rest on what
such a PR changed: read the PR before putting the question to the user.
