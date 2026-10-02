# Next step for a stalled worktree

A `stalled` row has nothing running and nothing asked, yet its worktree is
open: it waits on the user as surely as a question does. Give every one a
proposed next step in the same pass as the questions.

Read the row's `git`, `pr`, Orca comment, and latest message, then propose:

| State | Next step |
|---|---|
| Reason "final message says work continues, yet no agent runs" | read its screen: a helper, background job, or CI still running means it is working, so leave it; otherwise the lane continues or says where it stopped |
| Reason "final message waits on someone else" | blocker: name who it waits on, no question |
| Reason "final message says done" | the work is done but not merged or closed: the rows below pick the step |
| `git` is null | the checkout is missing or unreadable: look at the path first |
| `pr.head_matches` is false | the PR found is not this checkout's HEAD (a reused branch name): confirm the PR before using its state |
| `git.dirty` or `git.unpushed` above 0 | the lane commits and pushes, or discards |
| No PR, commits beyond the base | the lane opens a PR |
| `pr.review` is `APPROVED` and checks pass | merge |
| No review requested and none given | request a review, or merge |
| Review requested, none given yet | blocker: waiting on that reviewer, no question |
| `CHANGES_REQUESTED`, or review comments not yet answered | the lane addresses the review |
| PR merged, or nothing left to do | close the worktree (`cleanup.md`) |

- Repository practice decides who reviews and who merges; the user picks the
  step. Before offering, check the author's recent merged PRs
  (`gh pr list -R <repo> --state merged --author <login> --json mergedBy,latestReviews`)
  and the repository's agent memory.
- Open items written in the Orca comment or the latest message ("要確認",
  "残り", "未確認") are questions: list them with the row.
- When one row waits on another's merge or deploy, say which row unblocks which
  and order the steps that way.
- Send the chosen step to the row's lane as one line, as with answers, so the
  lane carries the CI and deploy follow-ups. Run it yourself only when the lane
  is gone.
- Keep the board truthful: `orca worktree set --worktree path:<path>
  --workspace-status in-review` while a reviewer is the blocker, `completed`
  once the work is done.
