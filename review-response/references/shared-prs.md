# PRs Other Actors Can Change

Read before pushing to, or merging, a PR that another session or a person can also
act on.

- Before touching an existing branch, look for a live owner: another session on
  that branch, a worktree with uncommitted work, or a remote head that keeps
  moving. When one exists, hand it a patch instead of pushing.
- Re-read the PR's state and head immediately before every push and every merge.
  A commit pushed after the PR merged never reaches the base branch; keep it for
  the next PR on the same work.
- When an approval arrives while fixes are still in flight, say on the PR who will
  merge it and after what, so nobody merges the older head.
- Merge at the verified head (for GitHub, `--match-head-commit`), then watch the
  landed head's checks to completion. Do the same for merges made by others.
- After a PR lands, read what else landed in the files it touched since its base.
  A merge that applies cleanly can still leave two statements that contradict each
  other, and checks do not see it.
