---
name: markdown-lint-setup
description: Set up remark and textlint for Markdown structure and Japanese prose linting in a JavaScript or TypeScript repository.
---

# Markdown Lint Setup

Add the Markdown checks the user requested while preserving the repository's
package-manager, script, and formatting conventions.

## Decide the stack

- Use `remark` for Markdown structure, formatting, GFM, frontmatter, and optional
  table-of-contents handling.
- Use `textlint` with `@textlint-ja/preset-ai-writing` for Japanese prose checks.
- Install both when the user requests the combined house setup. If the user names
  only one tool, preserve that choice unless repository policy already requires
  both.
- Skip the Japanese prose dependency for an English-only repository unless the
  user explicitly wants it.

## Workflow

1. Inspect the package-manager lockfile, workspace root, existing Markdown tools,
   scripts, ignore files, hooks, and CI.
2. Confirm current package names and supported configuration from installed
   metadata or primary documentation before adding dependencies.
3. Read [references/configuration.md](references/configuration.md) for the base
   configs, paired ignore rules, scripts, and smoke test. Adapt only where the
   repository already has an equivalent convention.
4. Add optional hooks, CI, editor settings, or auto-fix commands only when the
   user requests them or the repository already standardizes on them.
5. Run both the configured checks and the ignore smoke test for every tool added.
   Report pre-existing prose failures separately from configuration failures.

## Invariants

- Ignore directories using file-matching globs such as `dir/**`; a bare `dir/`
  does not reliably protect nested Markdown files in remark.
- Keep `.remarkignore` and `.textlintignore` aligned when both tools should scan
  the same scope.
- Use check-only commands during verification. Run rewriting commands such as
  `remark --output` or `textlint --fix` only when the user requested content
  changes.
- Preserve existing package scripts and do not hand-edit lockfiles.
- Treat warnings in existing prose as user content to report, not as permission
  to rewrite the repository.

## Completion

The requested linters are installed and callable through repository scripts,
their configs parse, ignored agent/tool directories do not appear in results,
and any remaining content findings are summarized without being silently fixed.
