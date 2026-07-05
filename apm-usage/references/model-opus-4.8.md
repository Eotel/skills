# apm-usage — Claude Opus 4.8

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Opus 4.8 executing APM tasks with
this reference. On self-load, apply them as written. When delegating, paste
the Instructions block into the subagent prompt. They do not override
SKILL.md's command facts.

## Instructions

- Execute the named steps and the documented follow-ons whose trigger
  occurred: lockfile changed → commit it; lockfile is chezmoi-managed → run
  the sync-back step. Do not treat a follow-on as out of scope because it was
  not restated in the request.
- Read the exact flag from SKILL.md before running a command; a recalled flag
  is not evidence at any effort level.
- Verify with `apm deps list` / `apm targets` and state which output is the
  completion evidence.
- Read the Gotchas section before concluding APM is broken.
- Confirm before `--force` or a global update that can touch `.gitignore`;
  run global updates from a neutral cwd.

## Caller notes

- Effort: `medium` suffices for reference-driven command work; `high` when
  debugging unfamiliar APM behavior.
