# markdown-lint-setup — Claude Opus 4.8

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Opus 4.8 executing this skill. On
self-load, apply them as written. When delegating, paste the Instructions
block into the subagent prompt. They do not override SKILL.md's configs or
steps.

## Instructions

- Apply the ignore list to BOTH `.remarkignore` and `.textlintignore` — the
  rule covers both files, not only the one currently being edited.
- Copy the config files exactly as given; keep every `dir/**` glob (a bare
  `dir/` silently fails). Do not tidy or improve the configs.
- Install both remark and textlint unless the user opts out.
- Run the smoke test and read its output before claiming done. A non-zero
  count is a config bug — fix it.
- Report pre-existing prose warnings as counts; they are the user's content,
  not config bugs.

## Caller notes

- Effort: `medium`; raise only if the package-manager or workspace layout is
  genuinely ambiguous.
