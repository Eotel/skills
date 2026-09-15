---
name: plan-exec
description: Create and execute a repo-local implementation plan for cross-system, high-risk, or multi-turn work. Skip routine single-file changes.
---

# Plan And Execute

Use a file-backed plan when the work crosses subsystems, changes a public or
security-sensitive contract, spans multiple turns, or needs a durable decision
record. A plan is unnecessary for routine, bounded edits whose diff explains the
work.

## Start the plan

Follow `docs/PLANS.md` and `docs/exec-plans/plan-template.md` when present.
Otherwise create:

```text
docs/exec-plans/active/{yyyy-mm-dd}-{2-6-word-kebab-slug}.md
```

Use the repository timezone and include:

- Goal
- Constraints and Non-Goals
- Plan of Work
- Progress
- Verification
- Decision Log
- Surprises and Discoveries
- Outcomes and Retrospective

Make the plan detailed enough to resume without chat history, but do not restate
facts the repository makes cheap to inspect.

## Authorization boundary

If the user asked only for a plan, stop after writing it. If the user asked to
perform the work, the approved request authorizes in-scope implementation after
the plan is written; do not add a second approval ritual. Pause only when the
plan exposes a material choice, new external effect, destructive operation, or
scope expansion that the request did not authorize.

## Execute and maintain

- Keep `Progress` current at meaningful stopping points.
- Update the plan when evidence changes the approach; do not preserve a fictional
  itinerary.
- Record decisions and surprises that would matter after context compaction.
- Put unrelated follow-up work in a separate plan rather than expanding this one.
- Verify each milestone with evidence capable of detecting failure in that step.

When concurrent agents are explicitly requested, the plan remains the shared
source of truth. Give each worker a disjoint ownership slice and update progress
only after checking its artifacts.

## Complete the lifecycle

Before finishing, update `Outcomes and Retrospective` with the delivered result,
remaining work, and actual verification. Move the plan to
`docs/exec-plans/completed/` in the same change as the completed work.

Promote only durable discoveries:

- a repository-wide convention belongs in agent or architecture documentation;
- a mechanically enforceable rule belongs in a test, lint, type, or structural
  check;
- a real follow-up belongs in a new active plan.

Do not create follow-up plans merely to preserve speculative ideas.
