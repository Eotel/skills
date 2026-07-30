# plan-exec — GPT-5.6

> Snapshot: 2026-07. Sources:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>,
> <https://learn.chatgpt.com/docs/models>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for GPT-5.6 (`sol` / `terra` / `luna`,
including Codex sessions) executing this skill. On self-load, apply them as
written. When delegating plan execution, paste the Instructions block into
the subagent prompt. They do not override SKILL.md.

## Instructions

- The plan file is the contract: reach each step's stated end-state, verify
  with its listed commands, and mirror every stopping point into the file.
- Stop after writing the plan file and wait for approval. Do not implement
  past an unapproved plan on reasonable assumptions.
- The approved plan is your authorization boundary: in-plan reads, edits,
  and non-destructive validation proceed without re-asking; destructive
  actions or work outside the Plan of Work require a new approval first.
- Done means the completion contract holds: the Verification commands pass,
  Progress and Outcomes are updated, and the finished plan is moved to
  `completed/` inside the PR that ships the final step. Do not add work
  beyond it.

## Caller notes

- Reasoning: carry the GPT-5.5 baseline (`medium`), then test one level
  lower for bounded steps; `high` only for a step that proves genuinely
  hard.
- Model: `gpt-5.6-terra` for routine plan execution; `gpt-5.6-sol` for hard
  cross-subsystem plans.
- Keep stable prompt content first and dynamic repo context last for
  caching.
