# Subagents And Session Continuity

Use current OpenAI documentation as the authority:
<https://developers.openai.com/codex/subagents>.

## When to delegate

Use subagents for independent work that benefits from parallel execution or
separate context, especially exploration, review lenses, test/log analysis, and
bounded implementation slices with disjoint ownership.

Prefer one agent when the work is small, sequential, write-heavy on overlapping
files, or cheaper to understand in one context. Delegation consumes more tokens
and adds coordination overhead.

## Prompt contract

Tell the parent agent:

- how to divide the work;
- which files or questions each worker owns;
- whether work is read-only or may edit;
- what evidence and summary each worker returns;
- when to wait, consolidate, or stop;
- who owns integration and external operations.

For concurrent repository writes, use `worker-contract` and explicit disjoint
file allowlists. A fresh critic should receive the diff and acceptance criteria,
not the author's reasoning.

## Custom agents

Project agents live under `.codex/agents/` and personal agents under
`~/.codex/agents/`. Current releases require `name`, `description`, and
`developer_instructions`; other configuration fields are version-sensitive.
Verify them against the current subagent and configuration documentation before
writing a file.

## Sessions

Resume or fork an existing session when continuity matters more than a blank
context. Confirm current commands with `codex --help` before embedding them in an
instruction. Do not ask an agent to rediscover or shell into prior sessions when
the launcher can select the session directly.

External transports such as agmsg have their own lifecycle and delivery rules.
Use the `agmsg` skill when the user explicitly selects that transport instead of
duplicating its manual here.
