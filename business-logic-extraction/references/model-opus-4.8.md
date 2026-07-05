# business-logic-extraction — Claude Opus 4.8

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Opus 4.8 executing this skill. On
self-load, apply them as written. When delegating a batch, paste the
Instructions block into the subagent prompt. They do not override SKILL.md.

## Instructions

- Apply the Core Rule, the Verification Ladder order, and the Review Checklist
  to every batch, not only the first one.
- Before claiming a batch done, run and cite the focused characterization test
  and the touched-surface ladder rungs. Do not reason your way past the narrow
  test to a wide check — run the narrow proof.
- Report every behavior-preservation concern, including low-severity and
  uncertain ones (auth weakening, contract drift, error-routing changes); do
  not filter for importance.
- Keep each batch minimal: no docs, abstractions, or error handling beyond the
  extraction itself.
- Spawn a fresh-context verifier subagent when the batch touches authorization
  or state transitions; verify inline with the ladder otherwise.

## Caller notes

- Effort: `xhigh` for extraction and characterization where behavior
  preservation is subtle; `high` minimum; `low` only for a single scoped
  verify command.
- Thinking is off by default; enable adaptive thinking for extraction work.
