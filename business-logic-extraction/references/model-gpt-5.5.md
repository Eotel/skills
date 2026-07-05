# business-logic-extraction — GPT-5.5

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for GPT-5.5 (including the Codex harness)
executing this skill. On self-load, apply them as written. When delegating a
batch, paste the Instructions block into the subagent prompt. They do not
override SKILL.md.

## Instructions

- Target end-state per batch: the entrypoint reads like an adapter, the named
  decision lives in its owning layer, behavior unchanged. Choose your own
  route to that state.
- After each result, check whether the batch can close: it is done when the
  focused characterization test and the touched-surface ladder rungs pass.
  Stop then — no gold-plating.
- Implement with reasonable assumptions; do not end the turn on clarifying
  questions unless truly blocked.
- Keep the Verification Ladder order — run the narrow characterization proof
  first. Use the planning tool only for non-trivial batches.
- No broad try/catch with silent defaults; no destructive git without
  approval. Report in the requested sections, in order, after a final
  correctness/grounding pass.

## Caller notes

- Reasoning: `medium` default; `high` only for a genuinely hard batch —
  re-evaluate before escalating.
