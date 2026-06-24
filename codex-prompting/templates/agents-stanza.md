<!--
agents-stanza.md — a block for AGENTS.md (repo root or a subdirectory). Codex
auto-discovers and injects AGENTS.md per directory and adheres closely to it, so
put DURABLE, repo-wide rules here instead of repeating them in every task prompt.
Deeper-directory AGENTS.md files override shallower ones. Keep it tight — this is
injected into every Codex turn in this tree.
-->

# AGENTS.md — [project name]

## What this project is
[1–3 sentences: stack, entry points, what it does.]

## Conventions (conform to these)
- [Naming / formatting / module layout rules Codex must follow]
- [Preferred helpers/abstractions to reuse instead of reinventing]
- [Error handling: e.g. "no broad try/catch or silent defaults"]

## Forbidden / be careful
- [Files, dirs, or patterns NOT to touch or generate]
- [Security/compliance constraints invisible in the code]

## Verification (required gates)
- Build/typecheck: `[command]`
- Lint/format: `[command]`
- Tests: `[command]`  ← run targeted tests for changed behavior; all green before done

## Model & effort (if standardized)
- Default model: `[id from references/models.md]` at `medium` effort; escalate to
  `high`/`xhigh` only for [hard cases].

## Subagents (if used)
- Predefined agents in `.codex/agents/`: `[name]` = [role], …
- For multi-part tasks: spawn one subagent per [unit], wait for all, consolidate.
  (Codex won't fan out unless a task prompt asks it to.)
