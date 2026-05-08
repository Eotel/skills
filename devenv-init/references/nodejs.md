# Node.js / TypeScript templates (planned)

Not yet implemented. Tracked for: `nodejs`, `nodejs+next`, `nodejs+vite`, `bun`.

When added, expected toggles:
- `features.packageManager` enum: `npm | pnpm | yarn | bun` (defaults from `dot_codex/skills/setup-pm/`)
- `features.typescript.enable` (tsconfig strict, `@types/node`)
- `features.biome.enable` vs `features.eslint.enable`

Until then, hand-write devenv.nix using `languages.javascript.enable` / `languages.typescript.enable` from devenv docs.
