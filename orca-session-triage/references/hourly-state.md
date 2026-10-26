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
final message, question, and answer. The CLI reads UTF-8 text from it.
When carrying a question or answer over from an older session, pass its actual
ISO 8601 time with `asked --asked-at` or `answered --answered-at`.
For `asked --decision`, use the same subject and action across runs, such as
`merge:PR#10`. A later PR in the same row gets its own identifier, such as
`merge:PR#11`. Inspect the source of the decision before assigning it.

| Decision | Before moving on |
| --- | --- |
| User says to leave a row | `leave --row ROW --final-message-file MESSAGE_FILE`. On later runs, `left-now --row ROW --final-message-file MESSAGE_FILE` exits 0 while the stored final message exactly matches; exit 1 makes it eligible again. |
| Ask a question | Check `show` for the same decision, including a differently worded version. If already recorded, do not ask it again; check whether its answer still applies. Otherwise record with `asked --row ROW --decision SUBJECT_AND_ACTION --final-message-file MESSAGE_FILE --question-file QUESTION_FILE` **immediately before** AskUserQuestion and keep its returned `id`. The script records the Orca pane that asked, so another run can leave that tab open while closing unrelated idle tabs. If presenting it fails, resolve that failed delivery before another run treats it as asked. |
| User answers | Record the exact answer with `answered --id ID --answer-file ANSWER_FILE` before relaying it or acting on it. A pending question has `answer: null`; the old tab stays open, but the next hourly run continues with other rows and does not repeat that question. If the answer arrived in another tab, relay it to the waiting tab and mark it answered before closing that tab. |
| User promises “when A settles, give B the task” | Record `promise --from-row A --to-row B --condition CONDITION --task TASK`. Every run checks undelivered promises against the latest A result. When the condition holds, send B the task as the Boundary permits. Record `delivered --id ID --delivery queued` or `--delivery turn_started` once that delivery is confirmed. |

`show` retains full final messages, asked times, answers, and handoff terms. It
does not infer a user's answer from a tab closing or from a later verdict.
Consult the actual answer and update the state. A waiting tab is not an
invitation to ask the same question in the new session.
