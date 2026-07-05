# GPT-5.5 Guide

> Snapshot: 2026-07. Source:
> <https://developers.openai.com/api/docs/guides/prompt-guidance>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is GPT-5.5 (including the Codex harness).

## Fit

- Responds to short, outcome-first prompts and picks an efficient path; give it
  the destination and constraints, not a re-listed procedure.
- Biases to action with reasonable assumptions — good for this bootstrap, but
  pin down the exact-config and stop rules so it does not improvise.

## Execution Prompting

- Effort: medium is a good default here; re-evaluate before escalating, since
  5.5 reasoning is efficient on mechanical work.
- Give an outcome contract: the four config files created, the npm scripts
  added, and the smoke test returning `0` for both linters.
- Exact-config fidelity over improvisation: copy the configs verbatim. The
  `dir/**` ignore glob is load-bearing; a bare `dir/` silently fails. Do not
  refactor or "improve" the given configs.
- Keep `.remarkignore` and `.textlintignore` as one synced list.
- Stop rule: after the smoke test shows two `0` counts and warnings are
  summarized, the task is done — a non-zero count is a config bug to fix, and
  pre-existing prose warnings are the user's content, not yours to chase.
- Keep the both-tools default; warn before install (lockfile churn) and do not
  run `remark --output` to rewrite prose unless asked.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Outcome: four config files written, npm scripts added, smoke test 0 for both
linters. Copy the configs verbatim including every `dir/**` glob; keep both
ignore files in sync; install both tools. Stop once the smoke test is 0 and
warnings are summarized. A non-zero count is a config bug. Warn before install.
```
