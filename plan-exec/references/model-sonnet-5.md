# plan-exec — Claude Sonnet 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Sonnet 5 executing this skill. On
self-load, apply them as written. When delegating plan execution, paste the
Instructions block into the subagent prompt. They do not override SKILL.md.

## Instructions

- A step is done when its listed verification commands pass: mark it in the
  plan file and move on. Do not re-verify earlier steps or keep searching for
  extra evidence past the listed checks.
- Stay inside each step's file scope; route anything outside it through a plan
  edit before touching the code.
- Apply plan-wide rules (Progress updates, verification, scope discipline) to
  every step, not only the step where they are written.
- The plan file's Progress section is the durable record; do not duplicate it
  with mechanical status messages.
- Write the plan and reports in the tone/format the plan template specifies —
  formatting comes from the text you were given, nothing else.

## Caller notes

- Effort: `high` default; `xhigh` only for the hardest step.
- Non-default temperature/top_p/top_k return HTTP 400; steer style via prompt
  text.
- Leave `max_tokens` headroom — the tokenizer emits ~30% more tokens than
  Sonnet 4.6 for the same text.
