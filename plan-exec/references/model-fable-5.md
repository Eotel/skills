# plan-exec — Claude Fable 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Fable 5 executing this skill. On
self-load, apply them as written. When delegating plan execution, paste the
Instructions block into the subagent prompt. They do not override SKILL.md.

## Instructions

- Apply the "when NOT to use" gate strictly; do not wrap routine work in a
  plan file.
- Plan only to the depth the evidence supports. Put open questions into
  Constraints instead of expanding Plan of Work speculatively, and start
  executing as soon as the plan is approved — do not relitigate settled
  sections.
- Writing the plan file ends the turn: surface it and stop for approval.
  Autonomy instructions elsewhere do not override this pause.
- After approval, execute without asking permission mid-plan. Anything outside
  the approved Plan of Work becomes a new plan section or a follow-up plan
  first — never a silent expansion, never an unrequested extra (defensive
  branches, unasked docs).
- Record Progress only after checking the actual tool output; a subagent's
  status line is not evidence.
- Write Decision Log and Surprises in full sentences a cold resume can follow,
  not session shorthand.
- When delegating batches, the plan file stays the single source of truth:
  verify subagent output yourself, then update the file.

## Caller notes

- Effort: `high` default; `medium` is enough for small plan-worthy changes.
- For trivial fixes, skip this skill entirely rather than downshifting the
  model — the gate exists for that.
