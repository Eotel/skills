# business-logic-extraction — Claude Fable 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Fable 5 executing this skill. On
self-load, apply them as written. When delegating a batch, paste the
Instructions block into the subagent prompt. They do not override SKILL.md.

## Instructions

- Extract only branches that decide business meaning per the Core Rule; keep
  mechanical adaptation inline even when it looks improvable. Do not
  restructure neighbors, add layers, or refactor beyond the batch you were
  asked for.
- No unrequested docs, error handling, abstractions, or defensive git
  branches.
- Claim "batch verified" only with the characterization test output and the
  ladder rungs you actually ran — attach that evidence to the claim.
- Run the Verification Ladder in order: the narrow characterization proof
  first, then widen.
- Delegate behavior-preservation review to a fresh-context verifier subagent
  instead of self-review (it catches what the author defends). Keep DB-backed
  tests serial unless the repo isolates test databases per process.
- End with a plain-language report: batches done, tests and rungs run, any
  recorded contract drift. No session shorthand.

## Caller notes

- Effort: `high` for extraction; `medium` for routine mechanical batches,
  where Fable 5 otherwise over-deliberates.
