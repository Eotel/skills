# Eotel Skills

Reusable agent skills packaged for APM.

## Install

```bash
apm install -g Eotel/skills/agentic-docs --target claude
apm install -g Eotel/skills/devenv-init  --target codex
```

Codex can use shared Claude skills through a symlink from
`~/.codex/skills/<name>` to `~/.claude/skills/<name>`.

## Skills

- **`agentic-docs`** — bootstrap, audit, and maintain repository-local
  documentation systems for agentic software work.
- **`business-logic-extraction`** — plan, execute, and verify refactors that
  move hidden business decisions out of adapters into named services, policies,
  query helpers, lifecycle helpers, hooks, or route-local models.
- **`django`** — apply Django ORM/query placement guidance, including
  model-owned QuerySet extraction and focused verification.
- **`devenv-init`** — scaffold a per-language devenv.sh project from
  [Eotel/devenv-templates](https://github.com/Eotel/devenv-templates) with
  toggleable features (direnv, delta, treefmt, git-hooks, postgres, mysql,
  redis, lsp, strict-types) and Python version patching.
- **`repo-local-git-hooks`** — detect and repair global `core.hooksPath`
  overrides that bypass repository-local pre-commit or pre-push hooks.
- **`plan-exec`** — write repo-local execution plans under `docs/exec-plans/active/`, get approval, and keep progress updated through implementation.
