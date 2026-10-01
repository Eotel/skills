---
name: review-response
description: Handle code reviews that came back on many PRs at once (items returned to the author in a tracker, approvals with optional points, post-merge follow-up issues) and drive each PR to its end state. Use for bulk review response, not for one PR's comments.
---

# Review Response

## When

- Reviews came back on several PRs and the user wants them fixed, answered,
  merged, or re-requested in one run.
- A single PR's comments belong to the repository's review-handling skill.

## Where

- `references/triage.md`: building the work list and each item's end state. Read
  before dispatching any work.
- `references/shared-prs.md`: pushing to or merging a PR that other sessions or
  people can also change.
- Progress lives in one local status file from the start of the run: a JSON list
  of units rendered to Markdown with `scripts/render_status.py`, one row per unit
  with its state, next step, and links. The orchestrator updates it at every
  milestone so the user can see what is running, what is waiting, and what was
  left out of scope.
- `scripts/`: tracker listing and updates, PR and CI watchers, and the status
  table renderer. Each script documents its options with `--help`; tracker IDs
  and repository aliases come from the caller's configuration, not from this
  skill.
- Implementation goes through `codex-orchestration-core`, and through
  `codex-tdd-orchestration` when parallel workers are wanted.
- The team's tracker conventions (task naming, reviewer mapping, when a review
  task is complete) stay in the team's own review skill.

## Boundary

- Merging needs the user's authorization. A run-level authorization (for example
  "approved PRs may be merged once fixed") covers only PRs whose review approves
  merging, after each requested or optional point is addressed or answered, at
  the verified head. Without it, an approved PR's end state is "ready to merge"
  and the user merges.
- Replies, review re-requests, follow-up issues, and tracker updates are part of
  the run. Closing PRs or issues, merging anything else, and production writes
  need the user's explicit word.
- Scope choices a review leaves open (whether to fix follow-up issues now, which
  option an issue should take) go to the user once, with a recommendation.

## Done

- A fresh pull of the tracker shows every returned item at its end state (see
  `references/triage.md`), and out-of-scope items are listed with a reason.
- Every commit that landed during the run has finished its checks, and any red
  result has an owner.
- The status file lists each unit with links to its PRs and issues.
