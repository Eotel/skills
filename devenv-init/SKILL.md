---
name: devenv-init
description: Initialize a reproducible devenv.sh project from Eotel/devenv-templates. Use for supported language stacks and development-service options.
license: MIT
---

# devenv-init

Scaffold a reproducible per-language development environment in the current directory using [Eotel/devenv-templates](https://github.com/Eotel/devenv-templates).

## Prerequisites

- `nix` (Nix 2.18+)
- `devenv` CLI
- `direnv` (system-installed via nix-darwin / home-manager)

## Options

| Option | Values | Default | Notes |
|---|---|---|---|
| `--lang` | `python` | (required) | Only Python is currently supported. |
| `--framework` | `django` / `fastapi` / `flask` | (none) | Pairs with `--lang python`. |
| `--python` | `3.10` / `3.11` / `3.12` / `3.13` | `3.12` | Patches `languages.python.version`, `requires-python`, `target-version`, `pythonVersion`. |
| `--name` | string | dirname | Patches `pyproject.toml` `name` and `packages = ["src/<name>"]`. |
| `--with-direnv` / `--no-direnv` | flag | enabled | direnv hint module. |
| `--with-delta` / `--no-delta` | flag | enabled | delta as local git pager. |
| `--with-treefmt` / `--no-treefmt` | flag | enabled | treefmt + nixfmt. |
| `--with-git-hooks` / `--no-git-hooks` | flag | enabled | devenv git-hooks (pre-commit). |
| `--with-lsp` / `--no-lsp` | flag | disabled | Claude Code LSP integration hint. |
| `--strict-types` / `--no-strict-types` | flag | disabled | Flips `pyrightconfig.json` to strict. |
| `--with-postgres` / `--no-postgres` | flag | varies | `python+django` defaults ON, others OFF. |
| `--with-mysql` / `--no-mysql` | flag | disabled | mysqlclient build env + service. |
| `--with-redis` / `--no-redis` | flag | disabled | devenv redis service. |
| `--with-sqlite` / `--no-sqlite` | flag | enabled | sqlite CLI in PATH. |

## Flow

1. Resolve template name: `--lang python --framework django` → `python+django`.
2. `nix flake init -t github:Eotel/devenv-templates#<template>` (in CWD).
3. Patch `devenv.nix` `features = { … };` block with toggles.
4. Patch python version in `devenv.nix` / `pyproject.toml` / `pyrightconfig.json`.
5. Patch project name in `pyproject.toml` if `--name` was given.
6. Patch `pyrightconfig.json` `typeCheckingMode` if `--strict-types`.
7. Run `nix-instantiate --parse devenv.nix` for syntax validation.
8. Print next-step hints (`direnv allow` / `uv sync`).

The actual implementation lives at `scripts/scaffold.sh`. Invoke it directly with the parsed arguments — do not reimplement the patching logic in Codex.

## Reference material

- `references/options-matrix.md` — option × template combinations with notes.
- `references/python.md` — Python-specific details (db, framework, strict types).

## Project-level rules to honor

- After editing Nix, run `nix flake check` or at least
  `nix-instantiate --parse` on the changed file.
- `git-hooks.hooks` is the modern devenv pre-commit (not the deprecated `pre-commit.hooks`).
- After scaffolding, use `uv add` to install Python deps; do not edit `pyproject.toml` `dependencies` directly.
