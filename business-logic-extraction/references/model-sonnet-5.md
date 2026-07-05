# Claude Sonnet 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Sonnet 5.

## Fit

- Strong default for the coding and agentic work of a multi-batch extraction.
- More tool-eager and self-verifying than Sonnet 4.6; keep batch scope explicit so
  the eagerness does not pull in adjacent handlers.

## Extraction Prompting

- Adaptive thinking is on by default and effort defaults to `high`; use `xhigh`
  only for the hardest behavior-preservation slices. It respects effort strictly,
  so avoid `low` on batches where transitions or auth are subtle.
- It self-verifies and will keep searching. Define what evidence ends a batch:
  the focused characterization test plus the touched-surface ladder rungs pass,
  then stop rather than hunting for more coverage.
- Follow the Verification Ladder order (narrow characterization first, then wider
  regression and hygiene); do not let tool-eagerness jump straight to a wide check.
- Literal instruction following means scope must be explicit. Name the files and
  branches inside the current batch, and restate the Core Rule's keep-inline
  side, so it does not refactor mechanical adapter code you asked it to leave.
- The new tokenizer emits ~30% more tokens; leave `max_tokens` headroom so batch
  reports and diffs are not truncated mid-verification.
- Use prompt wording to set report tone, not temperature/top-p/top-k — the API
  rejects non-default values for those parameters with an HTTP 400 error.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Run at high (xhigh only for the hardest slice). A batch is done when its focused
characterization test and the touched-surface ladder rungs pass — stop then, do
not keep searching for more coverage. Extract only branches that decide business
meaning; leave mechanical adapter code inline.
```
