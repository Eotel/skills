---
name: orca-row-classify
description: Classes the rows of an orca-session-triage review queue (worktrees whose state the triage scan could not settle) from each row's final message, PR, git state, and Orca comment, and writes one verdict per row. Invoked by orca-session-triage with the directory holding review-queue.json; not for direct use.
user-invocable: false
context: fork
agent: general-purpose
model: sonnet
background: false
allowed-tools: Read Edit(~/.cache/orca-session-triage/**)
---

# Class the triage review queue

`$ARGUMENTS` is a directory (when it is not filled in, as in Codex, use the
directory the caller named). Read `review-queue.json` there, give every row in
`rows` one verdict, and write `review-result.json` to the same directory. You
see only these files: nothing from the triage conversation reaches you, so
judge each row from its own facts.

## Each row carries

- `path`, `repo`: the worktree. A remote row has `environment` and `identity`;
  copy its `identity` into the verdict so equal paths on different Macs stay distinct.
- `claude.final_text` / `codex.last_assistant` / `orca_message.text`: the agent's newest final
  message (its last part). `claude.last_user`: the prompt it answered.
- `comment`: the worktree's Orca comment, written by the agent or the triage.
- `pr`: the worktree's PR as GitHub reports it now (`state` OPEN / MERGED /
  CLOSED, `review`, `latest_human_activity` from a person other than the PR author,
  `requested` reviewers, `checks`), or null.
- `git`: `dirty` (uncommitted paths) and `unpushed` (commits ahead of the
  branch's upstream). Both are unknown on a remote Mac until its checkout is
  inspected there. An open PR whose head matches HEAD has that commit even if
  `unpushed` is positive because the branch tracks the PR's base.
- `reasons`, `jev`: why the scan could not settle the row.
- `open_pr_cleanup: true`: the checkout may be removed without asking after
  `rm_check.py` clears it. This does not settle a historical transcript's
  question or the PR's review.

## Rules

1. **Facts beat claims.** `pr` and `git` are read live where available; the message and the
   comment may be hours old. A new `pr.latest_human_activity` from another person
   supersedes an older message about waiting for review. A message saying
   "未コミット" while `git.dirty` is 0 has been committed since; a comment saying
   "merge 待ち" while `pr.state` is MERGED is merged. On a merged PR,
   `git.unpushed` above 0 or `head_matches`
   false is what a squash merge or a later merge of main leaves behind: it does
   not keep the row open. When the message reports work on other PRs than `pr`
   (the worktree moved on to new work), those PRs decide; say so in `evidence`.
   A claim the facts disprove is not a leftover: leave it out of `remaining`.
   `pr.reply_after_last_commit` and `waits_on: self` mean a human added a
   review, PR comment, or line comment after the latest commit date. Read it
   and give the lane `in_progress` if it can respond now. GitHub does not
   guarantee that the commit date equals its push time; do not claim exact push
   order from this flag alone.
2. **Who must act decides the class.** When several fit, the first one in this
   list wins.
   - `waiting`: the message or comment asks **the user who runs the triage** to
     decide, approve, or do something (`要判断`, "どうしますか", "教えてください",
     a choice offered to them), or a kind (b) leftover below is left. Put the
     ask in `question`: verbatim when the message asks it, in a few words of
     your own when it is a (b) item the message only lists.
   - `blocked`: the next move belongs to someone else: a reviewer, a customer, a
     tester, another lane, a deploy, or an environment or access someone must
     provide (a production connection). A question the agent sent to that person
     (on Backlog, GitHub, Slack) is `blocked`, not `waiting`. Name them in
     `waits_on`.
   - `in_progress`: the agent can do a kind (c) item below **now**, without
     waiting for anyone (a failing check it must fix, a step it said it would
     take next).
   - `done_unmerged`: the work is done and the PR is open with no reviewer
     requested and no review: someone must request a review or merge.
   - `finished`: the PR merged (or the task needed none) **and** no leftover
     of kind (c) below is left. An `open_pr_cleanup` row without a historical
     message is also `finished`; when it has a message, classify its remaining
     question or task as usual. The cleanup flag decides removal independently.
3. **Leftovers: keep each one and say whose it is.** List every open item from
   the message and the comment ("残り", "残っていること", "未確認", "needs its own
   issue") in `remaining`, one short line each. Then sort each into the first
   kind that fits:
   - (a) **Someone else carries it**: everything a worker lane leaves goes to
     its coordinator,
     decisions included; an item for a new issue or another repository leaves
     with it. It stays in `remaining` only and does not keep the worktree open.
     A worker lane is a row whose `claude.last_user` is a dispatch brief from a
     coordinator (it names the coordinator's terminal handle or says "You are
     a dispatched worker"); a row that receives orchestration notices is the
     lead, not a worker.
   - (b) **Only the user can decide it**: a choice about data, scope, or
     policy the agent left open (「〜の扱い」, "needs a separate decision"), an
     approval, a login, needed **now**. Put it in `needs_user` too, even when
     the message does not phrase it as a question. An approval the agent will
     ask for after someone else acts (merge once the review lands) is not
     needed now: it stays in `remaining`.
   - (c) **This worktree's agent must still do it**: a deploy or step it said
     it would take, open issues it says it will continue, a check on the base
     branch that went red after its merge and that the message says needs a
     fix without naming another owner. The row is not `finished`.

   An item that fits none of them (a follow-up already filed as an issue, a
   check nobody said they would do) stays in `remaining` only.
4. **No message, no guess.** When the final message is empty or says nothing
   about the state (an empty string, `[]`, `{}`, or null counts as empty),
   class the row from `pr`, `git`, and `comment`, say so in
   `evidence`, and keep `confidence` at 0.5 or below.

## Write

`review-result.json`:

```json
{"queue_generated_at": "<generated_at copied from review-queue.json>",
 "rows": [{"path": "<row path>", "identity": "<remote row identity, if present>", "class": "blocked",
           "question": null, "waits_on": "hdknr (PR #358 review)、k-mizokami (PR #827 review)",
           "next_step": "none until the review arrives",
           "remaining": ["..."], "needs_user": [],
           "evidence": "<a quote of at most 200 characters, or what you used instead>",
           "confidence": 0.9}]}
```

One entry per queued row, in queue order. Copy `identity` when present. A field that does not apply is
null (`question`, `waits_on`) or `[]` (`remaining`, `needs_user`); `waits_on`
is one string, several parties joined with "、". Write the free-text fields in
the language of the row's messages, keeping quotes as written; `next_step` is
free text. A `finished` row's
`next_step` is "close the worktree". Then reply with one line per row:
`<worktree name> | <class> | <question if set, else waits_on, else next_step>`,
where the worktree name is the last segment of `path`.
