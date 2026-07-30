# plan-exec — GPT-5.5

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for GPT-5.5 (including Codex sessions on
GPT-5.x-codex) executing this skill. On self-load, apply them as written. When
delegating plan execution, paste the Instructions block into the subagent
prompt. They do not override SKILL.md.

## Instructions

- The plan file is the contract: reach each step's stated end-state, verify
  with its listed commands, and update the file at every stopping point. Never
  end a turn with only your internal planning tool updated — mirror it into
  the file.
- Stop after writing the plan file and wait for approval. Do not implement
  past an unapproved plan on reasonable assumptions; the approval ritual
  overrides bias-to-action defaults.
- Done means the completion contract holds: the Verification commands pass,
  Progress and Outcomes are updated, and the finished plan is moved to
  `completed/` inside the PR that ships the final step. Do not add work
  beyond it.
- Choose your own path within a step, but keep the step order and the plan
  template's section order; append volatile detail (Progress, Surprises)
  rather than restructuring.

## Caller notes

- Reasoning: `medium` default; escalate to `high` only for a step that proves
  genuinely hard.
- Keep stable prompt content first and dynamic repo context last for caching.
