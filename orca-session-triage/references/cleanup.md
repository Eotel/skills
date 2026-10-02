# Closing worktrees

A `finished` worktree whose `meaningful_tabs` is empty is done and left behind:
remove it without asking when `rm_check.py` reports `ok` with no `containers`,
and report it afterwards. Every other worktree waits for the user's choice.

Run these per worktree, in order.

1. `scripts/rm_check.py --worktree <path> [--pr <n>]`. Pass `--pr` when the PR may
   have been squash-merged: a MERGED PR whose head equals HEAD counts as merged.
   - `blockers` (uncommitted changes, HEAD outside the base) stop the removal;
     report them and leave the worktree.
   - `review` lists ignored paths that are neither caches nor identical copies of
     the main checkout. Open each one: per-worktree `.env` ports, generated
     sources, and test output (screenshots, uploaded fixtures) are regenerable;
     anything else (exports, reports, hand-written notes) goes back to the user
     before removal.
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
final message and classed it.

1. `orca terminal close --terminal <handle> --json` per approved handle. It stops
   that pane's process (`ptyKilled: true`) and removes a single-pane tab; `--tab`
   would also close any pane split beside it.
2. Confirm the handles are gone from `orca terminal list --json`.

Closing drops Orca's resume record, not the transcript: `claude --resume
<session-id>` or `codex resume <session-id>` in the worktree reopens the session.
