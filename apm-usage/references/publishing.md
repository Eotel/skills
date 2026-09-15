# Publish An APM Package

Use `skill-creator` to author and validate a skill. This reference covers the
APM distribution layer only.

## Package shape

A distributable skill keeps `SKILL.md` at the skill root and includes only the
resources it uses:

```text
my-skill/
|-- SKILL.md
|-- agents/openai.yaml   # optional UI metadata
|-- references/          # optional, loaded conditionally
|-- scripts/             # optional deterministic helpers
`-- assets/              # optional output assets
```

For a repository containing several skills, install each skill by its repository
subpath. Keep the skill folder name equal to the frontmatter `name`.

## Distribution workflow

1. Validate the skill with OpenAI's `quick_validate.py` and any skill-specific
   script tests.
2. Add or update the package in `apm.yml`; prefer a tag or commit SHA when the
   consumer needs reproducibility.
3. Run `apm lock` or `apm install` and inspect `apm.lock.yaml`.
4. Test installation in a disposable directory using the installed APM CLI's
   documented flags.
5. Run `apm audit --ci` when the package will be consumed in CI or by others.
6. Publish using the registry or Git release workflow chosen by the repository.

Use `apm pack`, `apm publish`, and `apm view` only after reading their local
`--help`; registry flags and trust policy are version-sensitive.

## Release checks

- The description is concise and distinguishes this skill from its neighbors.
- Root instructions route to every required reference or script without loading
  unrelated material.
- No secret, local absolute path, generated cache, or unfinished placeholder is
  included.
- The package installs into the intended target and its lockfile pins the
  expected source.
- The README, release notes, and versioning policy are present only when the
  repository's publishing workflow requires them.
