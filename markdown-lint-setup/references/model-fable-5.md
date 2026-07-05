# Claude Fable 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Fable 5.

## Fit

- Built for the hardest, long-running end-to-end work; this bootstrap is well
  within reach and needs restraint more than horsepower.
- Reliable on simple tasks but can be excessive there — fence scope so it does
  not over-deliver on a mechanical setup.

## Execution Prompting

- Effort: medium is plenty for this routine task; high default can lead to
  over-planning the setup it should just execute.
- Scope fence: install remark + textlint and write the configs, nothing more.
  CI (GitHub Actions), lefthook/husky, VS Code, and extra rules are listed
  opt-ins — do not add them unprompted.
- Fable follows instructions literally, so copy the configs verbatim. The
  `dir/**` ignore glob is load-bearing; a bare `dir/` silently fails.
- Keep `.remarkignore` and `.textlintignore` as one synced list.
- Back completion claims with evidence, not assertion: run the smoke test and
  quote both `0` counts. Treat a non-zero count as a config bug to fix.
- Warn before installing (lockfile churn) and do not run `remark --output` on
  the user's prose unless asked. Keep the both-tools default.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Do only the setup: install both tools and write the configs verbatim, including
every `dir/**` glob, with both ignore files in sync. Do not add CI, lefthook,
husky, VS Code, or extra rules unless asked. Prove done by quoting the two zero
smoke-test counts; a non-zero count is a config bug. Warn before install.
```
