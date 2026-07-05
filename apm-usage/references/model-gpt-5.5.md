# GPT-5.5 Guide

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is GPT-5.5 (including the Codex harness).

## Fit

- Efficient on outcome-first APM tasks: state the destination and constraints,
  then let it pick the path. Avoid legacy process-heavy step stacks.
- Biased to action; pair that with explicit stop rules and confirmation gates so
  it does not run irreversible steps unprompted.

## Execution Prompting

- Give the outcome, not a step list: "package X deployed to target Y, lockfile
  committed". Let 5.5 choose commands, but require exact flag fidelity — flags
  come from SKILL.md, not from memory.
- Add a stopping rule: after each command, ask whether the core request is now
  satisfied (confirmed via `apm deps list` / `apm targets`) and stop if so.
- Run a verification loop before finalizing: correctness, grounding against tool
  output, and safety of any irreversible action.
- Before reporting a failure, consult the Gotchas section; most surprises there
  are renamed flags or by-design no-refresh behavior, not real breakage.
- No destructive action without approval: `--force`, `.gitignore` mutation from
  `apm deps update`, or destructive git steps need explicit sign-off first.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Target the outcome, not steps, but use exact flags from the reference. After
each command check apm deps list / apm targets and stop once the outcome holds.
Run a final correctness/grounding/safety pass. Get approval before --force,
destructive git, or global updates that rewrite .gitignore.
```
