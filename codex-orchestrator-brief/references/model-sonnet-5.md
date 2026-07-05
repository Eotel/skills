# Claude Sonnet 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the implementation model that will run the handoff package
is Claude Sonnet 5.

## Fit

- Good default runner for coding-heavy packages with clear waves and an
  executable rubric.
- More tool-eager and self-verifying than earlier Sonnets; the package must
  say when verification is *enough*, or waves run long.

## Handoff Authoring

- For every rubric line, define the exit evidence precisely (command + expected
  output). Sonnet 5 keeps verifying until told what suffices; an open-ended
  "make sure it works" line burns the retry budget on re-checking.
- Instruction following is literal: scope each constraint ("all waves" vs
  "wave 3 only") in the spec and the prompt, and keep the two consistent.
- Recommend `high` effort as the default and `xhigh` only for the hardest
  implementation waves; narrow verifier checks can run lower.
- Do not tune tone or variety via sampling parameters in the runner config —
  Sonnet 5 rejects non-default temperature/top_p; put tone and output shape in
  the prompt text.
- Adaptive thinking is on by default; if a wave is latency-sensitive, narrow
  its objective and rubric lines rather than adding process text.
- Its eagerness can expand a wave's blast radius. Restate the wave's file
  ownership next to each implementer contract, not only in the spec.

## Prompt Patch

Add this block to the orchestrator prompt when the runner is Sonnet 5:

```text
Use Claude Sonnet 5 behavior intentionally: follow each wave's scope exactly,
verify with the listed commands, and stop the wave when the listed evidence
passes. Do not expand a wave beyond its file ownership after verification
succeeds.
```
