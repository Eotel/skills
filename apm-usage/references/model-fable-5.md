# apm-usage — Claude Fable 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Fable 5 executing APM tasks with this
reference. On self-load, apply them as written. When delegating, paste the
Instructions block into the subagent prompt. They do not override SKILL.md's
command facts.

## Instructions

- Do only the requested APM change. No unrequested chezmoi/dotfile refactors,
  defensive git branches, or extra files.
- Take flags verbatim from SKILL.md; never improvise them from memory.
- Verify with `apm deps list` / `apm targets` before reporting done — a
  remembered state is not evidence.
- Stop and confirm before `--force`, global updates, or anything that mutates
  `.gitignore` (`apm deps update` does); run global updates from a neutral
  cwd.
- Check the Gotchas section before diagnosing breakage — most surprises
  (renamed flags, install-not-refreshing) are documented behavior.

## Caller notes

- Effort: `medium` suffices for routine installs; Fable-level deliberation is
  wasted on one-command tasks.
