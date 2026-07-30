# markdown-lint-setup — GPT-5.6

> Snapshot: 2026-07. Sources:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>,
> <https://learn.chatgpt.com/docs/models>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for GPT-5.6 (`sol` / `terra` / `luna`,
including the Codex harness) executing this skill. On self-load, apply them
as written. When delegating, paste the Instructions block into the subagent
prompt. They do not override SKILL.md's configs or steps.

## Instructions

- Outcome contract: the four config files written, the npm scripts added,
  the smoke test returning `0` for both linters, and pre-existing warning
  counts summarized. Stop when it holds.
- Copy the configs verbatim — every `dir/**` glob intact, both ignore files
  identical, both tools installed. Do not improvise or improve the configs.
- A non-zero smoke-test count is a config bug; fix it before reporting.
- Warn before installing (lockfile churn); do not rewrite the user's prose
  (`remark --output`) unless asked.

## Caller notes

- Reasoning: `low` — the work is mechanical; re-evaluate before escalating.
- Model: `gpt-5.6-luna` or `gpt-5.6-terra` suffices; `sol` is not needed.
