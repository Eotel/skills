---
name: codex-orchestration-core
description: Shared safety and acceptance contracts for codex-orchestrator-brief and codex-tdd-orchestration. Use only when either workflow loads it.
---

# Codex Orchestration Core

Apply these invariants when another skill explicitly loads this dependency.
The caller supplies its workflow and harness-specific tool names; this file owns
the shared safety and acceptance behavior.

## Invariants

1. **No-code orchestrator.** When the workflow separates orchestration from
   implementation, the orchestrator plans, dispatches, integrates, and verifies;
   implementation files belong to workers.

2. **Cold critic.** Acceptance is judged in a fresh context that receives the
   spec, artifact or diff, and acceptance checks—not the author's rationale or
   self-report. The author never approves its own output.

3. **Executable contract.** State each required outcome with evidence a critic
   can independently inspect or run. Replace subjective gates such as "cleaner"
   with observable behavior, commands, or diff constraints.

4. **PASS gate.** Integrate a unit only after its required checks pass and the
   critic has no blocking finding. CI or other remote gates remain additional
   requirements when the workflow includes them.

5. **Durable objective.** Long-running orchestration needs a verifiable end-state
   and a progress record that survives context loss. Use the harness's durable
   goal mechanism only while an active worker or monitor is advancing it.

6. **Verify reports.** Inspect the actual diff, files, and command output after a
   worker finishes. An empty or out-of-scope diff contradicts a success report.

7. **Bounded retries.** Give each unit a retry cap. On the first recurrence, fix
   the defect class rather than another literal example; repeated recurrence
   triggers redesign or a user decision instead of an unbounded loop.

8. **Verified memory.** Record only facts established by artifacts or commands.
   Feed relevant lessons to later workers without forwarding author reasoning to
   the cold critic.

9. **Disjoint parallelism.** Parallel workers must have explicit, non-overlapping
   ownership. Serialize overlapping work and integration. After absorbing another
   unit, rerun the checks capable of detecting integration drift.

10. **Protected surfaces.** Fence security boundaries, public contracts,
    migrations, generated files, and product decisions according to the user's
    scope. A protected or unowned path in a diff stops that integration until the
    boundary is resolved.

11. **Environment contract.** Resolve the exact worktree, branch, write access,
    available dependencies, sandbox, and network limits before dispatch. Supply
    known facts to workers; do not make each one rediscover the environment.

## Worker prompt contract

When two or more workers edit the same repository, load `worker-contract` and
give each worker:

- the global outcome and its independently checkable slice;
- an explicit file ownership allowlist and out-of-scope paths;
- the exact working directory and expected branch/base;
- relevant environment facts and permitted operations;
- the checks it owns and the evidence it must return;
- a short structured final report.

Add domain-specific prohibitions only when the current task or repository makes
them relevant. Do not paste a catalog of every failure seen in unrelated work.

## Critic contract

The critic is read-only unless it has been explicitly assigned a separate fix
phase. It checks the acceptance contract, searches for sibling cases suggested by
the diff, and reports only actionable findings with file locations and evidence.
A later fixer receives those findings verbatim; a fresh critic judges the result.

## Completion

Stop when every planned unit has evidence-backed PASS, integration checks are
green, protected surfaces are clean, and remaining work is either explicitly
out of scope or reported as blocked with evidence. Merge, close, production
writes, and other external state changes still require the user's authorization.
