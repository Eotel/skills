# Claude Opus 4.8 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Use this only when the model executing this skill is Claude Opus 4.8.

## Fit

- Strong at long-horizon plan execution and at the review/debug steps a plan's
  Verification section demands.
- Literal instruction following: the plan text is what gets executed, so write
  plan steps with explicit scope.

## Plan Execution Prompting

- Opus 4.8 does not silently generalize. A rule meant for the whole plan
  ("update Progress at every stopping point", "run lint after each step") must
  say so — stated once inside step 1, it stays in step 1.
- Use high effort for cross-subsystem execution; at low/medium it scopes to
  exactly the written step and may under-think steps that turned out harder
  than planned. When a step surprises, raise effort rather than adding text.
- It favors reasoning over tool calls. In the Verification section, name the
  concrete commands per step so evidence is gathered, not inferred.
- Its native progress reporting is good — do not add forced-update
  scaffolding; the plan's Progress section plus normal updates suffice.
- It tends to overbuild. Keep Constraints and Non-Goals explicit ("no
  unrequested docs, abstractions, or error handling") so the diff matches the
  plan.
- When the plan delegates to subagents, say explicitly when to spawn them;
  Opus 4.8 defaults to doing the work in-session.

## Prompt Patch

Add this block to the executing prompt only when useful:

```text
Use Claude Opus 4.8 behavior intentionally: execute the plan steps literally
and completely, run the named verification commands before marking a step
done, keep the diff within the plan's stated scope, and update the plan file
at every stopping point.
```
