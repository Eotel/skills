# Claude Opus 4.8 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Opus 4.8.

## Fit

- Long-horizon APM work: authoring apm.yml, auditing installs, debugging why a
  dependency did not deploy. Opus 4.7 prompts transfer.
- Literal, scope-respecting execution; the risk is doing exactly the named step
  and nothing adjacent, so name every step you actually want.

## Execution Prompting

- Opus follows scope literally and will not silently generalize. When a lockfile
  refresh should also trigger the chezmoi sync-back step, say so explicitly — it
  will not infer that follow-on from "update the deps".
- Do not improvise command flags from memory; read the exact flag from SKILL.md
  before running it. Opus favors reasoning over tool calls at low/medium effort,
  so make the lookup an explicit instruction — a recalled guess is not evidence
  at any effort level.
- Verify outcomes with `apm deps list` / `apm targets` rather than asserting a
  deploy landed; name which of those outputs is the evidence that ends the task.
- Before concluding "APM is broken", read the Gotchas section — a literal
  reading of an error is usually a documented rename or a no-refresh behavior.
- Treat `--force` and any `.gitignore` mutation from `apm deps update` as
  destructive: confirm before running, and run global updates from a neutral cwd.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Follow the named APM steps literally and do not generalize scope. Read exact
flags from the reference before running. Confirm before --force or any global
update that can touch .gitignore. Verify with apm deps list / apm targets, and
apply the chezmoi sync-back step only when told the lockfile changed.
```
