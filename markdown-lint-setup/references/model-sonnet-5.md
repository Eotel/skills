# markdown-lint-setup — Claude Sonnet 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Sonnet 5 executing this skill. On
self-load, apply them as written. When delegating, paste the Instructions
block into the subagent prompt. They do not override SKILL.md's configs or
steps.

## Instructions

- Done means: both smoke-test counts are `0` and pre-existing warning counts
  are summarized. Stop there — existing prose warnings are the user's content;
  do not iterate them toward zero.
- A non-zero smoke-test count is a config bug (wrong glob form or file not at
  repo root) — fix it, then re-run.
- Copy the configs verbatim with every `dir/**` glob intact; keep both ignore
  files in sync as one list.
- Install both tools by default; warn before installing (lockfile churn); do
  not run `remark --output` on prose unless asked.

## Caller notes

- Effort: `high` default is fine; lower is acceptable for a clean
  single-package repo.
