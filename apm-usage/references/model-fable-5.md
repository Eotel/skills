# Claude Fable 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Fable 5.

## Fit

- Best on hard, ambiguous, long-running APM work end to end; reliable but can be
  excessive on a routine one-command install.
- Strong yet literal instruction following; steer with brief intent, not long
  enumerated rules, and fence scope so it does not add unrequested work.

## Execution Prompting

- Fence scope hard: apply exactly the APM change asked. Do not expand into
  unrequested dotfile or chezmoi refactors, and do not open defensive git
  branches or draft extra files unless told to.
- Lead with the outcome, then let it act once it has enough info — do not have
  it over-survey command options. Read exact flags from SKILL.md rather than
  improvising them.
- Audit progress claims against tool output: confirm a deploy with `apm deps
  list` / `apm targets` before reporting done. A fresh-context verifier subagent
  beats self-critique here.
- Set explicit pause boundaries — stop and confirm only for destructive or
  scope-changing actions: `--force`, `.gitignore` mutation from `apm deps
  update`, or global updates.
- Before deciding "APM is broken", check the Gotchas section; long agentic runs
  can drift into dense guesses, so ground the diagnosis in the documented notes.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Do only the requested APM change; no unrequested chezmoi/dotfile refactors or
defensive branches. Read exact flags from the reference. Confirm before --force
or global updates that touch .gitignore. Verify with apm deps list / apm targets
and check Gotchas before calling APM broken.
```
