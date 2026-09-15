---
name: codex-prompting
description: Create or revise Codex task prompts, AGENTS.md instructions, system prompts, or subagent definitions. Use codex-orchestrator-brief for full multi-wave handoff packages.
---

# Codex Prompting

Write the smallest instruction that changes Codex's decisions in the intended
way. Assume the model can inspect, plan, edit, and verify without a scripted
itinerary.

## Choose the artifact

- A one-off outcome belongs in a task prompt.
- Durable repository conventions and contextual document pointers belong in
  `AGENTS.md`.
- A reusable assistant's role, authority, and communication defaults belong in a
  system or developer prompt.
- A specialized delegated role belongs in `.codex/agents/*.toml`.
- A repository-backed multi-wave handoff belongs to `codex-orchestrator-brief`.

Use the corresponding file under `templates/` as a starting point. Delete unused
sections instead of filling them with boilerplate.

## Write the instruction

Read [references/prompt-design.md](references/prompt-design.md) when revising
prompt behavior or migrating older instructions. Read
[references/codex-harness.md](references/codex-harness.md) for durable Codex and
`AGENTS.md` mechanics. Read
[references/subagents-and-sessions.md](references/subagents-and-sessions.md) only
when the user explicitly wants delegation, parallel work, or session continuity.

State:

1. the outcome;
2. observable completion evidence;
3. hard boundaries and authorization limits;
4. repository or domain context the model cannot cheaply discover;
5. output requirements only when the default is unsuitable.

Prefer decision rules to fixed procedures. Keep absolute language for safety,
permissions, public contracts, and other true invariants.

## Current facts

Model names, effort levels, CLI flags, config keys, and tool behavior change.
Verify them against current official OpenAI documentation or the installed Codex
CLI before writing them. Put model and effort in runtime configuration unless the
prompt itself must choose among workers.

Codex can use project instructions to trigger subagents, but delegation consumes
additional tokens and write-heavy parallelism adds coordination risk. Request it
only when independent work materially benefits from separate context or parallel
execution.

## Review

Before delivering the prompt, check that:

- each instruction changes behavior and is stated once;
- the user request remains the authority for task scope;
- no skill, template, or contextual pointer creates an unnecessary approval gate;
- completion is explicit enough to encourage follow-through without gold-plating;
- verification is proportionate and capable of detecting the relevant failure;
- referenced files, tools, and configuration fields exist;
- optional branches remain optional.

Deliver the prompt as text unless the user asked to place it in a file.
