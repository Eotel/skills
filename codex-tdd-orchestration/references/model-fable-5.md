# Claude Fable 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/ja/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Fable 5.

## Fit

- Best for very hard, ambiguous, long-running, end-to-end work where autonomy,
  delegation, and sustained context matter.
- Can be excessive for small or routine work; prefer the Scope Fit Gate and
  lighter route when the task is single-topic or PR-sized.

## Orchestration Prompting

- Fable 5 can run for a long time. Give it a finite acceptance objective,
  milestone roadmap, and retry caps before launch.
- It can over-plan on ambiguous tasks. Instruct it to act once enough evidence is
  available and avoid relitigating settled decisions.
- It follows concise high-level instructions well. Prefer outcome, constraints,
  boundaries, and stop rules over long process stacks.
- It may proactively do extra work. Put hard fences around out-of-scope files,
  non-requested refactors, git operations, and destructive actions.
- It handles parallel subagents well. Delegate independent subtasks and keep the
  orchestrator working while they run; intervene only on evidence of drift.
- Require progress reports to be audited against tool evidence.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Use Claude Fable 5 behavior intentionally: drive toward the acceptance objective,
act once enough evidence exists, keep subagents moving asynchronously, and report
only progress grounded in tool results. Do not expand scope beyond the roadmap.
```
