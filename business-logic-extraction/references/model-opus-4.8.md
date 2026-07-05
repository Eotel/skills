# Claude Opus 4.8 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Opus 4.8.

## Fit

- Strong at long-horizon extraction batches, code review, and debugging the
  behavior-preservation questions this skill raises.
- Opus 4.7 prompts transfer; scope discipline still needs to be stated because it
  follows instructions literally rather than generalizing.

## Extraction Prompting

- Opus respects effort strictly, so under-thinks moderately complex batches at
  low/medium. Use `xhigh` for the extraction and characterization work, `high`
  minimum when behavior preservation is subtle; reserve `low` for a single scoped
  verify command.
- It follows scope literally and will not silently generalize. Say the Review
  Checklist and Core Rule apply to every batch, not just the first, or later
  batches ship unchecked.
- It favors reasoning over tool calls. When you need proof, name the exact
  evidence: which characterization test and which ladder rungs must pass before it
  claims a batch is done.
- "Report only high-severity findings" is obeyed literally and drops recall. To
  surface risky-but-uncertain drift (auth weakening, contract change), ask it to
  report every concern including low-severity and uncertain ones.
- It spawns fewer subagents by default. If you want a fresh-context verifier for
  behavior preservation, say so explicitly; otherwise it verifies inline.
- Keep the Verification Ladder order as written; do not let its preference for
  reasoning skip the narrow characterization test in favor of a wide check.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Run at xhigh for extraction and characterization. The Core Rule, Verification
Ladder order, and Review Checklist apply to every batch, not just the first.
Before claiming a batch done, cite the focused test and ladder rungs you ran, and
report every behavior-preservation concern including low-severity and uncertain.
```
