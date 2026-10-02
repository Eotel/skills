# Batching questions and sending answers

- Quote each `waiting` session's question from its transcript; Codex request
  titles and options come from the rollout. A request marked
  `answered_in_chat_later` was usually answered by a later user message: confirm
  before listing it. Put a recommended option first.
- Offer `finished` and `unstarted` worktrees in the same pass as close/keep
  choices, and each kept worktree's `closable` tabs as one close/keep choice per
  worktree. An `unstarted` agent's task reuses its repository's established
  request form, found in that repository's earlier transcripts (for example
  `$ship <issue URL>`).
- Questions go stale within minutes. Right before sending, re-read each target
  session's latest message and composer and drop answers that no longer apply.
- Send each answer as one line:
  `orca terminal send --terminal <handle> --text "<answer>" --enter --wait-submit 15 --json`.
  It landed when `result.send.prompt.stages` contains `turn_started`. A busy
  Codex shows the input queued until its next tool call; report that instead of
  resending.
- A pending AskUserQuestion in Claude Code is a selector ("Enter to select ·
  ↑/↓ to navigate"), not a prompt. Move the cursor with
  `orca terminal send --terminal <handle> --text $'\e[B' --json` (↓; `\e[A` is
  ↑), read the screen to confirm the highlighted option, then send a bare
  `--enter`. The transcript then records the chosen answer.
