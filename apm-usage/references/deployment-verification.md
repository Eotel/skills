# Deployment verification

## Procedure

1. Before changing package state, read the live `apm.yml`, lockfile, and resolved
   targets. For a dotfiles-managed manifest, compare it with its maintained source
   and apply the intended source state before updating; inspect local edits first.
2. Confirm the installed CLI version and the exact command flags from `--help`.
3. Run the narrow command that matches the requested outcome. For a targeted
   update, map the CLI dependency name to its repository owner and subpath in the
   live manifest and lockfile; the same name can refer to another owner.
4. Inspect the resulting package, ref, target, and manifest/lockfile diff. Compare
   the deployed files with the intended published package contents, including each
   requested client's path or bridge. Package reports alone do not prove deployment.
5. Report any generated or deployed files separately from source changes.

## Evidence

Treat inspection output and `resolved_commit` as supporting evidence. Compare the
materialized files: a skill subdirectory can remain current while the repository's
HEAD changes. Check every requested client's resolved path; a canonical directory
match does not establish that all clients load it.

Why: a stale managed manifest can select a different repository, and an update
report or recorded commit does not establish what each client actually loads.
