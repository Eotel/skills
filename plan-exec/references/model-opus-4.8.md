# plan-exec — Claude Opus 4.8

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Opus 4.8 executing this skill. On
self-load, apply them as written. When delegating plan execution, paste the
Instructions block into the subagent prompt. They do not override SKILL.md.

## Instructions

- Apply every plan-wide rule to every step — update Progress at each stopping
  point and run the step's verification commands even where the plan states
  the rule only once.
- Run the named verification commands and read their output before marking a
  step done; do not infer a result from reasoning alone.
- Keep the diff inside the plan's stated scope: no unrequested docs,
  abstractions, or error handling.
- When a step proves harder than planned, record it in Surprises with evidence
  and adjust the plan file; do not silently re-plan in your head.
- Spawn subagents only where the plan says to; otherwise work in-session.
- The plan file's Progress section is the durable record — keep chat status
  updates brief and non-duplicative.

## Caller notes

- Effort: `high`–`xhigh` for cross-subsystem execution; at low/medium Opus 4.8
  scopes to exactly the written step and may under-think surprises.
- Thinking is off by default; enable adaptive thinking for plan execution.
