# Giving lanes work and receiving their reports

- **Reports come through this tab's inbox, not its terminal.** Text a lane sends
  with `orca terminal send` lands in the user's half-typed message. Give each
  triage tab its own inbox,
  `~/.cache/orca-session-triage/${CLAUDE_SESSION_ID}/inbox.jsonl`, and put the
  recipe below at the top of every brief with that path filled in. The quoted
  heredoc keeps quotes, `$`, and backticks in a report from being run.

  ```sh
  python3 ~/.claude/skills/orca-session-triage/scripts/triage_notify.py <inbox> <<'EOF'
  <report>
  EOF
  ```

  Receive with Monitor running
  `python3 ~/.claude/skills/orca-session-triage/scripts/watch_inbox.py <inbox> <offset-file>`;
  when the Monitor expires, start it again with the same two paths and no report
  is skipped or repeated. Lanes still message each other with
  `orca terminal send`.
- **Change the channel only for lanes this tab directs.** `orca terminal list`
  shows the other triage tabs; their lanes report to them.
- **Find where an unnamed screenshot or question comes from before dispatching
  it.** Search its fixed text in the candidate repositories and read the other
  triage tabs' latest output. If the source is still unclear, ask the user
  which system it is.
- **Before writing a brief or an AGENTS.md, read `codex-prompting` and
  `agentic-docs` (and `writing-for-agents` where it is installed); before
  running several lanes in one repository, read `worker-contract`.** Quote the
  user's rule verbatim: add no condition, change no verb, and give a check that
  settles a borderline case instead of "when in doubt, take the strict side".
