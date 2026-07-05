# apm-usage — Claude Sonnet 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Sonnet 5 executing APM tasks with
this reference. On self-load, apply them as written. When delegating, paste
the Instructions block into the subagent prompt. They do not override
SKILL.md's command facts.

## Instructions

- Stop when the named check confirms the outcome — `apm deps list` shows the
  package at the expected ref, or `apm targets` prints the intended target.
  Do not keep re-verifying past that.
- Take flags verbatim from SKILL.md; do not improvise from memory.
- Check the Gotchas section before diagnosing breakage; renames and
  no-refresh-by-design behavior are documented there.
- Confirm before `--force` or a global update that can rewrite `.gitignore`;
  prefer a neutral cwd for global updates.

## Caller notes

- Effort: `high` default is fine; this task class rarely needs more.
- Non-default temperature/top_p/top_k return HTTP 400; steer output style via
  prompt text.
