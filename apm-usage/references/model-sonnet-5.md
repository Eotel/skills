# Claude Sonnet 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Sonnet 5.

## Fit

- Strong default for running APM commands and agentic install/update flows.
  Sonnet 4.6 prompts transfer.
- More tool-eager and self-verifying than 4.6; give it a stop condition so the
  eagerness does not keep re-running or re-checking past success.

## Execution Prompting

- Name the acceptance evidence that ends the task: e.g. "`apm deps list` shows
  the package at the expected ref" or "`apm targets` prints the intended
  target". Without it, Sonnet keeps verifying.
- Do not improvise flags from memory; read the exact flag from SKILL.md. Sonnet
  respects effort literally at the low end, so keep effort high when a step
  touches install, update, or the lockfile.
- Adaptive thinking is on by default — steer with a tight objective, not extra
  process text, when latency matters.
- Before diagnosing "APM is broken", check the Gotchas section; the failure is
  usually a documented rename or a no-refresh-by-design behavior.
- Treat `--force` and the `.gitignore` mutation from `apm deps update` as
  destructive: confirm first, and prefer a neutral cwd for global updates.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Run APM commands with exact flags from the reference. Stop when the named check
(apm deps list / apm targets) confirms the outcome — do not keep re-verifying.
Check Gotchas before calling APM broken. Confirm before --force or a global
update that can rewrite .gitignore.
```
