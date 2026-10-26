# Batching questions and sending answers

- Before showing Miura a file for business users, check it against the current
  acceptance criteria for those files.
- Quote each `waiting` session's question from its transcript; Codex request
  titles and options come from the rollout. A request marked
  `answered_in_chat_later` was usually answered by a later user message: confirm
  before listing it. Put a recommended option first.
- Before asking a decision that a verdict or final message leaves open, search
  that lane's user messages and, in an hourly run, the shared questions and
  answers in `hourly-state.md`. A final
  message that offers to change course ("if you decide otherwise, I will fix
  the PR") reports a decision already made: ask only the step still open, such
  as the merge.
- Read each row's `done_cards` with its final message. What is left for the
  user (a decision, an approval, a login or check only they can do, a request
  they cut short) is a question in this pass, quoting what is left. A card with
  nothing left for them closes without a question (`cleanup.md`).
- Offer `finished` worktrees that `cleanup.md` does not remove without asking,
  and `unstarted` worktrees, in the same pass as close/keep choices. Include
  each `stalled` row's next step (`next-step.md`) and each kept
  worktree's `closable` shells as one close/keep choice per worktree. An
  `unstarted` agent's task reuses its repository's established request form,
  found in that repository's earlier transcripts (for example
  `$ship <issue URL>`).
- Before asking the user about a Backlog ticket or posting to it, run
  `~/.cache/orca-briefs/bin/ticket-activity <KEY>` and drop the questions and
  posts another tab already settled. On 2026-10-19 the triage asked about two
  tickets and drafted a reply on a third that a lead tab in another workspace
  had already settled.
- Questions go stale within minutes. Right before sending, re-read each target
  session's latest message and composer and drop answers that no longer apply.
- Before relaying a lane's question to the user, write one concrete example: who
  uses which screen, does what, and what happens. If the lane's question cannot
  support that example, return it to the lane for a clearer question before
  asking the user.
- A null `handle` on a done card can mean its tab is asleep or no live tab can
  be assigned to it uniquely. Check the worktree's agent terminals and
  transcript first.
  Use a live terminal only when it clearly belongs to that session. If no tab
  of that agent type is live, reopen the same session in its worktree using the
  transcript's session ID and the resume command in `cleanup.md`, then ask it
  to rewrite the question. If tab ownership remains ambiguous or the session
  cannot be reopened, report a blocked next step with the reason; keep the
  unclear question out of the user's decision list.
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
