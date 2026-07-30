# apm-usage — GPT-5.6

> Snapshot: 2026-07. Sources:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>,
> <https://learn.chatgpt.com/docs/models>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for GPT-5.6 (`sol` / `terra` / `luna`,
including the Codex harness) executing APM tasks with this reference. On
self-load, apply them as written. When delegating, paste the Instructions
block into the subagent prompt. They do not override SKILL.md's command
facts.

## Instructions

- Outcome: the requested package state is deployed and the lockfile
  committed where it changed. Choose the path yourself, but take flags
  verbatim from SKILL.md — never from memory.
- Read-only checks (`apm deps list` / `apm targets` / `apm outdated`) run
  freely; get approval before `--force`, destructive git, a global update
  that mutates `.gitignore`, or any widening beyond the requested
  operation.
- After each command, check `apm deps list` / `apm targets`; stop once the
  outcome holds.
- Read the Gotchas section before reporting a failure; most surprises are
  documented renames or by-design no-refresh behavior.

## Caller notes

- Reasoning: `low` suffices for this mechanical work; `medium` when
  debugging unfamiliar behavior.
- Model: `gpt-5.6-luna` or `gpt-5.6-terra` — `sol` is not needed here.
