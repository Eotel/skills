---
name: apm-usage
description: Use APM to author or debug apm.yml, install or update agent packages, inspect targets, and resolve lockfile behavior.
---

# APM Usage

Use the installed `apm` CLI as the authority for flags and supported targets.
Run `apm --version` and the relevant `<command> --help` before changing a manifest
or diagnosing behavior; this reference intentionally does not cache the full CLI
manual.

## Choose the operation

| Desired outcome | Command |
|---|---|
| Reproduce the locked package state | `apm install` |
| Enforce a synchronized lockfile in CI | `apm install --frozen` |
| Refresh dependency refs | `apm update` |
| Preview an update | `apm update --dry-run` |
| Resolve without deploying | `apm lock` |
| Inspect installed packages or dependency causes | `apm deps list`, `apm deps tree`, `apm deps why <package>` |
| Inspect target resolution | `apm targets` |
| Detect upstream drift | `apm outdated` |
| Diagnose the local APM environment | `apm doctor` |
| Validate package integrity | `apm audit` or `apm audit --ci` |

`apm install` follows `apm.lock.yaml`; it does not refresh an existing locked
ref. Use `apm update` when the intended outcome is to pick up a newer upstream
commit. Treat `--force` as an overwrite boundary, not as an update mechanism.

## Workflow

1. Before changing package state, read the applicable `apm.yml`, lockfile, and
   resolved targets.
2. Confirm the installed CLI version and the exact command flags from `--help`.
3. Run the narrow command that matches the requested outcome.
4. Verify the resulting package, ref, and target using `apm deps list` and
   `apm targets`; inspect the lockfile diff when it changed.
5. Report any generated or deployed files separately from source changes.

Project lockfiles are normally committed. User-scope lockfiles may instead be
managed by the user's dotfile system; use the `chezmoi-management` skill only
when the user explicitly asks to manage that boundary.

## Manifest routing

For exact `apm.yml` fields and dependency forms, read
[references/manifest-schema.md](references/manifest-schema.md). Validate any
field that may have changed against the installed CLI before relying on the
reference.

For publishing and package layout, read
[references/publishing.md](references/publishing.md). Use `skill-creator` for the
skill's own content and APM only for packaging, installation, and distribution.

## Safety boundaries

- Inspect before using `--force`; it may overwrite locally authored files.
- Use comma-separated targets when selecting more than one target. Repeating
  `--target` keeps only the last value in current APM releases.
- `all` does not include every explicit or experimental target. Read
  `apm install --help` before assuming its expansion.
- Do not hand-edit `apm.lock.yaml`; regenerate it with APM.
- Preserve unrelated manifest entries, local files, and deployed packages.
- Authentication, target support, and registry behavior are version-sensitive;
  rely on `apm doctor`, `apm config`, and command help rather than remembered
  defaults.

## Completion

Stop when the requested package state is visible in APM's inspection commands
and the manifest/lockfile diff contains only the intended change. If APM's local
help contradicts these instructions, follow the installed CLI and report the
documentation drift.
