# Base Markdown Lint Configuration

Adapt command prefixes to the repository's package manager. Use workspace-root
dependency flags only when the repository is a workspace.

## Packages

For the combined setup:

```text
remark remark-cli remark-toc remark-lint
remark-preset-lint-consistent remark-preset-lint-recommended
remark-frontmatter remark-gfm
textlint @textlint-ja/textlint-rule-preset-ai-writing
```

Confirm compatible current versions before installation.

## `.remarkrc.json`

```json
{
  "plugins": [
    "remark-preset-lint-consistent",
    "remark-preset-lint-recommended",
    "remark-gfm",
    ["remark-toc", { "heading": "目次|table of contents", "maxDepth": 3 }],
    ["remark-frontmatter", ["yaml"]]
  ],
  "settings": {
    "bullet": "-",
    "emphasis": "*",
    "strong": "*"
  }
}
```

Remove `remark-toc` if the user does not want generated tables of contents.

## `.textlintrc.json`

```json
{
  "rules": {
    "@textlint-ja/preset-ai-writing": true
  }
}
```

Prefer a narrow rule option or `allows` entry over disabling the preset when the
repository has an intentional phrase.

## Paired ignore files

Start from the directories that exist in the repository. A typical shared list
is:

```text
node_modules/**
dist/**
build/**
coverage/**
test-results/**
playwright-report/**
.wrangler/**
.output/**
.vite/**
.agents/**
.claude/**
.codex/**
.impeccable/**
.serena/**
pnpm-lock.yaml
CHANGELOG.md
```

Do not exclude `.github/**` automatically: it may contain Markdown templates and
contributor documentation the project wants checked. Add other generated or
agent-context directories only when inspection shows they should be outside the
documentation surface.

## Package scripts

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

Use only the scripts for tools actually installed and adapt `pnpm` to the local
package manager.

## Verification

Run the check-only scripts. Then create a temporary invalid Markdown file under
one ignored directory, verify that neither tool reports it, and remove only that
file and directory after confirming they were created by the current check.

Report:

- dependencies, configs, ignores, and scripts added;
- command exit status and the output summary;
- ignored-path smoke-test result;
- pre-existing content findings that remain.
