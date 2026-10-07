# Hourly run

An Orca automation starts `/orca-session-triage hourly` every hour in the
triage workspace. Run the triage as usual; this page covers what differs.

## In the run

- Ask with AskUserQuestion and set the triage workspace's Orca comment to
  「要判断: …」. The open question holds the session, and later runs skip until
  the user answers, so each question is asked once.
- A row the user chose to leave in an earlier run of this session stays left
  until its newest final message changes. Only those: every other row's
  verdict is acted on in every run, changed or not, so a finished or stalled
  row is offered again until it is closed.
- When nothing needs the user, end with one line and no question.
- Last, set the triage workspace's comment to the run time and what the run
  did, so the board shows the last run.
- End the run with no background shell, monitor, or helper left. Orca counts
  the session as working while one runs, and the precheck then skips every run
  until it exits.

## While this session works on a request

The precheck skips every run while this session works, so a long request
leaves the board unswept. On 2026-10-07 five runs (11:00 to 15:00) were
skipped while this tab handled the user's other requests, and three agents'
questions waited up to 2 hours 40 minutes.

Before ending a turn in this session, list the automation's runs
(`orca automations runs --id <id> --json`). When one was `skipped_precheck`
since your last sweep, sweep before ending the turn: scan, ask every new
question from the `waiting` rows, and wake the rows `next-step.md` says to
wake. Leave the rest to the next scheduled run.

## The automation

```sh
orca automations create --name "session triage" --trigger hourly \
  --prompt "/orca-session-triage hourly 報告と質問は日本語で" --provider claude \
  --workspace "id:<repoId>::<triage workspace path>" --reuse-session \
  --precheck "python3 ~/.claude/skills/orca-session-triage/scripts/precheck.py --workspace <triage workspace path>" \
  --json
```

- An existing workspace with `--reuse-session`: a run goes to the previous
  run's session once that one is done, so one tab holds every run and the
  session remembers what it asked. A new worktree per run would pile up on the
  board it triages.
- Orca starts a scheduled run even while the previous one waits on the user,
  then in a new tab. `scripts/precheck.py` skips the run while any agent in the
  workspace works, is blocked, or waits. A skipped run is recorded with the
  precheck's output (`orca automations runs --id <id> --json`). A manual
  `orca automations run` does not run the precheck.
- Orca runs the precheck through `/bin/sh` with the login `PATH`, but in the
  repo's main checkout rather than the workspace, so the command names the
  workspace.
- The precheck reads this Mac's worktrees only, so keep the triage workspace
  on the local host.
