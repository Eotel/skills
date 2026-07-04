# Claude Opus 4.8 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/ja/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Opus 4.8.

## Fit

- Good for long-running orchestration, knowledge work, vision, memory-heavy
  tasks, code review, and debugging.
- Existing Opus 4.7-style prompts usually transfer, but tune effort and
  explicitness before adding more process.

## Orchestration Prompting

- Prefer explicit scope and concrete acceptance checks. Opus 4.8 can interpret
  instructions literally, especially at lower effort; do not rely on implied
  generalization across topics.
- Use `high` or `xhigh` effort for difficult coding and agentic workflows.
  Consider `medium` only when the task is well bounded and latency/cost matters.
- Opus 4.8 can reason before using tools. If tool evidence is required, say which
  evidence must be gathered before reporting progress.
- Subagent spawning may be conservative. State when to spawn multiple subagents:
  independent topics, disjoint file reads, or cold reviewer/fixer separation.
- Keep user updates concise and evidence-backed; avoid forcing mechanical
  updates every N tool calls.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Use Claude Opus 4.8 behavior intentionally: follow the explicit scope literally,
gather tool evidence before claiming progress, and spawn subagents only for
independent work or required cold-review separation. Use high effort for this
multi-step coding task unless the orchestrator specifies otherwise.
```
