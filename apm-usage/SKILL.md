---
name: apm-usage
description: Reference for APM (Agent Package Manager) — apm.yml syntax, install / update / uninstall / outdated commands, target detection (including agent-skills), lockfile workflow, chezmoi integration. Read when you need exact field names or are debugging unfamiliar APM behavior. Do NOT auto-invoke on every apm-related task; user prompts about projects with installed skills can be handled with general APM knowledge. Invoke explicitly when the user mentions APM by name, asks to author or audit an apm.yml, runs into an unfamiliar APM command / error, or wonders why `apm install` did not pick up an upstream commit.
---

# APM (Agent Package Manager)

APM is a dependency manager for AI agent skills, instructions, prompts, and MCP servers. Think of it as npm for agent configuration.

Forked from `mizchi/skills/apm-usage` and updated for APM 0.14.x (Eotel fork).
Original authorship and structure by mizchi; the additions are documented in
[references/changes-from-mizchi.md](references/changes-from-mizchi.md).

## When this skill applies

- "add a skill to this project"
- "install skills globally"
- "create a skill for this repo"
- "set up apm.yml"
- "update agent dependencies"

## Core commands (APM 0.14+)

```bash
# Install all dependencies from apm.yml — honors apm.lock.yaml, does NOT
# re-resolve unpinned refs even if upstream has new commits.
apm install

# Install a specific package
apm install owner/repo
apm install owner/repo/skills/skill-name    # subdirectory skill
apm install owner/repo#v1.0.0               # pinned tag
apm install owner/repo#abc1234              # pinned commit SHA (recommended for reproducibility)

# Global (user-scope) install → ~/.apm/, deployed per target
apm install -g owner/repo/skills/skill-name

# Refuse to install if lockfile is missing or out of sync (CI-safe)
apm install --frozen        # NOTE: renamed from --frozen-lockfile

# Force-overwrite locally-authored files. Does NOT refresh refs.
apm install --force

# Refresh refs (the actual "update" command)
apm update                  # interactive plan + prompt
apm update --yes            # for CI; skip prompt
apm update --dry-run        # render plan, no changes

# Or update specific packages via the deps subcommand:
apm deps update --global --target agent-skills owner/repo
apm deps update --force                     # overwrite local changes

# Show which locked deps are behind upstream
apm outdated                # project scope
apm outdated -g             # global scope

# Remove
apm uninstall owner/repo
apm uninstall -g owner/repo

# Inspect
apm deps list               # project deps
apm deps list -g            # global deps
apm deps tree               # dependency tree
apm targets                 # show resolved deployment targets

# Remove packages no longer listed in apm.yml
apm prune

# Security scan
apm audit
```

### `apm install` vs `apm update` (critical distinction)

`apm install` is **lockfile-faithful**:

- If `apm.lock.yaml` exists, APM installs the exact SHAs it records.
- Unpinned refs (`owner/repo` with no `#tag` / `#sha`) are **not re-resolved**.
- Upstream commits made after the lockfile was generated will not appear
  until you run `apm update` (or `apm deps update`).
- This is the same model as `npm ci` vs `npm install`.

`apm install --update` exists but is **deprecated**; prefer `apm update`.

If you push a new commit to a skill repo and want it picked up, run
`apm update` (or `apm deps update --global --target agent-skills <repo>`),
**then** commit the refreshed `apm.lock.yaml`.

## apm.yml manifest

```yaml
name: my-project
version: 1.0.0
# Deployment targets. APM 0.12+ requires this when no marker directory
# (.claude/ / .github/ / etc.) exists at the repo root. Declare it
# unconditionally to make the install reproducible.
targets:
  - claude
  # - agent-skills      # deploys to .agents/skills/ (cross-client); see below
dependencies:
  apm:
    # GitHub shorthand
    - owner/repo
    - owner/repo#v1.0.0                  # pinned tag
    - owner/repo#abc1234                 # pinned commit SHA (most reproducible)
    - owner/repo/skills/skill-name       # subdirectory

    # Non-GitHub hosts
    - gitlab.com/org/repo
    - git: git@gitlab.com:org/repo.git
      path: skills/my-skill
      ref: main

    # Local path (dev only, not for -g)
    - ./packages/my-skill

  mcp:
    - io.github.github/github-mcp-server
scripts: {}
```

**Pinning recommendation**: prefer `#sha` over `#tag` over bare ref. `apm
update` prints `[!] N dependencies unpinned: ... -- add #tag or #sha to
prevent drift` for every bare ref, and bare refs silently move when upstream
force-pushes a branch.

### `scripts:` examples

`scripts:` is a name → command map. Register post-`apm install` setup or one-shot tasks used during development:

```yaml
scripts:
  postinstall: "echo 'skills installed; restart Claude Code to pick them up'"
  verify: "ls -1 .claude/skills | sort"
  audit: "apm audit"
```

Invoke with `apm run <name>` (e.g. `apm run verify`). `postinstall` runs automatically when `apm install` succeeds (hook). One-shot tasks (e.g. `apm run audit`) must be called explicitly.

### Lockfile (`apm.lock.yaml`) workflow

`apm install` and `apm update` write `apm.lock.yaml`. To guarantee
reproducibility:

- **project scope**: **commit** `apm.lock.yaml` so teammates resolve the
  same skill versions. Same idea as `package-lock` in `node_modules`.
- **global scope**: sync `~/.apm/apm.lock.yaml` via chezmoi so a new machine
  installs the same versions.
- In CI / on a new machine, use `apm install --frozen` to prevent drift
  (fails if the lockfile is missing or out of sync with `apm.yml`).
- Only run `apm update` (not `apm install`) when you intentionally want to
  refresh refs.

### Coexisting with chezmoi

If you manage dotfiles with chezmoi, the boundary with APM is:

| path | chezmoi | APM |
|---|---|---|
| `~/.apm/apm.yml` | managed (copied into source) | reads |
| `~/.apm/apm.lock.yaml` | managed (for new-machine reproducibility) | generates |
| `~/.apm/apm_modules/` | ignore (large cache) | manages |
| `~/.agents/skills/<name>/` | ignore (APM-managed) | deploy target for `agent-skills` |
| `~/.claude/skills/<name>/` | ignore unless self-managed | deploy target (or bridge symlink) |

Add the following to chezmoi's `.chezmoiignore`:

```
.apm/apm_modules
.agents/skills/<apm-managed-name>
.claude/skills/<apm-managed-name>
```

Watch for name collisions with your own skills (those copied into the chezmoi
source with `chezmoi add`). On collision APM overwrites at install time. See
the `chezmoi-management` skill for details.

#### Sync the lockfile back into chezmoi source

`apm install` / `apm update` rewrite `~/.apm/apm.lock.yaml` and at minimum
touch `generated_at`. Without syncing back, chezmoi prompts on the next
`chezmoi apply` ("`.apm/apm.lock.yaml` has changed since chezmoi last wrote
it?"). Fix by running the install/update inside a chezmoi `run_after_*.sh`
script that copies the updated lockfile back into the source dir:

```bash
# run_after_apm-install.sh (excerpt)
apm install --global --target agent-skills

if [ -n "${CHEZMOI_SOURCE_DIR:-}" ] && [ -f "$HOME/.apm/apm.lock.yaml" ]; then
  cp "$HOME/.apm/apm.lock.yaml" "$CHEZMOI_SOURCE_DIR/dot_apm/apm.lock.yaml"
fi
```

`CHEZMOI_SOURCE_DIR` is injected by chezmoi when it runs the script.

#### Bridging `~/.agents/skills/` into `~/.claude/skills/` and `~/.codex/skills/`

`--target agent-skills` deploys to `~/.agents/skills/`. Claude Code reads
`~/.claude/skills/` and Codex reads `~/.codex/skills/`. Symlink each entry
into both:

```bash
agents_dir="$HOME/.agents/skills"
for src in "$agents_dir"/*; do
  [ -d "$src" ] || continue
  name=$(basename "$src")
  for dst_dir in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    dst="$dst_dir/$name"
    if [ ! -e "$dst" ] || [ -L "$dst" ]; then
      ln -sfn "$src" "$dst"
    fi
  done
done
```

Skip slots that are already real directories (probably chezmoi-managed or
APM `--target claude`-deployed there directly). Combining `--target claude`
**and** `--target agent-skills` lays down the file twice — pick one per
skill to keep ownership clear.

## Creating skills in a repository

Follow the [agentskills.io](https://agentskills.io/specification) open standard. Publishing-focused guide (repo layout, tag/release, dependency declaration, verification checklist) is in [references/publishing.md](references/publishing.md).

### Directory structure

```
my-repo/
└── skills/
    └── my-skill/
        ├── SKILL.md           # Required
        ├── scripts/           # Optional: executable code
        ├── references/        # Optional: detailed docs
        └── assets/            # Optional: templates, resources
```

### SKILL.md format

```markdown
---
name: my-skill
description: One-line description of what this skill does and when to use it.
---

# Skill body

Instructions for the AI agent. Keep under 500 lines.
Move detailed reference material to references/ directory.
```

### Frontmatter fields

| Field | Required | Constraints |
|-------|----------|-------------|
| `name` | Yes | 1-64 chars, lowercase alphanumeric + hyphens, must match directory name |
| `description` | Yes | 1-1024 chars, describe what + when |
| `license` | No | SPDX identifier or license file reference |
| `compatibility` | No | Environment requirements (max 500 chars) |
| `metadata` | No | Arbitrary key-value pairs |

### Name validation rules

- Lowercase letters, numbers, hyphens only
- Cannot start or end with hyphen
- No consecutive hyphens (`--`)
- Must match the parent directory name

### Users install with

```bash
apm install owner/my-repo/skills/my-skill
```

## Skill patterns for library authors

### Single skill in a library repo

```
my-library/
├── skills/
│   └── my-library-guide/
│       └── SKILL.md
├── src/
└── package.json
```

### Multiple skills (monorepo)

```
my-org-skills/
├── skill-a/
│   └── SKILL.md
├── skill-b/
│   └── SKILL.md
└── skill-c/
    └── SKILL.md
```

Users install individually: `apm install owner/my-org-skills/skill-a`

## Target detection

APM resolves the deployment target in this priority order:

1. `--target` / `-t` CLI flag
2. `targets:` field in `apm.yml`
3. Auto-detect from marker directories in the project

| Target value | Skills deployed to | Notes |
|---|---|---|
| `claude` | `.claude/skills/` | Marker: `.claude/` |
| `copilot` | `.github/skills/` | Marker: `.github/` |
| `cursor` | `.cursor/skills/` | Marker: `.cursor/` |
| `codex` | `.codex/skills/` | Marker: `.codex/` |
| `gemini` | `.gemini/skills/` | |
| `windsurf` | `.windsurf/skills/` | |
| `opencode` | `.opencode/skills/` | |
| `agent-skills` | `.agents/skills/` | Cross-client; bridge symlinks into per-tool dirs |
| `all` | every per-tool dir above (excludes `agent-skills`) | Combine with `agent-skills` for both |
| `copilot-cowork` | experimental | Enable via `apm experimental enable copilot-cowork` |

`agent-skills` is the **recommended target for skills that should work across
Claude Code + Codex** (and any future cross-tool ECC). Pair with the bridge
script in the chezmoi section above.

**APM 0.12+ no longer falls back to `copilot` when no marker directory exists**
— `apm install` errors out asking for an explicit target. **Always declare
`targets:` in `apm.yml`** so the install never depends on the working tree's
directory layout:

```yaml
name: my-project
version: 1.0.0
targets:
  - claude
dependencies:
  apm:
    - owner/repo
```

Or override per command with `--target claude` / `--target agent-skills`.

Use `apm targets` to print the resolved targets for the current project.

## Global vs project scope

| | Project (`apm install`) | Global (`apm install -g`) |
|---|---|---|
| Manifest | `./apm.yml` | `~/.apm/apm.yml` |
| Modules | `./apm_modules/` | `~/.apm/apm_modules/` |
| Lockfile | `./apm.lock.yaml` | `~/.apm/apm.lock.yaml` |
| Deploy to | `./.claude/skills/` | `~/.claude/skills/` |
| Local `.apm/` content | Deployed | Skipped |

## Authentication

For private repos, APM resolves auth automatically:

1. `gh auth login` (GH_TOKEN) — zero-config if already logged in
2. `git credential fill` — OS keychain, SSH keys
3. `GITHUB_APM_PAT` environment variable — for CI or explicit setup

No extra configuration needed if `gh auth login` is done.

## Priority and conflict resolution

- Local skills always override dependency skills on name collision
- Dependencies processed in declaration order; first wins
- `apm install --force` overwrites local files on collision. It does **not**
  refresh refs — use `apm update` for that.

## Model-specific execution guides

The facts above are model-independent — command semantics never change per
model. What differs is how an executing model should *use* this reference.
When the executing model is known, load **at most one** matching guide and
apply its Instructions as your operating instructions; when delegating, paste
that guide's Instructions block into the subagent prompt. Otherwise skip.

- Claude Opus 4.8: [references/model-opus-4.8.md](references/model-opus-4.8.md)
- Claude Sonnet 5: [references/model-sonnet-5.md](references/model-sonnet-5.md)
- Claude Fable 5: [references/model-fable-5.md](references/model-fable-5.md)
- GPT-5.5 / Codex: [references/model-gpt-5.5.md](references/model-gpt-5.5.md)

Each guide is written as directly injectable instructions plus caller-side
knobs under "Caller notes". Guides must not restate or override the command
facts, the install-vs-update distinction, or the lockfile workflow in this
file.

## Gotchas

- **`apm deps update` mutates the current working directory's `.gitignore`**:
  it appends `apm_modules/` whenever it runs, even if the cwd is unrelated
  to APM. Run global updates from a neutral cwd (e.g. `cd ~`) or revert the
  `.gitignore` diff afterwards.
- **`apm install` does not pick up upstream commits.** If you push a new
  commit to a skill repo and want it deployed, run `apm update` (or `apm
  deps update --global --target agent-skills <repo>`); then commit the new
  lockfile.
- **`--frozen-lockfile` was renamed to `--frozen`**. The old name no longer
  works in 0.14.
- **`apm install --update` is deprecated**; prefer `apm update`.
- **`--target all` excludes `agent-skills`**. To deploy to both per-tool
  dirs and the cross-client dir, pass `--target all,agent-skills`.
- **`generated_at` in lockfiles**: every `apm install` updates this
  timestamp even when nothing else changes. If chezmoi-managed, sync the
  lockfile back to source dir to avoid TTY prompts on next `chezmoi apply`.
