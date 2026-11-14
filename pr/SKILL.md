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

Run `scripts/pr_template.py pick --repo-dir <checkout>` (path relative to this skill).
It prints the kind, the template file, and its headings:

- `repo`: the repository's own `pull_request_template.md`. Use its headings in order; it
  outranks both built-in templates.
- `review-first`: the repository the PR goes to (a fork's parent, else origin) is owned by the
  signed-in GitHub account or one of its organizations. Read
  [references/review-first.md](references/review-first.md).
- `show-me`: any other repository, such as an open-source project. Read
  [references/show-me.md](references/show-me.md), the upstream template, and write in the
  project's language.

Match the title style of the destination's five recent PR titles printed by `pick`; if their history is unavailable, it reports the error alongside the template.

Exit 2 means it could not tell (no GitHub origin, `gh` not signed in). Decide by the same
rules and pass `--kind` to `check`.

## Screen changes

A PR that changes what a screen shows must carry before/after PNGs taken on the real screen: the
changed part cropped and the whole screen. Read [references/screenshots.md](references/screenshots.md)
when writing one, for where the section goes and how `gh --attach` uploads the images. Never commit
the images to the repository.

## Check

Run `scripts/pr_template.py check <body-file> --repo-dir <checkout> --base <PR base branch>`
(`-` reads stdin; `check --help` describes the diff rules).
It fails on missing, misordered, or empty template headings, or added dependency, feature,
or extra names absent from the body.

## Done

- `check` passes on the body that is posted.
- A PR that adds a dependency, feature, setting, or mechanism explains why it is needed,
  which alternative fixes were not chosen, and when it can be removed.
- A PR that changes a screen shows its before/after PNGs in the posted body, with no local path
  left.
- Verified results and unverified points are listed apart; nothing unverified reads as fact.
