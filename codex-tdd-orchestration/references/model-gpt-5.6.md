# GPT-5.6 Guide

> Snapshot: 2026-07. Sources:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>,
> <https://learn.chatgpt.com/docs/models>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is GPT-5.6 (`gpt-5.6-sol` /
`gpt-5.6-terra` / `gpt-5.6-luna`).

## Fit

- `gpt-5.6-sol` is the flagship for complex coding, computer use, and
  research; `gpt-5.6-terra` performs like GPT-5.5 at lower cost for everyday
  slices; `gpt-5.6-luna` fits cheap fast workers.
- Same outcome-first preference as GPT-5.5, but leaner: repeated instructions
  and verbose tool descriptions measurably cost both score and tokens.

## Orchestration Prompting

- Put the outcome first (objective, success criteria, constraints, evidence
  rules, output shape) and state each instruction once — do not restate rules
  per role or per slice.
- Effort spans `none`–`max` on the API; Codex adds `ultra` (subagent-based
  delegation). Carry a GPT-5.5 prompt's effort baseline, then test one level
  lower; start new work at `medium`, use `high`/`xhigh` for the hardest
  slices, `max` only for quality-first work.
- Define the autonomy boundary per role: safe local inspection and
  non-destructive validation run without asking; external writes, destructive
  actions, and scope expansion go back to the orchestrator.
- Control output length with `text.verbosity` plus one line naming what must
  be preserved; replace vague tone words with concrete behavior.
- Put stable prompt content first and dynamic issue/repo context last to
  preserve cacheability.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Use GPT-5.6 behavior intentionally: start from the outcome and success
criteria, apply each instruction as stated once, and stop when the acceptance
evidence is satisfied. Inspect and validate locally on your own; return to the
orchestrator before external writes, destructive actions, or any scope
expansion.
```
