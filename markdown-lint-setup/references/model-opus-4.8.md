# Claude Opus 4.8 Guide

> Snapshot: 2026-07. Source:
> <https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-4-8>.
> Re-verify before relying on a version-specific claim.

Use this only when the execution model is Claude Opus 4.8.

## Fit

- Long-horizon agentic and knowledge work; this bootstrap is mechanical, so
  scope tightly rather than reasoning the setup open-ended.
- Follows instructions literally and does not silently generalize across scope,
  which suits copying the SKILL configs verbatim.

## Execution Prompting

- Effort: medium is enough for this mechanical setup; go high only if the repo's
  package-manager or workspace layout is genuinely ambiguous.
- Opus reads scope literally, so state that the ignore list applies to BOTH
  `.remarkignore` and `.textlintignore` — otherwise it may sync only the file
  it is currently editing and leave the two to drift.
- Write the config files exactly as given. The `dir/**` glob is load-bearing:
  a bare `dir/` silently fails, so keep the `**` form and do not "tidy" it.
- Because effort is respected strictly at the low end, name the evidence
  required before claiming done: run the smoke test and read its output.
- Treat any non-zero smoke-test count as a config bug to fix, not a result to
  report around. Do not skip the smoke test.
- Keep the both-tools default (remark + textlint) unless the user opts out;
  do not drop one on your own reasoning.

## Prompt Patch

Add this block to model-specific agent prompts only when useful:

```text
Copy the SKILL config files verbatim, including every `dir/**` ignore glob, and
apply the same ignore list to BOTH .remarkignore and .textlintignore. Install
both remark and textlint unless told otherwise. Run the smoke test and treat a
non-zero count as a config bug to fix before reporting done.
```
