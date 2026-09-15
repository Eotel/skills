---
name: codex-tdd-orchestration
description: Orchestrate multi-agent Codex implementation with isolated workers, TDD, cold review, and integration gates. Use only when the user explicitly requests Codex orchestration or parallel agents.
---

# Codex TDD Orchestration

Load `codex-orchestration-core` first. This skill adds the live execution loop
for a main agent coordinating Codex workers; it does not apply to ordinary work
merely because Codex could perform it.

## Scope gate

Use full orchestration when the request explicitly asks for multiple agents or
an orchestrator and the work has at least two meaningful implementation units
that benefit from isolated ownership and cold review.

Use a single worker or inline TDD for one coherent issue, a narrow review, a
mechanical change, or work whose files cannot be separated. Do not manufacture
extra topics to justify orchestration.

Before dispatching, confirm:

- the global acceptance outcome and the evidence that proves it;
- topic boundaries, dependencies, ownership, and retry caps;
- the target worktree/branch and integration order;
- the harness's actual agent, message, wait, and monitoring capabilities;
- which operations remain with the main agent, including git and external state.

## Live workflow

1. **Baseline.** Establish the integration base and record relevant pre-existing
   failures. Create isolated worktrees only when isolation is needed.
2. **Implement.** Assign one owner per topic. For behavioral changes, require a
   regression check that fails before the fix and passes after it; use a
   structural assertion for behavior-neutral refactors.
3. **Review.** Give a fresh read-only critic the spec, diff, and checks. Do not
   include the author's report or hidden reasoning.
4. **Fix.** Return actionable findings to the same topic owner or a bounded
   fixer, then have a fresh critic judge the updated result.
5. **Integrate.** Serialize commits and integration. After each topic lands,
   rerun the checks that could detect cross-topic drift.
6. **Finish.** Run the final acceptance contract, remote gates included when in
   scope, and report unresolved blockers. Do not merge or close without explicit
   user authorization.

Read [references/workflow.md](references/workflow.md) when launching full
orchestration. It contains the detailed milestone and integration procedure.

Read [references/prompts.md](references/prompts.md) only when composing worker,
critic, or fixer prompts. Use `worker-contract` for concurrent file ownership.

Read [references/pitfalls.md](references/pitfalls.md) only when the selected
harness uses isolated worktrees or has known sandbox/network constraints.

## Progress and persistence

Use the harness's native durable goal and task tracking when available. Describe
the end-state, not the itinerary, and keep progress synchronized with verified
milestones. A durable goal must have an active worker or monitor advancing it;
do not create an idle stop gate.

Let workers run until a milestone completes or produces evidence of a blocker.
Intervene on completion, ownership drift, environment mismatch, retry exhaustion,
or explicit user input—not merely because a worker has been quiet.

## Completion

The workflow is complete when every topic has a cold PASS, the integrated state
satisfies the global checks, the diff contains no protected or unowned paths,
and all unresolved work is explicitly classified. Worker status lines alone are
not completion evidence.
