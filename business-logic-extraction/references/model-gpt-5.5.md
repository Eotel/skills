# GPT-5.5 Guide

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is GPT-5.5 (including the Codex harness).

## Fit

- Prefers shorter, outcome-first prompts; give the destination and constraints for
  a batch and it picks an efficient path itself.
- Codex harness auto-injects AGENTS.md root-to-leaf and is trained on apply_patch,
  which fits the "orient on architecture first" step of this skill.

## Extraction Prompting

- Phrase each batch as a target end-state, not a step list: "this entrypoint reads
  like an adapter; the named decision lives in its owning layer; behavior
  unchanged." Let it route to the branches; do not enumerate legacy process steps.
- Set explicit stopping rules. After each result it should ask "can I close this
  batch now?" — done when the focused characterization test and the touched-surface
  ladder rungs pass. Bias to action: implement with reasonable assumptions, do not
  end the turn on clarifications unless truly blocked.
- Keep the Verification Ladder order and run the narrow characterization proof
  first; the planning tool is for non-trivial batches only, never single-step ones.
- State the output contract: return the requested sections in order, and run a
  verification loop (correctness, grounding, formatting) before finalizing.
- Hold scope with the Core Rule: extract only business-meaning branches, keep
  mechanical adaptation inline, no broad try/catch with silent defaults, and no
  destructive git without approval.
- Prefer dedicated tools over raw shell and parallelize independent reads during
  inventory; give progress updates every few steps on long batches.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Target end-state per batch: entrypoint reads like an adapter, the named decision
moved to its owning layer, behavior unchanged. Stop when the focused test and the
touched-surface ladder rungs pass. Extract only Core-Rule business branches; keep
mechanical adaptation inline. No silent-default catches, no destructive git
without approval.
```
