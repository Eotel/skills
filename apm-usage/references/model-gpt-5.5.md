# apm-usage — GPT-5.5

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for GPT-5.5 (including the Codex harness)
executing APM tasks with this reference. On self-load, apply them as written.
When delegating, paste the Instructions block into the subagent prompt. They
do not override SKILL.md's command facts.

## Instructions

- Outcome: the requested package state is deployed and the lockfile committed
  where it changed. Choose the path yourself, but take flags verbatim from
  SKILL.md — never from memory.
- After each command, check `apm deps list` / `apm targets`; stop once the
  outcome holds.
- Before finalizing, run one pass over correctness, grounding in actual tool
  output, and safety of any irreversible step.
- Get approval before `--force`, destructive git, or a global update that
  mutates `.gitignore`.
- Read the Gotchas section before reporting a failure; most surprises are
  documented renames or by-design no-refresh behavior.

## Caller notes

- Reasoning: `low`/`medium` suffices for reference-driven command work.
