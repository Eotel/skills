---
name: markdown-lint-setup
description: Bootstrap remark + textlint for Markdown linting in a Node.js / pnpm / npm / yarn / bun project. Installs remark (lint presets + GFM + TOC + frontmatter) and textlint with @textlint-ja/textlint-rule-preset-ai-writing to catch AI-flavored Japanese prose. Always excludes AI agent context dirs (.agents .claude .codex .impeccable .serena .github) so generated agent files are never touched. Use this skill whenever the user asks to set up markdown lint, prose lint, textlint, remark, remark-lint, table-of-contents generation, AI writing detection, or wants to catch "AI っぽい" Japanese patterns like 「**項目:** 説明」 list items, hype expressions like 「完全に」「革新的」, or to keep a docs/ folder consistent — even when they only mention one of the tools (remark OR textlint), set up both unless they explicitly opt out. Strongly prefer this skill over hand-rolling textlint/remark configs from scratch.
---

# markdown-lint-setup

Bootstraps `remark` + `textlint` in a JS/TS repo so Markdown is consistently formatted, has a table of contents where requested, and is screened for AI-flavored prose patterns — **without** the linters reaching into AI agent context dirs (`.claude/`, `.codex/`, `.agents/`, `.impeccable/`, `.serena/`, `.github/`).

## When to use

Trigger when the user wants Markdown linting set up. Common phrasings:
- 「textlint 入れて」「remark 入れて」「markdown の lint 整備して」
- "set up textlint" / "add remark lint" / "lint my docs"
- 「AI っぽい文章を弾きたい」「目次を自動生成したい」

If the user mentions only one of remark/textlint, default to installing both — they cover different layers (structure vs prose) and ignore lists need to stay in sync. Confirm only if the user pushes back.

## Why both tools

- **remark** = structural / formatting lint for Markdown (heading consistency, list-marker style, table cell padding, link/reference integrity). Also generates a `## 目次` table of contents in-place.
- **textlint + preset-ai-writing** = prose lint that flags "AI っぽい" Japanese patterns such as `**項目:** 説明` list formatting, 「完全に」「革新的」「シームレス」 hype words, and over-confident predictive phrasing. Pure-English prose still passes through cleanly; the preset focuses on Japanese.

Neither tool's defaults exclude AI agent dirs, so they walk into `.claude/`, `.codex/`, etc. and either rewrite them (remark's `--output`) or drown the user in irrelevant errors. The ignore files in this skill stop that.

## Setup workflow

Follow this order. Detect the package manager from lockfiles (`pnpm-lock.yaml` → pnpm, `bun.lockb` → bun, `yarn.lock` → yarn, otherwise npm). The examples below use pnpm; substitute `npm install --save-dev` / `yarn add -D` / `bun add -d` as needed. For a pnpm workspace root, use `pnpm add -Dw`.

### 1. Install packages

```bash
pnpm add -Dw \
  remark remark-cli remark-toc remark-lint \
  remark-preset-lint-consistent remark-preset-lint-recommended \
  remark-frontmatter remark-gfm \
  textlint @textlint-ja/textlint-rule-preset-ai-writing
```

`textlint` requires v15.1.0 or later for the preset; the latest releases satisfy this automatically.

### 2. Write `.remarkrc.json`

```json
{
  "plugins": [
    "remark-preset-lint-consistent",
    "remark-preset-lint-recommended",
    "remark-gfm",
    [
      "remark-toc",
      {
        "heading": "目次|table of contents",
        "maxDepth": 3
      }
    ],
    ["remark-frontmatter", ["yaml"]]
  ],
  "settings": {
    "bullet": "-",
    "emphasis": "*",
    "strong": "*"
  }
}
```

The `remark-toc` plugin only fires on files that contain a heading matching `目次` or `table of contents`, so leaving it on by default is safe.

### 3. Write `.remarkignore`

**Critical syntax gotcha**: `.remarkignore` is NOT plain `.gitignore`. To ignore a directory you must write a glob that matches files inside it (`some_dir/**`). A bare directory name with a trailing slash (`some_dir/`) silently does nothing — remark walks into it anyway. Always use the `**` form for directories; this is the load-bearing detail of the whole skill, and getting it wrong silently defeats the AI-dir ignore.

```
# Dependencies / build artifacts
node_modules/**
dist/**
build/**
coverage/**
test-results/**
playwright-report/**
.wrangler/**
.output/**
.vite/**

# AI agent context / tooling directories (no TOC, no lint)
.agents/**
.claude/**
.codex/**
.impeccable/**
.serena/**
.github/**

# Lockfiles / generated
pnpm-lock.yaml
CHANGELOG.md
```

If the project has additional agent/tool dirs (look for top-level dotted dirs like `.cursor/`, `.windsurf/`, `.aider/`, `.continue/`), add them to both ignore files using the same `dir/**` form.

### 4. Write `.textlintrc.json`

```json
{
  "rules": {
    "@textlint-ja/preset-ai-writing": true
  }
}
```

If the user later wants to relax specific rules, prefer per-rule options over disabling the whole preset — see [the preset README](https://github.com/textlint-ja/textlint-rule-preset-ai-writing) for `allows`, `disableBoldListItems`, `disableEmojiListItems`, `disableAbsolutenessPatterns`, etc.

### 5. Write `.textlintignore`

Keep this list identical to `.remarkignore`. Use the same `dir/**` glob form for consistency (textlint accepts both `dir` and `dir/**`, but standardizing on the same syntax both files prevents copy-paste drift). Drift between the two is the #1 source of "but I set up the ignore" bug reports.

```
# Dependencies / build artifacts
node_modules/**
dist/**
build/**
coverage/**
test-results/**
playwright-report/**
.wrangler/**
.output/**
.vite/**

# AI agent context / tooling directories
.agents/**
.claude/**
.codex/**
.impeccable/**
.serena/**
.github/**

# Lockfiles / generated
pnpm-lock.yaml
CHANGELOG.md
```

### 6. Add npm scripts

Insert these into the root `package.json` `scripts` block. Keep existing scripts (`lint`, `format`, `secretlint`, etc.) untouched.

```json
{
  "scripts": {
    "remark": "remark . --output --quiet",
    "remark:check": "remark . --frail --quiet",
    "textlint": "textlint \"**/*.md\"",
    "textlint:fix": "textlint --fix \"**/*.md\"",
    "lint:md": "pnpm remark:check && pnpm textlint"
  }
}
```

Mapping:
- `remark` — writes back (`--output`) so it generates TOCs and applies safe rewrites. Run on demand.
- `remark:check` — read-only, `--frail` flips warnings into a non-zero exit. Use in CI / pre-push.
- `textlint` / `textlint:fix` — same split for textlint.
- `lint:md` — convenience entrypoint for "run all Markdown lint."

If the project uses npm/yarn/bun, swap the `pnpm` prefix inside `lint:md` accordingly (`npm run remark:check && npm run textlint`, etc.).

### 7. Verify

Run both checks and confirm:

```bash
pnpm remark:check 2>&1 | tail -5
pnpm textlint 2>&1 | tail -5
```

Existing prose will almost always surface warnings/errors — that's normal and is the user's content to fix, not a config bug. What you want to verify is that **no file inside an ignored directory appears in the output**. A quick smoke test:

```bash
mkdir -p .claude/_lint-smoke && printf '# x\n## 目次\nbody\n' > .claude/_lint-smoke/dummy.md
pnpm remark:check 2>&1 | grep -c "_lint-smoke/dummy" || true   # → 0
pnpm textlint    2>&1 | grep -c "_lint-smoke/dummy" || true   # → 0
rm -rf .claude/_lint-smoke
```

Both counts must be `0`. If either is non-zero, the ignore file is misconfigured — re-check the path (some shells expand globs unexpectedly) and that the file is at the repo root.

## Optional add-ons

Mention these to the user as opt-ins; don't install unprompted.

### Lefthook / pre-commit integration

If the repo already uses [lefthook](https://lefthook.dev/) (look for `lefthook.yml`), append:

```yaml
pre-commit:
  jobs:
    - name: remark
      glob: "*.md"
      run: pnpm exec remark --frail --quiet {staged_files}

    - name: textlint
      glob: "*.md"
      run: pnpm exec textlint {staged_files}
```

If the project uses [`husky`](https://typicode.github.io/husky/) + [`lint-staged`](https://github.com/lint-staged/lint-staged), add to `package.json`:

```json
{
  "lint-staged": {
    "*.md": ["remark --frail --quiet", "textlint"]
  }
}
```

### GitHub Actions

A minimal job for `.github/workflows/docs-lint.yml`:

```yaml
name: docs-lint
on:
  pull_request:
    paths: ["**/*.md"]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: "pnpm"
      - run: pnpm install --frozen-lockfile
      - run: pnpm lint:md
```

### VS Code

If the user uses VS Code:

`.vscode/extensions.json`
```json
{
  "recommendations": [
    "unifiedjs.vscode-remark",
    "3w36zj6.textlint"
  ]
}
```

## Edge cases & gotchas

- **Monorepos**: install at the workspace root with `pnpm add -Dw`. The `remark .` / `textlint "**/*.md"` patterns walk the whole tree, so a single root config covers all packages. Don't duplicate configs per-package unless a package needs distinct rules.
- **No Japanese content**: `@textlint-ja/textlint-rule-preset-ai-writing` is Japanese-focused; on English-only repos it stays quiet but adds a dependency. If the user explicitly says English-only, skip the textlint half and only install remark.
- **`pnpm-lock.yaml` churn**: installing this many packages adds ~400 deps to the lock. Warn the user before installing in a PR-sensitive repo.
- **`.github/` ignore**: `.github` mainly holds YAML workflows, but PR templates and `CONTRIBUTING.md` sometimes live there. If the user wants those linted, remove `.github` from both ignore files but keep the rest.
- **`AGENTS.md` (no dot)**: this is a regular file at the repo root, *not* in `.agents/`. It is linted by default — that's intentional, since it's project documentation.
- **`remark`'s `--output` flag rewrites files in place**: only run `pnpm remark` (without `:check`) when the working tree is clean or the user has accepted that risk. `pnpm remark:check` is always safe.

## What to report back to the user

After running steps 1–7, summarize:
1. Which files were created (`.remarkrc.json`, `.remarkignore`, `.textlintrc.json`, `.textlintignore`) and which scripts were added.
2. Warning/error counts from the initial verification run, so the user knows the scope of pre-existing content issues (most are auto-fixable via `pnpm remark` / `pnpm textlint:fix`).
3. Confirmation that the ignore smoke test returned `0` for both linters.
4. Suggested next step: run `pnpm remark` and `pnpm textlint:fix` to auto-fix the mechanical issues, then review remaining errors as prose to revise.

## References

- Inspiration: https://qiita.com/semba_yui/items/60246a8831e466b508cc
- Preset: https://github.com/textlint-ja/textlint-rule-preset-ai-writing
- remark plugins: https://github.com/remarkjs/remark/blob/main/doc/plugins.md
- textlint rules: https://textlint.github.io/docs/rule.html
