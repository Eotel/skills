---
name: pr
description: Write a PR body that says what went wrong, what changes, and where the reviewer should look, in the shape the repository expects. Use when writing or rewriting a pull request body.
license: MIT
metadata:
  upstream-repo: mattpocock/skills
  upstream-path: skills/engineering/pr
  upstream-commit: 24fe0ef7737efae15c87225755e9f6f5965e4888
---

# PR

Write the body in the template the repository calls for, then check it before creating or
editing the PR.

## Pick the template

Run `python3 scripts/pr_template.py pick --repo-dir <checkout>` (path relative to this skill).
It prints the kind, the template file, and its headings:

- `repo`: the repository's own `pull_request_template.md`. Use its headings in order; it
  outranks both built-in templates.
- `review-first`: the repository the PR goes to (a fork's parent, else origin) is owned by the
  signed-in GitHub account or one of its organizations. Read
  [references/review-first.md](references/review-first.md).
- `show-me`: any other repository, such as an open-source project. Read
  [references/show-me.md](references/show-me.md), the upstream template, and write in the
  project's language.

Exit 2 means it could not tell (no GitHub origin, `gh` not signed in). Decide by the same
rules and pass `--kind` to `check`.

## Screen changes

A PR that changes what a screen shows must carry before/after PNGs taken on the real screen: the
changed part cropped and the whole screen. Read [references/screenshots.md](references/screenshots.md)
when writing one, for where the section goes and how `gh --attach` uploads the images. Never commit
the images to the repository.

## Check

Run `python3 scripts/pr_template.py check <body-file> --repo-dir <checkout>` (`-` reads stdin).
It fails on a missing, misordered, or empty heading at the template's top heading level. It checks shape only: whether the first
section says what happened to users or data is still the writer's job. A repository's own CI
check on PR bodies, where one exists, has the final say.

## Done

- `check` passes on the body that is posted.
- A PR that changes a screen shows its before/after PNGs in the posted body, with no local path
  left.
- Verified results and unverified points are listed apart; nothing unverified reads as fact.
