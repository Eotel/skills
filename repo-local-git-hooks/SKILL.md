---
name: repo-local-git-hooks
description: Diagnose and repair repository Git hooks bypassed by a global core.hooksPath. Use when a repository's own pre-commit or pre-push checks do not run on commit or push.
---

# Repo Local Git Hooks

Use this skill when a repo expects local hooks but Git is using a global `core.hooksPath`. The common symptom is: `.git/hooks/pre-commit` exists, yet commits/pushes run only a global Nix/gitleaks hook or no repo-specific lint at all.

## Where

- `scripts/audit_git_hooks.sh`: run from the target repository to confirm the
  failure mode (effective hooks path versus the repository's own); `--fix` runs
  the repository's hook installer and writes the repo-local `core.hooksPath`.
- `references/fix.md`: manual audit commands and how to read them, the installer
  pattern, the order of the fix, and a session-start guard. Read before changing
  a repository's hook installer.

## Done

`git rev-parse --git-path hooks` equals `$(git rev-parse --git-common-dir)/hooks`,
and the repository's pre-commit and pre-push hooks actually run.

## Boundary

Change the repository's own installer and local Git config. A global
`core.hooksPath` (for example from nix-darwin or home-manager) lives outside the
repository and stays as it is.
