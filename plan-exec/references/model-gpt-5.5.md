# GPT-5.5 Guide

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Use this only when the model executing this skill is GPT-5.5 (including Codex
sessions on GPT-5.x-codex — the environment this skill was written for).

## Plan Execution Prompting

- Write plan steps outcome-first: each step names its end-state, constraints,
  and verification evidence, not a micro-procedure. GPT-5.5 picks an efficient
  path when given the destination and gold-plates when given neither an end
  state nor a stop rule.
- Give the plan an explicit completion contract: which Verification commands
  passing, plus Progress and Outcomes updated, means done. Persistence is
  strong; an undefined "done" produces unrequested extras.
- The approval ritual is a collaboration override: even under bias-to-action
  defaults, stop after writing the plan file and wait — do not "implement with
  reasonable assumptions" past an unapproved plan.
- Codex's internal planning tool does not replace the plan file. The file is
  the durable, resumable artifact; mirror milestone completion into it, and
  never end a turn with only an internal plan updated.
- Default `medium` reasoning for execution; escalate to `high` only for the
  step that proves genuinely hard — 5.5's low/medium reasoning is efficient,
  so re-evaluate before escalating.
- Keep the plan template's section order stable and put volatile detail
  (Progress, Surprises) last — stable-first ordering is also cache-friendly
  for long sessions.

## Prompt Patch

Add this block to the executing prompt only when useful:

```text
Use GPT-5.5 behavior intentionally: treat the plan file as the contract —
reach each step's stated end-state, verify with its listed commands, update
the plan file at every stopping point, and stop when the completion contract
is satisfied. Wait for plan approval before implementing.
```
