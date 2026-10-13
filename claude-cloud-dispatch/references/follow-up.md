# Steering a session and taking its PR

## While it runs

- `claude --teleport` and `/tasks` list sessions only interactively. Write each
  session ID beside its task when you start it (a `sessions.md` next to the
  briefs).
- Send a message without attaching:
  `claude -p "<message>" --cloud <session-id> --output-format json < /dev/null`.
  `{"ok": true, ...}` means it was queued; the session reads it at its next
  step.
- Progress shows on GitHub first: PRs whose head branch starts with `claude/`
  (seen 2026-10-13). Open the session page only when you need its own words.
- A session that stops on a permission prompt waits for the user, and can
  expire while it waits (docs). Tell them at once which session and what it
  asks; do not answer it for them.

## When its PR appears

- Review it outside the session: an independent reviewer that reads the diff,
  the issue, and the base branch. The session's own "all green" is a claim.
- If the base branch moved, ask the session to merge it in (no rebase, no
  force push) and watch CI again before merging. A check that fails only after
  that merge may be a flaky test another open PR fixes: read the failing test
  before sending the failure back.
- When the user asked you to merge, merge against the head you reviewed
  (`gh pr merge --match-head-commit <sha>`). On a squash, write the commit
  message yourself, and compare the squash commit's tree with the branch tip's.
- Do what the session could not: tracker entries, review requests, the note to
  another local session whose work the PR touches.
