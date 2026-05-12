# DESIGN.md Format

Reference for the Google Labs `DESIGN.md` format (v0.1.0, format version `alpha`).
Use this when a frontend or UI repository needs a single source of truth for
visual design tokens that both humans and coding agents can read.

## Not The Same As `docs/design-docs/`

- `docs/design-docs/` holds stable architectural decisions and project rules.
- `DESIGN.md` at the repo root defines visual design tokens (colors,
  typography, spacing, components) as a machine-readable file.
- Keep both. They do not replace each other.

## When To Create

Create `DESIGN.md` when the repository:

- Renders UI and currently has design tokens scattered across CSS, Tailwind
  config, component code, or external design tools.
- Has multiple agents or contributors making UI changes that need a shared
  token vocabulary.
- Wants design changes to be reviewable in pull requests.

Skip when the repository has no UI surface.

## File Shape

`DESIGN.md` is a two-layer file at the repository root:

1. YAML front matter: machine-readable tokens.
2. Markdown body: human-readable intent for each section.

## YAML Front Matter

Top-level keys:

| Key | Required | Purpose |
|---|---|---|
| `name` | yes | Brand or product name. |
| `version` | no | Format version. Current value: `alpha`. |
| `description` | no | Short brand summary. |
| `colors` | no | Named color tokens. |
| `typography` | no | Named typographic scales (`h1`, `body`, `code`, ...). |
| `spacing` | no | Spacing scale (`xs`, `sm`, `md`, `lg`, `xl`, ...). |
| `rounded` | no | Border-radius scale or per-component overrides. |
| `components` | no | Component-level token bindings. |

Token reference syntax: `{path.to.token}`. Example:

```yaml
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-tertiary}"
    rounded: "{rounded.sm}"
```

Fallback and nested reference patterns are not documented in the upstream
spec.

### Valid Component Properties

Use only these property names inside `components.*`. Unknown properties are
accepted by `lint` as warnings, so a misnamed property passes with exit
code `0` but silently loses meaning downstream (exporters drop it, strict
consumers ignore it).

- `backgroundColor`
- `textColor`
- `typography`
- `rounded` (not `borderRadius`)
- `padding`
- `size`
- `height`
- `width`

Inspect findings with `--format json` to catch warnings that exit code `0`
hides:

```bash
npx @google/design.md lint --format json DESIGN.md \
  | jq '.findings[] | select(.severity == "warning")'
```

## Markdown Body

Use this section order:

1. Overview — brand philosophy and cultural intent.
2. Colors — why each color was chosen.
3. Typography — why each typeface was chosen.
4. Layout — spacing principles.
5. Elevation & Depth — visual hierarchy.
6. Shapes — corner radii and geometric character.
7. Components — role of each UI element.
8. Do's and Don'ts — explicit allowed and forbidden patterns.

Write intent in the body. Keep numeric values in the front matter.

## CLI

The `@google/design.md` npm package provides the official tooling.

### lint

```bash
npx @google/design.md lint DESIGN.md
npx @google/design.md lint --format json DESIGN.md
cat DESIGN.md | npx @google/design.md lint -
```

Exit code: `1` on errors, `0` otherwise. Default `--format` is `json`.

### diff

```bash
npx @google/design.md diff DESIGN.md DESIGN-v2.md
npx @google/design.md diff --format json DESIGN.md DESIGN-v2.md
```

Exit code: `1` when regressions are detected.

### export

```bash
npx @google/design.md export --format json-tailwind DESIGN.md > tailwind.theme.json
npx @google/design.md export --format css-tailwind  DESIGN.md > theme.css
npx @google/design.md export --format tailwind      DESIGN.md
npx @google/design.md export --format dtcg          DESIGN.md > tokens.json
```

`--format` is required. Accepted values: `json-tailwind`, `css-tailwind`,
`tailwind`, `dtcg`.

### spec

```bash
npx @google/design.md spec
npx @google/design.md spec --rules
npx @google/design.md spec --rules-only --format json
```

`--format` accepts `markdown` (default) or `json`.

## Programmatic API

```ts
import { lint } from '@google/design.md/linter'

const report = lint(markdownString)
report.findings      // Finding[]
report.summary       // { errors, warnings, info }
report.designSystem  // Parsed DesignSystemState
```

Only `lint()` is documented in the upstream README.

## Notes

- Windows: use the `designmd` alias in `package.json` scripts instead of
  `design.md` to avoid file-association conflicts.
- No dedicated config file (`.designmdrc`, `design.config.json`, ...). All
  configuration is CLI-driven.
- No official CI recipe ships with the package. If you want CI enforcement,
  add `npx @google/design.md lint DESIGN.md` as a step. Do not invent
  project-specific recipes inside this skill.

## Verification

When introducing `DESIGN.md` to a repository:

- Run `npx @google/design.md lint DESIGN.md` and resolve every error.
- Also inspect warnings with `npx @google/design.md lint --format json DESIGN.md`.
  Exit code `0` is not sufficient: unknown component properties such as
  `borderRadius` validate as warnings and are silently dropped by downstream
  exporters.
- If exporting tokens to Tailwind or DTCG, regenerate the export and commit
  the generated artifact under `docs/generated/` with a header pointing back
  to `DESIGN.md` as the source of truth.

## References

- Upstream repository: <https://github.com/google-labs-code/design.md> (Apache-2.0)
- Library directory: <https://designmd.app/library/>
- Background article: <https://eotel.github.io/blogs/posts/2026/05/design-md-culture/>
