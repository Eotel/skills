# business-logic-extraction — Claude Sonnet 5

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Direct operating instructions for Claude Sonnet 5 executing this skill. On
self-load, apply them as written. When delegating a batch, paste the
Instructions block into the subagent prompt. They do not override SKILL.md.

## Instructions

- A batch ends when its focused characterization test and the touched-surface
  ladder rungs pass. Stop there — do not keep hunting for more coverage or
  re-verify earlier batches.
- Run the Verification Ladder in order: narrow characterization proof first,
  then wider regression and hygiene. Do not jump straight to a wide check.
- Work only on the files and branches named for the current batch. Extract
  only business-meaning branches per the Core Rule; leave mechanical adapter
  code inline even when it looks improvable.
- Report in the tone and sections the batch prompt specifies.

## Caller notes

- Effort: `high` default; `xhigh` for the hardest behavior-preservation
  slices; avoid `low` when transitions or auth are subtle.
- Non-default temperature/top_p/top_k return HTTP 400; steer style via prompt
  text.
- Leave `max_tokens` headroom — the tokenizer emits ~30% more tokens than
  Sonnet 4.6.
