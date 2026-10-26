# State shared by hourly runs

The hourly automation starts a fresh session each time. Read the single state
file with `python3 ~/.claude/skills/orca-session-triage/scripts/hourly_state.py show` at the start of the run and
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
| Ask a question | Check `show` for the same decision, including a differently worded version. If already recorded, do not ask it again; check whether its answer still applies. Otherwise record with `asked --row ROW --decision SUBJECT_AND_ACTION --final-message-file MESSAGE_FILE --question-file QUESTION_FILE` **immediately before** AskUserQuestion and keep its returned `id`. If presenting it fails, resolve that failed delivery before another run treats it as asked. |
| User answers | Record the exact answer with `answered --id ID --answer-file ANSWER_FILE` before relaying it or acting on it. A pending question has `answer: null`; the old tab stays open, but the next hourly run continues with other rows and does not repeat that question. If the answer arrived in another tab, relay it to the waiting tab and mark it answered before closing that tab. |
| User promises “when A settles, give B the task” | Record `promise --from-row A --to-row B --condition CONDITION --task TASK`. Every run checks undelivered promises against the latest A result. When the condition holds, send B the task as the Boundary permits. Record `delivered --id ID --delivery queued` or `--delivery turn_started` once that delivery is confirmed. |

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

Every hourly run reads the open requests in `show` and compares each assigned
lane with the scan. After the scan, run `requests-needing-owner`, passing
`--live-owner ROW_IDENTITY` once for each lane still actively handling its
assigned request. Send every returned request to the lead on every run, even
when that lane reported `done` earlier. The lead assigns a live lane or asks
for the needed direction. If the hourly run is the lead, it handles these
requests itself. A lane's `done` only changes its lane status; it does not close
the user's request.

The lead checks the original symptom in the real environment before closing
the request. Record the observation in a UTF-8 file and run `request-close
--id ID --evidence-file FILE`; `--closed-at ISO_TIME` preserves the actual
time when importing a prior verified closure. An implementation report, passing
test, or merged PR alone is not evidence that the symptom disappeared.

`show` retains full final messages, asked times, answers, handoff terms, and
requests with their owners and closure evidence. It
does not infer a user's answer from a tab closing or from a later verdict.
Consult the actual answer and update the state. A waiting tab is not an
invitation to ask the same question in the new session.
