---
name: plan-exec
description: Create and execute a repo-local implementation plan for cross-system, high-risk, or multi-turn work. Skip routine single-file changes.
---

# Plan And Execute

Use a file-backed plan when the work crosses subsystems, changes a public or
security-sensitive contract, or spans multiple turns. A plan is unnecessary for
routine, bounded edits whose diff explains the work.

A plan tells the next agent what to do. It is not a work log: history makes the
next agent read more and decide less.

## Start the plan

Follow `docs/PLANS.md` and `docs/exec-plans/plan-template.md` when present.
Otherwise create:

```text
docs/exec-plans/active/{yyyy-mm-dd}-{2-6-word-kebab-slug}.md
```

Use the repository timezone and include:

- Goal
- Context: current facts needed to resume
- Scope and Non-Goals
- Constraints From Decisions: decisions that still bind the remaining work
- Remaining Work
- Now And Next
- Blockers And Human Tasks
- Verification: acceptance criteria, not results

Keep the plan under 150 lines. Make it detailed enough to resume without chat
history, but do not restate facts the repository makes cheap to inspect.

## Authorization boundary

If the user asked only for a plan, stop after writing it. If the user asked to
perform the work, the approved request authorizes in-scope implementation after
the plan is written; do not add a second approval ritual. Pause only when the
plan exposes a material choice, new external effect, destructive operation, or
scope expansion that the request did not authorize.

## Execute and maintain

- Delete finished steps instead of checking them off. Do not keep `Progress`,
  `Outcomes`, `Surprises`, or dated decision logs.
- Rewrite `Context` when evidence changes a fact; do not append what happened.
- Before stopping, plan the next agent's first step in `Now And Next`, and
  list what blocks completion or needs a person in `Blockers And Human Tasks`.
- Put unrelated follow-up work in a separate plan rather than expanding this one.
- Verify each milestone with evidence capable of detecting failure in that step.

When concurrent agents are explicitly requested, the plan remains the shared
source of truth. Give each worker a disjoint ownership slice and update the plan
only after checking its artifacts.

## Complete the lifecycle

Delete the plan in the same change as the completed work. Put the delivered
result, remaining work, and actual verification in the PR description; git
history keeps the plan's last version for reviewers.

Before deleting, promote only durable knowledge:

- a repository-wide convention belongs in agent or architecture documentation;
- a hard-to-reverse decision with real alternatives belongs in an ADR;
- a mechanically enforceable rule belongs in a test, lint, type, or structural
  check;
- a real follow-up belongs in a new active plan.

Do not create follow-up plans merely to preserve speculative ideas.
