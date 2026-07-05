# markdown-lint-setup — Claude Fable 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Fable 5 executing this skill. On
self-load, apply them as written. When delegating, paste the Instructions
block into the subagent prompt. They do not override SKILL.md's configs or
steps.

## Instructions

- Do only the setup: install both tools and write the config files verbatim.
  CI, lefthook/husky, VS Code, and extra rules are listed opt-ins — do not add
  them unprompted.
- Write every directory ignore as `dir/**`; a bare `dir/` silently fails.
  Keep `.remarkignore` and `.textlintignore` identical.
- Prove completion with the smoke test and quote both zero counts. A non-zero
  count is a config bug to fix, not a result to report around.
- Warn before installing (lockfile churn). Never run `remark --output` on the
  user's prose unless asked.

## Caller notes

- Effort: `medium` — the task is mechanical and needs restraint, not
  deliberation.
