# Hourly retrospective

A copy that inherits the hourly conversation's context reviews the run. The
original transcript is never changed.

## At the end of a live hourly run

After the handoff, start one Agent with `subagent_type: "fork"` and the prompt
"Run `python3 ~/.claude/skills/orca-session-triage/scripts/retro.py fork-prompt`
and follow its output." The fork inherits the run's context and cache. It lists
failures that later succeeded, each with verbatim quotes from the run, and
pipes them to `retro.py submit`. `submit` keeps only quotes found in the
transcript, asks a separate `claude -p` context to review the evidence and
risk, records the result in the adoption ledger, and marks the session done.
Wait for the fork's one-line result before ending the turn. If `submit` fails,
report its error; the Stop hook still queues the session for the service below.

## Finished sessions

The Stop hook appends the hourly transcript path to
`~/.cache/orca-session-triage/retro/queue.jsonl` after the existing review,
inbox, and Artifact checks allow the turn to end. A repeated stop is deduped.
The queue is local and private; a failed append never blocks the triage tab.

The external `retro.py run-service` process checks the one-minute Mac load
before processing one queued session that no fork has submitted. In the
session's worktree it runs
`claude -p --resume <session-id> --fork-session --no-session-persistence` on
the session's model, with tools, MCP servers, and hooks off. The copy answers
like the live fork, and the same review and ledger steps follow. The copy
reads the whole conversation without a cache. For seven 133k–202k-token
hourly sessions on Claude Opus 5.5 on 2026-10-27, the copy cost $1.17–1.79
and the review $0.09–0.25 per session. `claude -p` counts the original
conversation's cost in its total; `service.log` prints the copy's own
`cost_usd`, and an `omitted:` line for each lesson dropped with its reason.

Only short, supported facts about commands, paths, and errors are appended
automatically to `references/observed-triage-facts.md` in the selected skill;
its root `SKILL.md` gets one short pointer. The limit is three adoptions per
local night and three automatic notes per skill. A full skill gets a rewrite
proposal for morning review. Rules about human decisions, destructive actions,
permissions, procedures, and scripts wait for a person.

## Failures

A failed `claude -p` call records its exit code and the `is_error`,
`subtype`, and `result` fields of its JSON output in `worker-state.json` and
`service.log`; authentication errors appear only there. When the same reason
fails twice in a row, the worker sends one `triage-notify --kind ask`.
`retro.py status` shows `last_failure`. After fixing the cause, run
`retro.py retry --session-id UUID` for each failed session ID.

## Service and morning review

On the Mac hosting the hourly triage workspace, render and activate the
service from a shell whose `CLAUDE_CONFIG_DIR` names a signed-in Claude
profile, such as an Orca tab. The plist keeps that variable; launchd's default
profile may hold an expired OAuth session. The worker reads transcripts under
`~/.claude/projects`; Orca profiles link their `projects` there. Run
`python3 ~/.claude/skills/orca-session-triage/scripts/retro_service.py render`
to inspect the user service and `activate` to start it outside Orca tabs.
`activate` refuses to replace a different plist: stop the old service with
`launchctl bootout gui/$(id -u)/com.eotel.orca-session-triage.retro` and move
`~/Library/LaunchAgents/com.eotel.orca-session-triage.retro.plist` aside first.
`status` confirms launchd. The service log is
`~/.cache/orca-session-triage/retro/service.log`. A busy Mac leaves the
session pending for the next five-minute check.
This Mac must be the adoption ledger's configured `decision_writer`; only that
Mac can record automatic adoptions and morning decisions.
If the skill source checkout has a different path, set
`TRIAGE_RETRO_SKILLS_REPO` when rendering and activating the service.

Use the adoption-policy dashboard for morning decisions. `retro.py status`
shows queue and proposal counts. `retro.py process-once`
processes at most one pending session when the load permits.

Automatic changes in the skill source checkout require the normal Eotel/skills
PR, merge, and Mac-family installation before all Macs receive them. Review
the overnight skill diff before committing. Project-specific destinations
must follow their repository's AGENTS.md and CLAUDE.md before any commit.
