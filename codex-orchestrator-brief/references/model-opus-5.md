# Claude Opus 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the implementation model that will run the handoff package
is Claude Opus 5.

## Fit

- Strongest runner for long-horizon multi-wave work: completes full waves
  without stubs when the package gives the complete spec up front.
- Opus 4.8 packages run unchanged, but their scaffolding needs pruning:
  explicit verification steps and anti-laziness scaffolding now overtrigger.

## Handoff Authoring

- Do not write "verify your work" or "double-check" steps into waves — Opus 5
  self-verifies natively and such instructions cause over-verification. Keep
  only the structural gates: the executable rubric and cold-verifier rounds.
- Unlike Opus 4.8, it spawns subagents readily. The package must cap
  spawning: name the allowed spawn points (implementer per wave, fresh cold
  verifier per round) and forbid ad-hoc delegation and subagent self-checks.
- Fence scope hard: it applies its own judgment about what the task should
  be. Put out-of-scope files, unrequested refactors, and destructive actions
  behind the 🛑 propose-only fences.
- Effort notes for the prompt: default is `high` with thinking on; recommend
  `low`/`medium` for bounded verification rounds (accuracy holds there) and
  `xhigh` for the hardest implementation waves. Do not disable thinking —
  lower the effort instead.
- For verifier roles keep "report every failed or uncertain line, do not
  filter for severity" — severity-filtering instructions are followed
  literally and drop recall.
- State the report and memory-log shape explicitly; written deliverables run
  long by default.

## Prompt Patch

Add this block to the orchestrator prompt when the runner is Opus 5:

```text
Use Claude Opus 5 behavior intentionally: complete each wave end-to-end within
its stated scope with thinking on, rely on native self-verification plus the
rubric gates instead of extra check steps, spawn only the named implementer
and cold-verifier subsessions, report every rubric finding without severity
filtering, and keep reports to the requested shape.
```
