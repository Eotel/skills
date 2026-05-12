# Agentic Docs Templates

Use this reference when creating a new docs scaffold from the bundled templates.

## Template Generation

Prefer the script:

```bash
python scripts/bootstrap_docs.py --target /path/to/repo --dry-run
python scripts/bootstrap_docs.py --target /path/to/repo
```

The script copies `assets/templates/` into the target repository and replaces
`YYYY-MM-DD` with today's date. It skips existing files unless `--force` is
provided.

Use templates only for bootstrap. For an existing project, edit in place and
preserve local conventions.

## Template Catalog

- `assets/templates/AGENTS.md`
- `assets/templates/ARCHITECTURE.md`
- `assets/templates/docs/index.md`
- `assets/templates/docs/design-docs/index.md`
- `assets/templates/docs/design-docs/core-beliefs.md`
- `assets/templates/docs/PLANS.md`
- `assets/templates/docs/QUALITY_SCORE.md`
- `assets/templates/docs/exec-plans/plan-template.md`
- `assets/templates/docs/exec-plans/tech-debt-tracker.md`
- `assets/templates/docs/product-specs/index.md`
- `assets/templates/docs/generated/README.md`
- `assets/templates/docs/references/README.md`

## Optional Templates

Templates under `assets/optional/` are not part of the default scaffold and
are not copied by `scripts/bootstrap_docs.py`. Copy them manually only when
the target repository needs them.

- `assets/optional/DESIGN.md` — Google Labs DESIGN.md (v0.1.0, format `alpha`)
  for visual design tokens. Use in frontend or UI repositories. The
  destination is the repository root.

  ```bash
  cp "$SKILL_ROOT/assets/optional/DESIGN.md" "$REPO_ROOT/DESIGN.md"
  ```

  Format reference: [design-md-format.md](design-md-format.md).

  Validate with the upstream CLI. Exit code `0` is not sufficient because
  unknown component properties are warnings — inspect JSON findings too:

  ```bash
  npx @google/design.md lint DESIGN.md
  npx @google/design.md lint --format json DESIGN.md \
    | jq '.findings[] | select(.severity == "warning")'
  cat DESIGN.md | npx @google/design.md lint -
  ```

## Cleanup After Copying

- Delete unused categories rather than leaving placeholders.
- Rename sections to match the target project vocabulary.
- Replace every `TBD` and any remaining `YYYY-MM-DD`.
- Add project-specific verification commands.
- Do not leave generic placeholder text in committed docs.
