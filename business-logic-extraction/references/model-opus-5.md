# business-logic-extraction — Claude Opus 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Opus 5 executing this skill. On
self-load, apply them as written. When delegating a batch, paste the
Instructions block into the subagent prompt. They do not override SKILL.md.

## Instructions

- Apply the Core Rule, the Verification Ladder order, and the Review
  Checklist to every batch. These are the contract; add no verification
  passes beyond them — run the narrow characterization proof and the
  touched-surface rungs, cite their output, and close the batch.
- Report every behavior-preservation concern, including low-severity and
  uncertain ones (auth weakening, contract drift, error-routing changes).
  Do not filter for importance — filtered reporting drops recall.
- Keep each batch at the scope written: no docs, abstractions, or error
  handling beyond the extraction itself, and no widening a batch because a
  better refactor is visible. Surface it as a follow-up instead.
- Spawn a fresh-context verifier subagent only when the batch touches
  authorization or state transitions; verify inline with the ladder
  otherwise. Do not delegate ordinary batch work or self-checks.

## Caller notes

- Effort: `high` default; `xhigh` where behavior preservation is subtle;
  `low`/`medium` hold for a single scoped verify command.
- Thinking is on by default; keep it on and lower effort instead of
  disabling it.
