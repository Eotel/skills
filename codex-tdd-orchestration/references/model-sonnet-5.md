# Claude Sonnet 5 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/ja/build-with-claude/prompt-engineering/prompting-claude-sonnet-5>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Sonnet 5.

## Fit

- Good default for coding and agentic tasks.
- Stronger and more tool-eager than earlier Sonnet models; keep the roadmap and
  scope boundaries explicit so the extra eagerness does not expand the task.

## Orchestration Prompting

- Start from `high` effort for normal orchestration; use `xhigh` for the hardest
  coding or agentic slices. Use lower effort only for narrow, low-risk checks.
- Adaptive thinking is on by default. If latency matters, narrow the objective
  and acceptance checks rather than adding more process text.
- Sonnet 5 tends to use tools and self-verify. Tell it which evidence is enough
  to exit a milestone so it does not keep searching.
- Literal instruction following is useful but requires scope to be explicit:
  say whether a constraint applies only to one topic or all topics.
- Do not rely on temperature/top-p/top-k for style variation; use prompt wording
  for tone and output shape.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Use Claude Sonnet 5 behavior intentionally: follow the roadmap and scope exactly,
use tools to verify the assigned slice, and stop when the listed acceptance
checks pass. Do not expand the task after successful verification.
```
