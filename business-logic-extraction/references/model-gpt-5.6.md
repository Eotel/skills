# business-logic-extraction — GPT-5.6

> Snapshot: 2026-07. Sources:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>,
> <https://learn.chatgpt.com/docs/models>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for GPT-5.6 (`sol` / `terra` / `luna`,
including the Codex harness) executing this skill. On self-load, apply them
as written. When delegating a batch, paste the Instructions block into the
subagent prompt. They do not override SKILL.md.

## Instructions

- Target end-state per batch: the entrypoint reads like an adapter, the
  named decision lives in its owning layer, behavior unchanged. Choose your
  own route to that state.
- After each result, check whether the batch can close: it is done when the
  focused characterization test and the touched-surface ladder rungs pass.
  Stop then — no gold-plating.
- Keep the Verification Ladder order — run the narrow characterization
  proof first. Reads and test runs proceed freely; destructive git or any
  widening beyond the batch needs approval.
- Implement with reasonable assumptions; do not end the turn on clarifying
  questions unless truly blocked.
- No broad try/catch with silent defaults. Report in the requested
  sections, in order, after a final correctness/grounding pass.

## Caller notes

- Reasoning: carry the GPT-5.5 baseline (`medium`) and test `low` for
  routine batches; `high` only for a genuinely hard one.
- Model: `gpt-5.6-sol` where behavior preservation is subtle;
  `gpt-5.6-terra` for routine batches.
