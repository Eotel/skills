# Hourly retrospective

The Stop hook appends the hourly transcript path to
`~/.cache/orca-session-triage/retro/queue.jsonl` after the existing review,
inbox, and Artifact checks allow the turn to end. A repeated stop is deduped.
The queue is local and private; a failed append never blocks the triage tab.

The external `retro.py run-service` process checks the one-minute Mac load before
processing one transcript. It records a failure and a later verified success,
checks the relevant skills and memory for overlap, and asks a separate
`claude -p` context to review the evidence and risk. It records candidate
lessons and source times through the adoption-policy ledger.
Only short, supported facts about commands, paths, and errors are appended
automatically to `references/observed-triage-facts.md` in the selected skill;
its root `SKILL.md` gets one short pointer. The limit is three adoptions per
local night and three automatic notes per skill. A full skill gets a rewrite
proposal for morning review. Rules about human decisions, destructive actions,
permissions, procedures, and scripts wait for a person.

## Service and morning review

After this change and `adoption-triage` are merged and installed, on the Mac
hosting the hourly triage workspace run
`python3 ~/.claude/skills/orca-session-triage/scripts/retro_service.py render`
to inspect the user service and `activate` to start it outside Orca tabs.
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
If `status` lists a failed session ID, inspect `service.log`, then run
`retro.py retry --session-id UUID` to queue that session again. The service
will pick it up on its next check.

Automatic changes in the skill source checkout require the normal Eotel/skills
PR, merge, and Mac-family installation before all Macs receive them. Review
the overnight skill diff before committing. Project-specific destinations
must follow their repository's AGENTS.md and CLAUDE.md before any commit.
