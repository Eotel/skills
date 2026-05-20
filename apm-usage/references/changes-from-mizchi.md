# Changes from `mizchi/skills/apm-usage`

This file documents what the Eotel fork added on top of mizchi's original
`apm-usage` skill, so the diff is easy to audit and upstream if needed.

Forked at: `mizchi/skills` commit available 2026-05-20, APM CLI 0.13.0 era.
Updated for: APM CLI 0.14.0.

## Why fork

The mizchi skill was written for APM 0.12-0.13 and is missing several
behaviors that bit Eotel in production:

- `apm install` honors the lockfile and does **not** re-resolve unpinned
  refs. The original skill said "Only run `apm install --update` when you
  intentionally want to update the lockfile" but did not state the
  consequence: a freshly pushed upstream commit is invisible to `apm
  install` until `apm update` runs.
- `apm install --update` is deprecated in 0.14 in favor of `apm update`.
- `--frozen-lockfile` is renamed to `--frozen`.
- `--target agent-skills` (cross-client deploy to `.agents/skills/`) is
  the recommended target for skills used by both Claude Code and Codex,
  but mizchi predates this feature.
- `apm deps update` appends `apm_modules/` to the current working
  directory's `.gitignore` regardless of whether the cwd is an APM
  project — a footgun worth calling out.
- Bridge-symlink pattern for `~/.agents/skills/` → `~/.claude/skills/` /
  `~/.codex/skills/` is not in the original.
- chezmoi sync-back script pattern (`cp $HOME/.apm/apm.lock.yaml
  $CHEZMOI_SOURCE_DIR/dot_apm/`) is not in the original.

## What changed

### `SKILL.md` / `SKILL-ja.md`

| Section | Change |
|---|---|
| Frontmatter `description` | Broadened to call out the lockfile / upstream-commit case so the skill triggers on those debugging sessions. |
| Header note | Attribution to mizchi + pointer to this changes file. |
| `## Core commands` | Renamed `## Core commands (APM 0.14+)`. Added `apm update` (top-level), `apm deps update`, `apm outdated`, `apm prune`, `apm targets`. Marked `apm install --update` deprecated. Renamed `--frozen-lockfile` to `--frozen`. Added `#sha` pinning example. Added "`apm install` vs `apm update`" callout explaining lockfile-faithful behavior. |
| `## apm.yml manifest` | Added `agent-skills` target comment. Added `#sha` pinning example. Added "Pinning recommendation" note about `apm update` warning on bare refs. |
| `### Lockfile workflow` | Mentions both `apm install` and `apm update` write the lockfile. Renamed `--frozen-lockfile` to `--frozen`. |
| `### Coexisting with chezmoi` | Added `~/.agents/skills/` row to boundary table. Added "Sync the lockfile back into chezmoi source" subsection with the `run_after_*.sh` `cp ... $CHEZMOI_SOURCE_DIR` pattern. Added "Bridging" subsection with the symlink loop. |
| `## Target detection` | Added priority order explanation. Expanded table to include `agent-skills`, `all`, `copilot-cowork`, gemini/windsurf/opencode. Added `apm targets` pointer. |
| `## Priority and conflict resolution` | Clarified `apm install --force` does NOT refresh refs. |
| `## Gotchas` (new) | apm_modules .gitignore mutation, upstream commit invisibility, --frozen-lockfile rename, --update deprecation, --target all excludes agent-skills, generated_at lockfile churn. |

### `references/`

- New file: `references/changes-from-mizchi.md` (this file).
- `references/publishing.md`, `references/manifest-schema.md`: unchanged so
  far; carry-over from mizchi.

## How to upstream later

If mizchi wants the changes back, this file is the diff manifest. The
Gotchas section, the `apm install` vs `apm update` callout, and the
agent-skills target documentation are the highest-value additions; the
chezmoi bridge/sync scripts are arguably Eotel-specific tooling that may
not belong upstream.
