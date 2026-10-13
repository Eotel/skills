---
name: ast-grep-practice
description: Create and test structural lint rules with ast-grep. Use when a repository needs syntax checks that its general-purpose linter cannot express.
license: MIT
metadata:
  upstream-repo: mizchi/skills
  upstream-path: ast-grep-practice
  upstream-commit: 62f580819410cb1d398e4d7f234bbd0aad1c1a15
---

# ast-grep Practice

Use ast-grep for project-specific structural checks, alongside the repository's
existing linter and formatter.

## References

- When configuring ast-grep, writing or fixing rules, or adding CI checks, read
  [references/practice.md](references/practice.md). Follow the project's package
  manager and validate flags against the installed CLI.
- When matching unfamiliar syntax, read
  [references/kind-catalog.md](references/kind-catalog.md) and inspect its AST.
- When checking native paths passed to Node's `--import`, read
  [references/node-import-guard.md](references/node-import-guard.md) for the
  shared rules, scope, and test command.

## Completion

The rule classifies both valid and invalid examples correctly, its generated
snapshots have been reviewed, and the repository's configured scan reaches the
intended files. State any excluded syntax and false-positive assumptions.

## Boundary

For an unfamiliar repository, use a tool installed outside it from a trusted
source. Read the tool-provenance section of [references/practice.md](references/practice.md)
before installing or executing a project-local binary.

Preserve the repository's existing checks. Apply rewrites only within the
requested scope; review changes that need imports or runtime context manually.
