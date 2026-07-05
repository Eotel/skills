# Claude Opus 4.8 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Use this only when the implementation model that will run the handoff package
is Claude Opus 4.8.

## Fit

- Strong runner for long-horizon agentic loops, review, and debugging; prompts
  written for Opus 4.7 transfer with minor tuning.
- Interprets instructions literally, so the package must spell out scope
  instead of relying on generalization.

## Handoff Authoring

- State instruction scope explicitly everywhere: "this rule applies to every
  wave," "the rubric gates every merge." Opus 4.8 does not silently generalize
  a rule stated once for wave 1.
- Recommend `xhigh` effort for implementation waves in the prompt's model
  notes; at low/medium it scopes to exactly what is written and may
  under-think moderately complex waves.
- It favors reasoning over tool calls. For each rubric line, name the tool
  evidence that must be gathered before a PASS/FAIL is recorded.
- Subagent spawning is conservative by default. The prompt must say exactly
  when to spawn (each implementer wave; a fresh cold verifier for every
  verification round, including retries) or the runner may try to do the work
  in one session.
- Do not import forced-update or anti-laziness scaffolding; its native
  progress reporting is sufficient and the old scaffolding overtriggers.
- For verifier roles, instruct "report every failed or uncertain line, do not
  filter for severity" — a bare "report problems" is followed too literally
  and drops recall.

## Prompt Patch

Add this block to the orchestrator prompt when the runner is Opus 4.8:

```text
Use Claude Opus 4.8 behavior intentionally: apply every rule to every wave,
gather the named tool evidence before recording rubric results, spawn an
implementer subsession per wave and a fresh cold verifier for every
verification round including retries, and run implementation waves at xhigh
effort.
```
