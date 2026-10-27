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

A lesson about a hard-to-use tool becomes a fix, as `adoption-triage`'s
policy asks: a change to a tool this setup maintains (category `script`) or
an upstream issue for one it cannot change (category `upstream`), with the
workaround as a separate fact until the fix ships. A fact that names a script
in Eotel/skills or `~/.cache/orca-briefs/bin` is never adopted automatically.
An adopted `script` or `upstream` proposal is not written as a note: the
worker records it as failed and sends the triage lead one `triage-notify
--kind ask` so that a lane takes it.

The skill list offers Eotel/skills, skills kept in a repository's
`.claude/skills` or `.agents/skills`, and the sources of skills installed by
the user's APM. A skill that APM deployed into a repository or home is written
at the source checkout its `apm.lock.yaml` names under `~/ghq/github.com`,
never at the deployed copy. Like Eotel/skills, that checkout must be on `main`
or `master` with the target files unchanged.

## Comments on decisions

A decision's `reason` from the adoption board is the deciding person's
comment. The worker's own `triage-retro` reasons are not comments.

- **Adopt with a comment:** a separate `claude -p` rewrites the note as the
  comment asks, within one line of 190 characters, and the rewrite is
  written instead of the proposal. The execution's `result` keeps the
  rewritten line; revoking removes that line.
- **Reject with a comment:** the twenty newest such proposals and their
  comments go into the next retrospective's prompt, live or service, so the
  same lesson is not proposed again.
- **Hold with a comment:** the worker writes nothing; the board notifies the
  triage lead.

## Failures

A failed `claude -p` call records its exit code and the `is_error`,
`subtype`, and `result` fields of its JSON output in `worker-state.json` and
`service.log`; authentication errors appear only there. When the same reason
fails twice in a row, the worker sends one `triage-notify --kind ask`.
`retro.py status` shows `last_failure`. After fixing the cause, run
`retro.py retry --session-id UUID` for each failed session ID.

An adoption or revocation the worker cannot apply is recorded as a `failed`
execution and retried at the next checks. When it stops after the third
failure (a script change stops at once), the worker sends one
`triage-notify --kind ask` with the proposal ID, the target skill, and the
reason.

## Service and morning review

On the Mac hosting the hourly triage workspace, render and activate the
service. Before each `claude -p` call, the worker reads Orca's
`~/Library/Application Support/orca/claude-profiles/selected-host`, uses that
profile with the local `USER`, and removes API credentials from the child
environment. The plist contains no Claude profile path. At startup, the worker
logs the authentication method and subscription type. It reads transcripts
under `~/.claude/projects`; Orca profiles link their `projects` there. Run
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
