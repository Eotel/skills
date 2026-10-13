# Giving lanes work and receiving their reports

- **Reports come through the inbox, not this tab.** Text a lane sends with
  `orca terminal send` lands in the user's half-typed message. Put this line at
  the top of every brief: "report to triage with
  `python3 ~/.claude/skills/orca-session-triage/scripts/triage_notify.py "<text>"`;
  never `orca terminal send` to the triage tab". Receive with Monitor running
  `scripts/watch_inbox.py <offset-file>`; when the Monitor expires, start it
  again with the same offset file and no report is skipped. Lanes still message
  each other with `orca terminal send`.
- **Change the channel only for lanes this tab directs.** `orca terminal list`
  shows the other triage tabs; their lanes report to them and do not read this
  inbox.
- **Find where an unnamed screenshot or question comes from before dispatching
  it.** Search its fixed text in the candidate repositories and read the other
  triage tabs' latest output. If the source is still unclear, ask the user
  which system it is.
- **Before writing a brief or an AGENTS.md, read `writing-for-agents` and
  `codex-prompting`; before running several lanes in one repository, read
  `worker-contract`.** Quote the user's rule verbatim: add no condition, change
  no verb, and give a check that settles a borderline case instead of "when in
  doubt, take the strict side".
