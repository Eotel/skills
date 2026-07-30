# markdown-lint-setup — Claude Opus 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Opus 5 executing this skill. On
self-load, apply them as written. When delegating, paste the Instructions
block into the subagent prompt. They do not override SKILL.md's configs or
steps.

## Instructions

- Copy the config files exactly as given; keep every `dir/**` glob (a bare
  `dir/` silently fails), apply the ignore list to BOTH `.remarkignore` and
  `.textlintignore`, and install both tools unless the user opts out. Do not
  tidy, improve, or extend the configs by your own judgment.
- Run the smoke test and read its output. A non-zero count is a config bug —
  fix it. The smoke test is the whole verification contract; add no extra
  review passes or verifier subagents on top.
- Report pre-existing prose warnings as counts; they are the user's content,
  not config bugs. Keep the report brief.

## Caller notes

- Effort: `low`/`medium` — the work is mechanical and quality holds there;
  raise only if the package-manager or workspace layout is genuinely
  ambiguous. Keep thinking on.
