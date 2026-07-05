# Claude Sonnet 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Sonnet 5.

## Fit

- Strong default for coding and agentic tasks; comfortable with this
  multi-step file-writing bootstrap.
- More tool-eager and self-verifying than earlier Sonnet; give it a clear stop
  condition so it does not keep iterating on pre-existing prose errors.

## Execution Prompting

- Effort: high is a fine default; the task is mechanical, so lower effort is
  acceptable for a clean, single-package repo.
- Define the evidence that ends the task: two zero smoke-test counts plus a
  warning-count summary. Existing prose warnings are the user's content to fix,
  not a config bug, so do not chase them to zero.
- Sonnet's self-verification suits the smoke test — run it and confirm both
  counts are `0`; a non-zero count is a config bug (wrong path or file not at
  repo root), not a stop-and-report signal.
- Write the configs exactly as given. The `dir/**` ignore glob is load-bearing;
  a bare `dir/` silently walks in. Do not "improve" the configs unprompted.
- Keep `.remarkignore` and `.textlintignore` in sync as one list.
- Keep the both-tools default; warn before installing (lockfile churn) and do
  not run `remark --output` to rewrite prose unless asked.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Stop when both smoke-test counts are 0 and you have summarized warning counts;
pre-existing prose warnings are the user's content, not a config bug. Copy the
configs verbatim including every `dir/**` glob, keep both ignore files in sync,
install both tools, warn before install, and do not auto-fix prose.
```
