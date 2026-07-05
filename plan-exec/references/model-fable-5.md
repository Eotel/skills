# Claude Fable 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the model executing this skill is Claude Fable 5.

## Fit

- Best at the long, cross-subsystem work this skill exists for; a plan file is
  the external memory that keeps a multi-hour run resumable.
- Prone to wrapping routine work in ceremony — apply the "when NOT to use"
  gate strictly before writing any plan file.

## Plan Execution Prompting

- Fable 5 can overplan ambiguous work. Cap the plan at what the evidence
  supports; write open questions into Constraints instead of expanding Plan of
  Work speculatively, and start executing once the plan is approved.
- The approval ritual is a hard pause boundary. Autonomy instructions
  elsewhere do not override it: write the plan, surface it, stop.
- It may take useful-looking unrequested actions. Anything outside the
  approved Plan of Work goes into the plan as a new section or follow-up plan
  first — never silently folded in.
- Audit Progress entries against actual tool output before writing them; a
  status line from a subagent is not evidence (the skill's reviewer-pass rule
  for extraction work applies to all delegated steps).
- Long sessions drift toward dense shorthand. Keep the plan file readable for
  a cold resume: full sentences in Decision Log and Surprises, not fragments.
- Delegating batches to subagents is encouraged, but the plan file stays the
  single source of truth — update it yourself after verifying their work.

## Prompt Patch

Add this block to the executing prompt only when useful:

```text
Use Claude Fable 5 behavior intentionally: plan only to the depth the evidence
supports, stop for approval after writing the plan file, execute autonomously
after approval, and record only tool-verified progress. Scope changes become
new plan sections, never silent expansions.
```
