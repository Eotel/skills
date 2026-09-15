---
name: codex-orchestrator-brief
description: Create a repository-backed handoff package for autonomous multi-wave implementation. Use when the deliverable is a refactor spec plus an implementer/verifier orchestration prompt.
---

# Codex Orchestrator Brief

Load `codex-orchestration-core` first. Produce a handoff package that another
agent can execute without relying on this conversation:

1. `refactor-instructions.md`: evidence-backed scope, constraints, phases, and
   verification.
2. When needed, pre-implementation questions in chat for decisions the
   repository cannot resolve.
3. `codex-goal-prompt.md`: the orchestration objective, worker/critic roles,
   executable acceptance contract, retry limits, and integration boundary.

This skill creates the package; `codex-prompting` creates or revises an individual
prompt, while `codex-tdd-orchestration` runs a live multi-agent implementation.

## Gather evidence

Start with applicable repository instructions and the current diff. Inspect only
the architecture, entry points, tests, CI, generated surfaces, and deployment
details that can affect this handoff. Do not force backend/frontend/infra lenses
that the repository does not have.

Verify every load-bearing claim directly before putting it in the package:

- file paths and ownership boundaries;
- behavior or contract that must remain stable;
- debt evidence and blast radius;
- commands used as acceptance gates;
- generated, security-sensitive, migration, or external-state surfaces.

An unresolved claim becomes a question or an investigation task, not an asserted
fact.

## Write the spec

Read [references/refactor-instructions-template.md](references/refactor-instructions-template.md)
for the detailed shape. Adapt it to the task rather than filling every optional
section mechanically.

Each implementation item needs evidence, a clear outcome, affected paths,
constraints, and a check capable of detecting failure. Mark items that require a
product or security decision as proposal-only. Prove deletion candidates are
unused before authorizing removal.

Order phases by dependency and blast radius. A later phase must not be a hidden
prerequisite for an earlier one.

## Ask only material questions

Ask when the code and existing documentation cannot determine a choice that
changes scope, public behavior, security, stored data, or external state. For each
question, give the competing evidence, blocked items, and the safe default. Do
not turn ordinary implementation choices into approval gates.

## Write the orchestration prompt

Read [references/orchestrator-prompt-template.md](references/orchestrator-prompt-template.md).
Name the target harness's actual delegation and messaging mechanisms only after
confirming they exist.

The prompt must preserve:

- a main orchestrator that does not implement worker-owned code;
- explicit worker ownership and a cold independent critic;
- an executable acceptance contract and bounded retries;
- verification of actual diffs and command output;
- serialized integration and protected-path checks;
- a durable, verifiable completion condition.

Include environment facts that materially affect execution. Do not paste a
catalog of historical failures or generic coding advice into every worker prompt.

## Completion

Re-read the spec and prompt together as a blank-slate executor. Every referenced
file and command must exist, the acceptance contract must match the planned
outputs, and proposal-only surfaces must remain fenced. Deliver the two files,
the material questions, and one concise invocation for the target runner.
