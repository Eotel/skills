<!--
codex-instruction.md — fill-in TASK PROMPT for a single Codex session.
Paste the filled result into an interactive Codex session, or as the prompt to
`codex exec "..."`. Delete bracketed guidance and any block you don't need.

Before filling: pick model + effort from references/models.md. Keep it OUTCOME-first
(say the goal, not the steps) — see references/gpt5-prompting.md. Standing repo
rules belong in AGENTS.md (templates/agents-stanza.md), not here.
-->

# Task: [one-line outcome, e.g. "Add idempotency keys to the payments webhook handler"]

## Context
[2–4 sentences: where this lives, why it matters, what's currently true.
Point to AGENTS.md for house rules instead of re-pasting them:
"Follow the conventions in AGENTS.md."]

## Goal
[The user-visible outcome. What is true when this is done — described as a result,
not a procedure. Let Codex choose the steps.]

## Success criteria (definition of done)
- [Observable behavior 1]
- [Tests: "targeted unit tests for the changed behavior, and they pass"]
- [Build/lint: "`<project build/lint/typecheck command>` is green for affected packages"]

## Constraints
- [Hard limits: files/areas NOT to touch, APIs to keep stable, perf/security limits]
- [Use real invariants only as ALWAYS/NEVER; everything else as "prefer X when Y".]

## Out of scope
- [What NOT to build — prevents gold-plating / adjacent-feature creep]

<!-- OPTIONAL: parallelization. Codex will NOT fan out unless told to.
     See references/subagents-and-sessions.md. -->
## Parallel work (optional)
[e.g. "Spawn one subagent per affected module to map call sites; wait for all,
then implement from the consolidated map." Name predefined agents if any.]

<!-- OPTIONAL: only if default tone is wrong for the context -->
## Reporting
[e.g. "Terse — this runs unattended via `codex exec`; minimal commentary." OR
"Pairing tone, short preambles before each step."]

## Stop / verification
[When are you done? "When success criteria pass. If blocked on <X>, stop and ask
rather than guessing." State what verification counts as proof.]
