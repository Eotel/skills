---
name: apm-usage
description: Reference for APM (Agent Package Manager) — apm.yml syntax, install / update / uninstall / outdated commands, target detection (including agent-skills), lockfile workflow, chezmoi integration. Read when you need exact field names or are debugging unfamiliar APM behavior. Do NOT auto-invoke on every apm-related task; user prompts about projects with installed skills can be handled with general APM knowledge. Invoke explicitly when the user mentions APM by name, asks to author or audit an apm.yml, runs into an unfamiliar APM command / error, or wonders why `apm install` did not pick up an upstream commit.
---

# APM (Agent Package Manager)

APM is a dependency manager for AI agent skills, instructions, prompts, and MCP servers. Think of it as npm for agent configuration.

`mizchi/skills/apm-usage` をベースに、APM 0.14.x 向けに Eotel が fork した版。
構造とオリジナルの記述は mizchi に帰属する。追加内容は
[references/changes-from-mizchi.md](references/changes-from-mizchi.md) を参照。

## When this skill applies

- "add a skill to this project"
- "install skills globally"
- "create a skill for this repo"
- "set up apm.yml"
- "update agent dependencies"

## Core commands (APM 0.14+)

```bash
# apm.yml の依存をインストール。lockfile を尊重し、unpinned ref は
# upstream に新 commit があっても再解決しない。
apm install

# 個別パッケージ
apm install owner/repo
apm install owner/repo/skills/skill-name    # subdirectory skill
apm install owner/repo#v1.0.0               # tag で pin
apm install owner/repo#abc1234              # commit SHA で pin（最も再現性高い）

# Global (user-scope) install → ~/.apm/, target ごとに展開
apm install -g owner/repo/skills/skill-name

# lockfile が manifest と一致しなければ fail（CI 用）
apm install --frozen        # NOTE: --frozen-lockfile から rename

# ローカル変更を上書き。ref は更新しない。
apm install --force

# ref を更新する正規コマンド
apm update                  # 対話プラン
apm update --yes            # CI 用、prompt スキップ
apm update --dry-run        # プランだけ表示

# パッケージ指定で update する場合は deps サブコマンド:
apm deps update --global --target agent-skills owner/repo
apm deps update --force                     # ローカル変更を上書き

# lockfile が upstream に遅れている依存を確認
apm outdated                # project スコープ
apm outdated -g             # global スコープ

# Remove
apm uninstall owner/repo
apm uninstall -g owner/repo

# Inspect
apm deps list               # project deps
apm deps list -g            # global deps
apm deps tree               # 依存ツリー
apm targets                 # 解決済み target を表示

# apm.yml に無いパッケージを削除
apm prune

# セキュリティスキャン
apm audit
```

### `apm install` と `apm update` の違い（重要）

`apm install` は **lockfile に忠実**:

- `apm.lock.yaml` があればそこに記録された SHA を install する
- unpinned ref（`#tag` / `#sha` 指定なし）は **再解決しない**
- lockfile 生成後に upstream に commit が追加されても、`apm update`
  （または `apm deps update`）を実行するまで反映されない
- `npm ci` vs `npm install` と同じモデル

`apm install --update` も存在するが **deprecated**。`apm update` を使う。

自分の skill repo に新 commit を push した後、それを反映させたい場合は
`apm update`（または `apm deps update --global --target agent-skills <repo>`）
を実行し、更新された `apm.lock.yaml` を commit する。

## apm.yml manifest

```yaml
name: my-project
version: 1.0.0
dependencies:
  apm:
    # GitHub shorthand
    - owner/repo
    - owner/repo#v1.0.0                  # pinned tag
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

### `scripts:` の実例

`scripts:` は name → command のマップ。`apm install` 後の setup、開発中のワンショット処理を登録する:

```yaml
scripts:
  postinstall: "echo 'skills installed; restart Claude Code to pick them up'"
  verify: "ls -1 .claude/skills | sort"
  audit: "apm audit"
```

呼び出し: `apm run <name>`（例: `apm run verify`）。`postinstall` は `apm install` 成功時に自動実行される（hook）。ワンショット処理（例: `apm run audit`）は明示的に呼ぶ。

### lockfile (`apm.lock.yaml`) の運用

`apm install` と `apm update` の両方が `apm.lock.yaml` を書き換える。再現性を担保するため:

- **project スコープ**: `apm.lock.yaml` を **commit** する（チーム間で同じ skill version を解決するため）
- **global スコープ**: `~/.apm/apm.lock.yaml` は chezmoi で同期すると新マシンで同じ version が入る
- CI / 新マシンでは `apm install --frozen` を使って drift を防ぐ（lockfile が無い、または manifest と一致しなければ fail）
- ref を意図的に更新したいときだけ `apm update`（`apm install` ではない）

### chezmoi との共存

chezmoi で dotfiles を管理している場合、APM との境界:

| path | chezmoi | APM |
|---|---|---|
| `~/.apm/apm.yml` | 管理（source にコピー） | 読む |
| `~/.apm/apm.lock.yaml` | 管理（新マシン再現性のため） | 生成 |
| `~/.apm/apm_modules/` | ignore（大きいキャッシュ） | 管理 |
| `~/.agents/skills/<name>/` | ignore（APM-managed） | `agent-skills` target の展開先 |
| `~/.claude/skills/<name>/` | 自前管理でなければ ignore | 展開先または bridge symlink |

chezmoi 側の `.chezmoiignore` に次を追加:

```
.apm/apm_modules
.agents/skills/<apm-managed-name>
.claude/skills/<apm-managed-name>
```

自作 skill（`chezmoi add` で source にコピーしたもの）とは名前が衝突しないよう注意。衝突時は APM が install 時に上書きする。詳細は `chezmoi-management` skill を参照。

#### lockfile を chezmoi source に書き戻す

`apm install` / `apm update` は毎回 `~/.apm/apm.lock.yaml` の
`generated_at` を更新する。source に書き戻さないと次の `chezmoi apply` で
"`.apm/apm.lock.yaml` has changed since chezmoi last wrote it?" の TTY
プロンプトが出る。chezmoi の `run_after_*.sh` 内で install/update を実行し、
直後に source へコピーするのが定石:

```bash
# run_after_apm-install.sh （抜粋）
apm install --global --target agent-skills

if [ -n "${CHEZMOI_SOURCE_DIR:-}" ] && [ -f "$HOME/.apm/apm.lock.yaml" ]; then
  cp "$HOME/.apm/apm.lock.yaml" "$CHEZMOI_SOURCE_DIR/dot_apm/apm.lock.yaml"
fi
```

`CHEZMOI_SOURCE_DIR` は chezmoi がスクリプト実行時に注入する環境変数。

#### `~/.agents/skills/` を `~/.claude/skills/` / `~/.codex/skills/` に bridge

`--target agent-skills` は `~/.agents/skills/` に展開する。Claude Code は
`~/.claude/skills/`、Codex は `~/.codex/skills/` を読むので、各エントリを
両方に symlink する:

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

既に実ディレクトリが存在するスロットはスキップ（chezmoi-managed か APM が
`--target claude` で直接展開した可能性）。`--target claude` と
`--target agent-skills` を同時に使うと同じ skill が二重に展開されるため、
skill ごとにどちらかに統一する。

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

target は次の優先順位で解決される:

1. `--target` / `-t` CLI フラグ
2. `apm.yml` の `targets:` フィールド
3. project root のマーカーディレクトリから auto-detect

| Target 値 | 展開先 | 備考 |
|---|---|---|
| `claude` | `.claude/skills/` | マーカー: `.claude/` |
| `copilot` | `.github/skills/` | マーカー: `.github/` |
| `cursor` | `.cursor/skills/` | マーカー: `.cursor/` |
| `codex` | `.codex/skills/` | マーカー: `.codex/` |
| `gemini` | `.gemini/skills/` | |
| `windsurf` | `.windsurf/skills/` | |
| `opencode` | `.opencode/skills/` | |
| `agent-skills` | `.agents/skills/` | cross-client。bridge symlink で各 tool dir に配る |
| `all` | 上記 per-tool dir 全て（`agent-skills` は **除く**） | 両方ほしければ `--target all,agent-skills` |
| `copilot-cowork` | 実験的 | `apm experimental enable copilot-cowork` |

`agent-skills` は **Claude Code + Codex の両方で使う skill 用の推奨 target**。
上記 chezmoi セクションの bridge スクリプトとセットで使う。

**APM 0.12+ は marker dir が無いとき `copilot` にフォールバックしない** —
`apm install` は明示的な target を要求してエラーになる。**常に
`apm.yml` で `targets:` を宣言** し、ディレクトリレイアウトに依存しない:

```yaml
name: my-project
version: 1.0.0
targets:
  - claude
dependencies:
  apm:
    - owner/repo
```

`--target claude` / `--target agent-skills` で都度上書きも可能。

`apm targets` で現在解決されている target を表示できる。

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
- `apm install --force` はローカル上書きのみ。ref は更新しない（`apm update` を使うこと）

## モデル別実行ガイド

上記の事実はモデル非依存（コマンドの意味はモデルで変わらない）。変わるのは
実行モデルがこのリファレンスを「どう使うか」。実行モデルが判明している場合
のみ該当ガイドを**最大1つ**ロードし、Instructions をそのまま自分の operating
instructions として適用する。サブエージェントに委譲する場合は、そのモデル用
ガイドの Instructions ブロックをプロンプトに貼り付ける。不明ならスキップ。

- Claude Opus 4.8: [references/model-opus-4.8.md](references/model-opus-4.8.md)
- Claude Opus 5: [references/model-opus-5.md](references/model-opus-5.md)
- Claude Sonnet 5: [references/model-sonnet-5.md](references/model-sonnet-5.md)
- Claude Fable 5: [references/model-fable-5.md](references/model-fable-5.md)
- GPT-5.5 / Codex: [references/model-gpt-5.5.md](references/model-gpt-5.5.md)
- GPT-5.6 / Codex: [references/model-gpt-5.6.md](references/model-gpt-5.6.md)

各ガイドは注入可能な命令文と、呼び出し側ノブ(Caller notes: effort など)で
構成される。本ファイルのコマンド事実、install と update の区別、lockfile
ワークフローを上書き・再説明してはならない。

## 落とし穴

- **`apm deps update` は cwd の `.gitignore` を勝手に書き換える**:
  実行のたびに `apm_modules/` を追記する。cwd が APM と無関係でも実行される。
  グローバル更新は中立な cwd（`cd ~`）で実行するか、終了後に `.gitignore`
  diff を revert する。
- **`apm install` は upstream の新 commit を拾わない**。skill repo に
  push したものを反映させたいときは `apm update`（または
  `apm deps update --global --target agent-skills <repo>`）を実行し、
  生成された lockfile を commit すること。
- **`--frozen-lockfile` は `--frozen` に rename**。0.14 で旧名は通らない。
- **`apm install --update` は deprecated**。`apm update` を使う。
- **`--target all` は `agent-skills` を含まない**。両方展開するなら
  `--target all,agent-skills`。
- **lockfile の `generated_at`**: 中身が変わらなくても毎回 timestamp が
  更新される。chezmoi 管理なら source に書き戻さないと次の apply で
  TTY プロンプトが出る（上記 chezmoi セクション参照）。
