# Claude Fable 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Fable 5.

## Fit

- Best on the hardest, ambiguous, long-running extractions spanning many handlers
  and several batches end-to-end.
- Reliable on simple batches but can over-deliberate there; steer routine work with
  brief instructions and lower effort.

## Extraction Prompting

- It may take unrequested actions — unasked refactors, extra abstractions,
  defensive git branches. Fence scope hard: extract only branches that decide
  business meaning per the Core Rule, keep mechanical adapter code inline, and do
  not expand the batch or restructure neighbors you were not asked to touch.
- It tends to overbuild: prompt for minimal scope — no unrequested docs, error
  handling, or new layers beyond the owning layer the placement guide names.
- Audit progress claims against tool results. Require that "batch verified" is
  backed by the actual characterization test output and the ladder rungs run — no
  claiming behavior preservation without the run.
- It follows brief instructions literally but degrades under enumerated legacy
  rules; give intent (entrypoints should read like adapters) not long step lists,
  and keep the Verification Ladder order as the sequence.
- It dispatches parallel subagents readily and a fresh-context verifier beats
  self-critique; delegate behavior-preservation review to one, but keep DB-backed
  tests off parallel runs unless the repo isolates test DBs per process.
- Long sessions produce dense shorthand. Instruct a clear final report: batches
  done, tests and ladder rungs run, and any recorded contract drift.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Operate autonomously; pause only for destructive/irreversible steps or a scope
change. Extract only Core-Rule business branches — no unrequested refactors,
abstractions, or docs. Back every "verified" claim with the test and ladder
output you actually ran, and end with a clear plain-language report.
```
