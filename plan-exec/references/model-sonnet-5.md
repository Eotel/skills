# Claude Sonnet 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the model executing this skill is Claude Sonnet 5.

## Fit

- Good default executor for plan-driven coding work.
- Tool-eager and self-verifying; the plan must say what evidence closes a
  step, or execution keeps re-checking.

## Plan Execution Prompting

- Write each Plan of Work step with its exit evidence (command + expected
  result) in the Verification section. Sonnet 5 stops cleanly when told what
  suffices and over-verifies when not.
- Instruction following is literal: scope every plan rule explicitly (whole
  plan vs one step), same as for Opus 4.8.
- Default `high` effort; reserve `xhigh` for the hardest step, not the whole
  plan. Latency-sensitive steps get narrower objectives, not more prose.
- Its eagerness can widen a step's blast radius mid-execution. Restate file
  ownership per step when steps touch adjacent subsystems, and route anything
  outside it through a plan edit first.
- Do not rely on sampling parameters for plan prose style; Sonnet 5 rejects
  non-default temperature — put tone and format expectations in the plan
  template itself.
- Progress updates are strong natively; keep the plan's Progress section as
  the durable record and avoid duplicate mechanical status chatter.

## Prompt Patch

Add this block to the executing prompt only when useful:

```text
Use Claude Sonnet 5 behavior intentionally: execute each plan step within its
stated file scope, verify with the step's listed commands, mark it done in the
plan file when the listed evidence passes, and move on without re-verifying
earlier steps.
```
