# GPT-5.6 Guide

> Snapshot: 2026-07. Sources:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>,
> <https://learn.chatgpt.com/docs/models>.
> Re-verify before relying on a version-specific claim.

Use this only when the implementation model that will run the handoff package
is GPT-5.6 (`gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna`).

## Fit

- The natural Codex runner after GPT-5.5: `gpt-5.6-sol` for the hardest
  waves, `gpt-5.6-terra` at GPT-5.5-level quality for lower cost,
  `gpt-5.6-luna` for cheap worker subsessions.
- Rewards lean packages: removing repeated instructions and simplifying tool
  descriptions measurably improves adherence and cuts tokens.

## Handoff Authoring

- Deduplicate the package: state each rule once in the orchestrator prompt
  instead of restating it per wave. GPT-5.6 keeps a rule stated once in
  force; repetition costs tokens without improving adherence.
- Still name the subsession spawning mechanism verbatim — the GPT-5.5
  failure mode (no spawning unless the primitive is spelled out) has not gone
  away.
- Write the autonomy boundary into the prompt: safe local inspection and
  non-destructive validation are pre-approved; external writes, destructive
  git, and any scope expansion require the package's gates.
- Effort: carry the GPT-5.5 baseline, then test one level lower. `medium`
  for the orchestrator loop, `high`/`xhigh` for the hardest waves, `max`
  only for quality-first work. Codex also exposes `ultra` (subagent-based
  delegation) — use it as an explicit design choice, not a default.
- Keep an explicit stopping rule per wave; persistence still gold-plates
  without a defined end.
- Order the prompt cache-friendly: stable rules and rubric first, per-wave
  dynamic context last. Set report length with `text.verbosity` plus what
  must be preserved.

## Prompt Patch

Add this block to the orchestrator prompt when the runner is GPT-5.6:

```text
Use GPT-5.6 behavior intentionally: start from the rubric and constraints,
apply each rule as stated once, spawn the named implementer/verifier
subsessions, and stop each wave when its rubric lines pass. Inspect and
validate locally on your own; the package's gates govern external writes,
destructive actions, and scope changes. Report in the exact sections
requested and record only tool-verified facts.
```
