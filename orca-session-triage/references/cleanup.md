# Closing approved worktrees

Run these per approved worktree, in order.

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
