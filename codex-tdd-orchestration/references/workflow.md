# Live Orchestration Workflow

Use this procedure only after the root skill's scope gate selects full
orchestration.

## Acceptance planning

Create four artifacts before dispatch:

1. A global `done when ...` outcome.
2. An acceptance checklist whose items name an owner, evidence, and expected
   result.
3. A dependency-ordered topic map with file ownership and retry caps.
4. A short contract for each worker and critic.

Keep these in the harness's durable plan/task surface. Update progress only after
inspecting evidence.

## Environment readiness

For each isolated worktree or checkout:

- resolve the exact base, branch, and working directory;
- confirm the worker's sandbox can write there;
- provision dependencies required by offline or network-restricted workers;
- record relevant baseline failures;
- reserve git commits, integration, push, and PR operations for the main agent
  when worker sandboxes cannot perform them reliably.

Do not create an isolated worktree for a bounded read-only pass that can use the
current checkout safely.

## Topic loop

### Implement

Give the owner the global outcome, slice outcome, owned files, exclusions,
environment facts, and checks. Use `worker-contract` for concurrent writers.

For changed behavior, establish a regression check that fails before the fix and
passes afterward. For a behavior-neutral refactor, use a structural or contract
check that detects the intended transformation.

### Review

Inspect the worker's actual diff before dispatching a critic. A success report
with no intended diff or with unowned paths is a failed handoff.

Give a fresh, read-only critic only the spec, diff, repository conventions, and
acceptance checks. Require actionable findings with evidence or an empty result.

### Fix and recheck

Send findings verbatim to the topic owner or a bounded fixer. Run focused checks
after the fix and use a fresh critic for the next acceptance judgment. Stop at
the retry cap; recurring defect classes require a structural change or user
decision.

## Between turns

A worker's report reaches an Orca lead as mail in its Run. Orca types a
"You have N orchestration messages" notice into an idle lead, but has typed
none for hours: on 2026-10-06 and 07, two Claude Code leads ended their turns
with workers still running and sat on seven reports and three questions until
someone woke them.

Before ending a turn while any Dispatch is unsettled:

1. Act on the mail already waiting, including mail that arrived during this
   turn: run the consuming `check`, answer questions, and validate each
   `worker_done`.
2. Arm the canonical loop's `check --wait` from `orca skills get orchestration`
   as a background command, so its exit starts your next turn. When it returns,
   process the batch and re-arm it with that batch acknowledged, as the loop
   shows, so the batch does not replay.
3. In a harness that cannot start a turn when a background command exits, keep
   the wait in the foreground instead of ending the turn.

A final message that asks the user something ends the turn as well: arm the
wait before you ask.

## Integration

Integrate one topic at a time in dependency order. Before accepting each topic:

- its required checks pass;
- its latest critic has no blocking finding;
- its diff is limited to owned and expected files;
- it has absorbed prior integrated changes when necessary;
- the checks capable of detecting cross-topic drift pass.

Run the full repository or release gate once on the integrated result when the
blast radius or repository policy requires it. Do not repeat broad suites between
small fixes without a new reason.

## Finish

Report the integrated evidence, critic decisions, retries, and blocked/out-of-scope
work. Push or open a PR when requested. Wait for remote checks when shipping is
in scope. Merge, close, production writes, and destructive cleanup require the
user's authority.
