# Steering a session and taking its PR

## While it runs

- No CLI lists cloud sessions. Write each session ID beside its task when you
  start it (a `sessions.md` next to the briefs).
- Send a message without attaching:
  `claude -p "<message>" --cloud <session-id> --output-format json < /dev/null`.
  `{"ok": true}` means it was queued; the session reads it at its next step.
- Progress shows on GitHub first: PRs whose head branch starts with `claude/`.
  Open the session page only when you need its own words.
- A session that stops on a permission prompt waits for the user. Tell them
  which session and what it asks; do not answer it for them.

## When its PR appears

- Review it outside the session: an independent reviewer that reads the diff,
  the issue, and the base branch. The session's own "all green" is a claim.
- If the base branch moved, ask the session to merge it in (no rebase, no
  force push) and watch CI again before merging. A check that fails only after
  that merge may be a flaky test another open PR fixes: read the failing test
  before sending the failure back.
- Merge as the repository's practice says, against the head you reviewed
  (`gh pr merge --match-head-commit <sha>`). On a squash, write the commit
  message yourself, and compare the squash commit's tree with the branch tip's.
- Do what the session could not: tracker entries, review requests, the note to
  a local lane whose work the PR touches.
