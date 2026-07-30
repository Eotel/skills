# plan-exec — Claude Opus 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Opus 5 executing this skill. On
self-load, apply them as written. When delegating plan execution, paste the
Instructions block into the subagent prompt. They do not override SKILL.md.

## Instructions

- Writing the plan file ends the turn: surface it and stop for approval.
  Bias-to-action does not override this pause.
- Deliver each step at the scope written. Do not widen, narrow, or transform
  a step by your own judgment — a better approach becomes a Surprises note
  and a plan-file update, never a silent change.
- Run each step's listed verification commands and read their output. Do not
  add verification passes or verifier subagents beyond what the plan names —
  the plan's commands are the whole contract.
- Spawn subagents only where the plan says to delegate; do not delegate
  small steps or checks of your own work.
- Keep the plan file lean: Progress and Surprises record what a cold resume
  needs, without padded sections or restated context.

## Caller notes

- Effort: `high` default; sweep down — `medium` holds for bounded plan-sized
  changes, `xhigh` for the hardest cross-subsystem steps.
- Thinking is on by default; keep it on and lower effort instead of
  disabling it.
