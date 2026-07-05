# Claude Fable 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the implementation model that will run the handoff package
is Claude Fable 5.

## Fit

- The strongest runner for long, ambiguous, multi-wave packages; it sustains
  hours of autonomous work and keeps instructions across the whole run.
- Overkill for a one-wave package; if the spec collapsed to a single small
  phase, recommend a lighter runner in the delivery notes.

## Handoff Authoring

- Write the orchestrator prompt outcome-first and lean: objective, rubric,
  fences, retry caps. Fable 5 degrades under over-prescriptive process stacks
  carried over from older models — cut enumerated micro-steps.
- It can overplan on ambiguous waves. Add "act once enough evidence exists; do
  not relitigate settled decisions" next to the wave definitions.
- It may do unrequested extra work (defensive branches, unasked docs). Make
  the 🛑 propose-only fences and out-of-scope paths explicit and hard — it
  follows brief explicit constraints well.
- It delegates to subagents readily. The implementer/verifier split needs
  bounding, not encouragement: say which roles exist and that no others are
  spawned.
- Since the author is not in the loop, state autonomy explicitly: "you are
  operating autonomously; finish or report blocked — do not ask permission
  mid-wave," with the pause boundaries limited to 🛑 surfaces and retry-cap
  exhaustion.
- Require every memory-log entry and progress claim to be audited against
  actual tool output before it is recorded.

## Prompt Patch

Add this block to the orchestrator prompt when the runner is Fable 5:

```text
Use Claude Fable 5 behavior intentionally: drive toward the rubric, act once
enough evidence exists, keep implementer/verifier subsessions moving, and
record only tool-verified facts in the memory log. Never touch a propose-only
surface; stop and report instead of expanding scope.
```
