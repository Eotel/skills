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
- **`devenv-init`** — scaffold a per-language devenv.sh project from
  [Eotel/devenv-templates](https://github.com/Eotel/devenv-templates) with
  toggleable features (direnv, delta, treefmt, git-hooks, postgres, mysql,
  redis, lsp, strict-types) and Python version patching.
