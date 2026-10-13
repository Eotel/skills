# The brief

The session reads the repository (its `AGENTS.md`, `CLAUDE.md`, committed
skills), the environment's `~/.claude/CLAUDE.md`, and this text. Rules that
live only on this machine, or that you give local sessions by habit, do not
exist for it unless one of those carries them.

## What it carries

- **The task and its contract**: the issue number, and where the acceptance
  criteria are (the issue body, or a named comment on it).
- **Where to read it**: `gh api repos/<owner>/<repo>/issues/<n>` and
  `.../comments`.
- **Rules the user holds for this repository or task** that neither the
  repository nor the environment states, for example: tests check behavior,
  none that only pin wording or comments; the language of the PR; the assignee.
- **Attribution**: a session adds a `Claude-Session` trailer to its commits and
  the session link to the PR body. Where the repository forbids such lines,
  `attribution.sessionUrl: false` in its committed `.claude/settings.json`
  turns both off (docs); until that is there, the brief says to leave them out.
- **The boundary**: whether it may merge; that it requests no reviews and
  writes to no tracker it cannot reach; that findings outside the task are
  reported, not fixed; that an open decision is written in the PR as a point to
  confirm, not settled by its own recommendation.
- **How it ends**: the PR URL, the result per acceptance criterion, the tests
  it ran, and what it could not verify.

## Shape

Write it in the language the repository's PRs use.

```text
Implement issue #<n> in <owner>/<repo> and open a PR.

## Read
- The issue body and its comments (gh api ...). <Which one> holds the acceptance criteria.
- AGENTS.md, and the design documents the issue names.

## Work
- Check the current behavior before changing it. Add only tests that check behavior.
- Open the PR (assignee ...) and watch CI until it is green.

## Do not
- Merge, request reviews, or write to <tracker>.
- Fix anything outside the task, or decide what is still undecided.

## Finish with
The PR URL, the result per acceptance criterion, the tests you ran, and what you could not verify.
```

Add a section for what is particular to the task: a dependency on another PR,
a file another open PR touches, a tool it must build before it can verify.
