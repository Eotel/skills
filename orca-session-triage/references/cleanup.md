# Closing worktrees

A `finished` worktree whose `meaningful_tabs` is empty is done and left behind:
remove it without asking when `rm_check.py` reports `ok` with no `containers`,
and report it afterwards. Every other worktree waits for the user's choice. A
row with `main: true` is a repository's own checkout: its tabs close and the
checkout stays (`rm_check.py` blocks its removal).

Run these per worktree, in order.

1. `scripts/rm_check.py --worktree <path> [--pr <n>]`. Pass `--pr` when the PR may
   have been squash-merged: a MERGED PR whose head equals HEAD counts as merged.
   - `blockers` (uncommitted changes, HEAD outside the base) stop the removal;
     report them and leave the worktree. A commit whose patch is already in the
     base under another hash (a PR rebased before it merged) does not block.
   - `review` lists ignored paths that are neither caches nor identical copies of
     the main checkout. Open each one: per-worktree `.env` ports, generated
     sources, and test output (screenshots, uploaded fixtures) are regenerable;
     anything else (exports, reports, hand-written notes) goes back to the user
     before removal.
   - `regenerable` lists generated sources and e2e output (`__generated__/`,
     `src/paraglide/`, `e2e/screenshots/`, `e2e/.state/`, `e2e/.auth/`): a setup
     or a test run writes them again, so they do not hold up the removal.
   - `containers` lists docker compose projects defined inside the worktree.
2. Stop those projects with `docker compose -p <project> down` from the worktree.
   Keep named volumes unless the user approved deleting them, and say which
   volumes remain.
3. `orca worktree rm --worktree path:<path> --json`. Orca also deletes the
   checked-out local branch when it can prove it merged; a detached HEAD leaves
   the branch, so name it in the report.
4. Confirm the path is gone from `orca worktree ps --json` and from disk.
   `orca terminal show` keeps answering for the removed terminals with
   `orphaned: true, connected: false`: that is a stale record, not a live PTY.

A merged remote branch the user approved deleting: check
`gh api repos/<owner>/<repo>/compare/<default>...<branch>` reports `ahead_by: 0`,
then `gh api -X DELETE repos/<owner>/<repo>/git/refs/heads/<branch>`.

## Closing tabs in a kept worktree

A worktree's `closable` list names the tabs it can lose while the checkout stays:
shells whose last line is a bare prompt (`❯`) with no output for
`--shell-idle-minutes` (a finished `Setup` tab, a spare shell), and, once the
worktree is `finished` or `stalled`, its agent sessions, unless the final
message says work continues: a background job may still run in that tab. A
helper agent in a `working` or `waiting` worktree stays: its lead may send it
the next round. An `unsure` worktree keeps its agents until you have read the
final message and classed it. Shells close on the user's choice; agent sessions
follow "Clearing done cards".

1. `orca terminal close --terminal <handle> --json` per approved handle. It stops
   that pane's process (`ptyKilled: true`) and removes a single-pane tab; `--tab`
   would also close any pane split beside it.
2. Confirm the handles are gone from `orca terminal list --json`.

Closing drops Orca's resume record, not the transcript: `claude --resume
<session-id>` or `codex resume <session-id>` in the worktree reopens the session.

## Clearing done cards

A row's `done_cards` are the agents whose turn ended; the board keeps each one
in Done until its tab closes. Read the card's final message (the row's
transcript; the card's `last_message` when none was read; its screen when both
are empty), then:

- Something is left for the user: it is a question (`delivery.md`). An
  `interrupted` card is a request they cut short: ask whether its `prompt`
  still stands. The card closes once the answer is delivered and the lane ends
  its turn with nothing more for them.
- The last message is itself what they asked for (an explanation, a report)
  and the row is `unread`: report it as ready to read and keep the card.
- Nothing is left for the user (finished, or waiting only on a reviewer, a
  release, a deploy, or another lane): write what remains and who it waits on
  with `orca worktree set --worktree path:<path> --comment "<text>" --json`,
  close the tab with `orca terminal close --terminal <handle> --json`, then
  report it. The worktree, its files, and the transcript stay; when the review
  or deploy arrives, a later sweep reopens the session there (`claude --resume
  <session-id>` or `codex resume <session-id>`) or carries the step itself.
- A lead whose helper still works, and a session with the user's draft in its
  composer, stay: report each with that reason.

A null `handle` is a sleeping tab. `orca terminal close --worktree path:<path>
--all --json` retires every tab of that worktree, shells included: use it when
the worktree's live tabs are all in `closable`, and otherwise report the card
as asleep. A card whose worktree is removed goes with it; what it left for the
user is still a question. Confirm each closed card is gone from its worktree's
`agents` in `orca worktree ps --json`.
