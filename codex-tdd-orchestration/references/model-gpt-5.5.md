# GPT-5.5 Guide

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is GPT-5.5.

## Fit

- Strong for complex coding, long-running tool-heavy workflows, codebase
  navigation, verification, and multi-step execution.
- Benefits from shorter, outcome-first prompts. Avoid carrying over legacy
  process-heavy prompt stacks unless the path itself is part of the contract.

## Orchestration Prompting

- Put the outcome first: acceptance objective, success criteria, constraints,
  allowed side effects, evidence rules, and final output shape.
- Leave path choice flexible when possible. Specify exact steps only for TDD
  ordering, reviewer/fixer separation, git/PR ownership, and verification gates.
- Start with `medium` reasoning for balanced orchestration, `low` for narrow
  checks, `high` for difficult coding slices, and `xhigh` for hardest async
  agentic tasks or evals.
- Use concise final answers intentionally. If a role needs warmth, rationale, or
  detailed progress reporting, state that explicitly.
- For Responses-style tool workflows, preserve phase handling, preambles, and
  assistant-item replay in the host integration; do not hide those requirements
  in long prompt prose.
- Put stable prompt content first and dynamic issue/repo context last to preserve
  cacheability.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Use GPT-5.5 behavior intentionally: start from the desired outcome and success
criteria, choose an efficient path, use tools precisely, and stop when the
acceptance evidence is satisfied. Do not add process beyond the explicit TDD,
review, and verification contracts.
```
