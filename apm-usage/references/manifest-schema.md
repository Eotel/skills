# `apm.yml` Reference

Use this as a shape guide, then verify version-sensitive fields with the
installed APM CLI. `apm init`, `apm install --help`, and `apm lock` are the
authorities for the installed release.

## Minimal manifest

```yaml
name: my-project
version: 1.0.0
targets:
  - agent-skills
dependencies:
  apm:
    - owner/repo#<tag-or-sha>
  mcp: []
scripts: {}
```

Declare `targets` when installation should not depend on directory-marker
auto-detection. Target names and expansion rules change; obtain the accepted
values from `apm install --help`.

## APM dependencies

Common forms:

```yaml
dependencies:
  apm:
    - owner/repo
    - owner/repo#v1.0.0
    - owner/repo#abc1234
    - owner/repo/path/to/package
    - git: https://gitlab.com/org/repo.git
      path: skills/my-skill
      ref: v2.0.0
    - ./packages/local-package
```

Prefer a tag or commit when reproducibility matters. Local-path packages are
for development and are not portable to user-scope installs.

## MCP dependencies

Registry and self-defined MCP entries have version-sensitive fields. Add them
with `apm install --mcp ...` where possible so APM writes the supported shape.
Inspect the result before committing it; do not copy a remembered schema into a
manifest.

## Scripts

`scripts` is a name-to-command map executed with `apm run <name>`:

```yaml
scripts:
  verify: "apm audit --ci"
```

Keep secrets out of the manifest and generated lockfile. Use environment
references supported by the installed APM release.

## Lockfile contract

- Regenerate `apm.lock.yaml` with `apm lock`, `apm install`, or `apm update`.
- Commit project lockfiles unless repository policy says otherwise.
- Use `apm install --frozen` in CI when manifest/lockfile drift must fail.
- Inspect lockfile diffs after updates; do not edit resolved refs by hand.
