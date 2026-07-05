# GPT-5.5 Guide

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Use this only when the implementation model that will run the handoff package
is GPT-5.5 (including Codex runners on GPT-5.x-codex).

## Fit

- Strong runner for long tool-heavy loops; prefers short outcome-first prompts
  that define the destination, constraints, and evidence — not step lists.
- The natural target when the package will be launched via Codex CLI; combine
  with the `codex-prompting` skill for harness phrasing.

## Handoff Authoring

- Lead the orchestrator prompt with the outcome contract: objective, rubric,
  allowed side effects, output shape. Keep exact steps only where the path is
  the contract (wave order, author/critic split, merge gates).
- Give every wave an explicit stopping rule ("after each result, check: does
  the rubric line pass? then stop") — GPT-5.5's persistence needs a defined
  end, or it gold-plates.
- Codex will NOT spawn subsessions unless the prompt names the mechanism.
  Spell out the implementer/verifier spawning primitive verbatim; this is the
  single most common failure of handoffs to Codex.
- Recommend `medium` reasoning for the orchestrator loop and `high`/`xhigh`
  only for the hardest implementation waves; re-evaluate before escalating —
  5.5's low/medium reasoning is more efficient than prior generations.
- Order the prompt cache-friendly: stable rules and rubric first, per-wave
  dynamic context last.
- State the reporting contract explicitly (sections, order, cadence);
  otherwise final reports come back terse and may skip the memory-log update.

## Prompt Patch

Add this block to the orchestrator prompt when the runner is GPT-5.5:

```text
Use GPT-5.5 behavior intentionally: start from the rubric and constraints,
choose an efficient path, spawn the named implementer/verifier subsessions,
and stop each wave when its rubric lines pass. Report in the exact sections
requested and record only tool-verified facts.
```
