# apm-usage — Claude Opus 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Opus 5 executing APM tasks with
this reference. On self-load, apply them as written. When delegating, paste
the Instructions block into the subagent prompt. They do not override
SKILL.md's command facts.

## Instructions

- Execute the named steps and the documented follow-ons whose trigger
  occurred: lockfile changed → commit it; lockfile is chezmoi-managed → run
  the sync-back step. Nothing beyond that — do not widen into unrequested
  manifest or target cleanup.
- Read the exact flag from SKILL.md before running a command; a recalled
  flag is not evidence at any effort level.
- Verify with `apm deps list` / `apm targets` and state which output is the
  completion evidence. Do not add verification passes or subagents beyond
  these commands.
- Read the Gotchas section before concluding APM is broken.
- Confirm before `--force` or a global update that can touch `.gitignore`;
  run global updates from a neutral cwd.
- Keep the report short: the completion evidence and any follow-on taken.

## Caller notes

- Effort: `low`/`medium` — quality holds there for reference-driven command
  work; `high` only when debugging unfamiliar APM behavior. Keep thinking
  on.
