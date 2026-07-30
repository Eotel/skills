# Claude Opus 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Opus 5.

## Fit

- Strongest on difficult, long-horizon coding: multi-file features, larger
  refactors, end-to-end work. Completes full tasks without stubs when given
  the complete spec up front and left to run.
- Opus 4.8 prompts work out of the box, but prune them: explicit verification
  and double-check instructions now overtrigger.

## Orchestration Prompting

- Thinking is on by default; keep it on and control cost with lower effort
  instead of disabling it. Default effort is `high`; `low`/`medium` hold
  quality on bounded slices, `xhigh` is for the hardest coding slices.
- It verifies and corrects its own work natively. Do not add "verify at the
  end" or "double-check" steps beyond this skill's contractual gates (TDD
  ordering, reviewer/fixer separation, verification gates).
- It spawns subagents more readily than prior Opus models — the opposite of
  Opus 4.8's conservatism. Name the scenarios that warrant a spawn, cap
  counts, and forbid subagents for self-verification.
- It can widen a task by its own judgment. Fence out-of-scope files,
  unrequested refactors, and git operations explicitly.
- For reviewer roles, ask for every finding and filter in a later pass;
  "only report high-severity issues" is followed literally and drops recall.
- Narration and written reports run long by default. State the update cadence
  and the report shape you want.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Use Claude Opus 5 behavior intentionally: complete the assigned slice
end-to-end within its stated scope, rely on native self-verification plus the
named gates instead of extra check steps, spawn subagents only for the named
scenarios, report every reviewer finding without severity filtering, and keep
progress updates brief.
```
