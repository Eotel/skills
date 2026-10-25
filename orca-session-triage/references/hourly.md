# Hourly run

An Orca automation starts `/orca-session-triage hourly` every hour in the
triage workspace. The automation's session leads triage. The unfiltered
`watch-inbox` runs as a macOS user service outside Orca tabs; owning a watch no
longer appoints a lead. A `--only` watch receives selected lanes' reports.
At the start of each run, use `scripts/inbox_read.py read` to see new inbox
entries in append order. After handling them, pass its `NEXT_OFFSET` to
`scripts/inbox_read.py ack <NEXT_OFFSET>`. The reader keeps its own cursor and
does not touch the watch's offset. A report pulled late from another Mac is
still shown even if its original timestamp is old. The service's output is in
`~/.cache/orca-briefs/triage-watch.log` for diagnosis.

## In the run

When another tab has explicitly been appointed lead, scan and review the rows, then do only the
cleanup allowed before a user choice by the Boundary and `cleanup.md`. Send questions
and instructions for other lanes to the lead in one `triage-notify --kind ask`
message, including any cleanup done. With nothing to hand off, report cleanup
with `--kind info`. The lead asks the user and directs the lanes. End without
starting a watch or changing the workspace comment.
Name any Orca environment the scan could not reach in the handoff. Review
rows from the reachable Macs in the same run.

When this run leads:

- Run the `backlog-watch` skill and include new notices and issues awaiting your reply in the hourly report.
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
- Leave no Monitor or other background job in this tab after the turn. A
  background job makes Orca report the tab as working and prevents reuse by
  the next hourly run. Publishing an Artifact starts a live
  watch (its result says `Live subscription: arming`): stop it right after
  publishing with ArtifactComments `action: "watch"`, `on: false`, and the
  artifact's `url`. The Stop hook names a watch still running.

Check the external watch with
`python3 ~/.claude/skills/orca-session-triage/scripts/inbox_watch_service.py status`.
The Stop hook names a stopped service. Do not start a replacement Monitor in
the tab.

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
   Monitors. Check the next scheduled run's tab before claiming reuse works.

An immediate alert for `kind=ask` could be added to this service later, with
a macOS notification containing the worktree and a prompt to read the inbox.
That would alert the person promptly; it would not wake or type into the lead
tab. Design and implementation need separate approval.

## While this session works on a request

The precheck skips every run while the lead works, so a long request leaves the
board unswept. When the Stop hook names a skipped run, the lead sweeps before
ending the turn: scan, ask every new question from the `waiting` rows, and
wake the rows `next-step.md` says to wake. Leave the rest to the next
scheduled run, unless the hook says several runs in a row were skipped: then
no scheduled run is cleaning up, so run the whole triage, cleanup included.
When another tab leads, scan and hand off as described above.
Codex ignores the hook: there, before ending a turn, look for
`skipped_precheck` runs since your last scan in
`orca automations runs --id <id> --json`.

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
- Any agent in the triage workspace holds the run, not only the triage
  session. A tab there that leads other lanes for hours skips every run (22 in
  a row, seen 2026-10-13): keep such tabs in another workspace.
- Orca runs the precheck through `/bin/sh` with the login `PATH`, but in the
  repo's main checkout rather than the workspace, so the command names the
  workspace.
- The precheck reads this Mac's worktrees only, so keep the triage workspace
  on the local host.
