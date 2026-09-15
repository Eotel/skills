# Prompt Design For Current Codex Models

Current high-capability models need less procedural scaffolding and follow loaded
instructions more literally. Revisit accumulated rules instead of adding another
model-specific overlay.

Primary references:

- <https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra>
- <https://developers.openai.com/api/docs/guides/prompt-guidance>

## Outcome and completion

Lead with the user-visible result and the evidence that marks it complete. For
long work, explicitly ask the model to continue through implementation,
inspection, and repair until that evidence holds.

Treat a request for action as authorization for its ordinary reversible steps.
Ask only when a missing choice changes the result, scope, external state, or risk.
Prepare any safe, reviewable work before requesting final approval for an
external or irreversible action.

## Boundaries

Separate three kinds of instruction:

- **Invariants:** safety, permissions, protected contracts, and destructive
  operations. These may use absolute language.
- **Decision rules:** preferred behavior under stated conditions. These should
  preserve room for judgment.
- **Examples:** illustrations, not hidden requirements.

Remove contradictory rules and prohibitions that merely describe an unwanted
behavior. State the intended behavior directly where possible.

## Context

Do not require the model to read a fixed stack of documents before every task.
Point to each document with the condition that makes it relevant. Keep cheap
facts in the repository and put only non-obvious conventions in the prompt.

Skill descriptions are selection metadata. Keep them short, distinguish nearby
skills by their output or workflow boundary, and avoid trigger dictionaries that
match unrelated work.

## Verification

The model already tends to test coding changes thoroughly. Name mandatory checks
only when repository policy or the risk requires them. For other work, require
checks capable of detecting the changed behavior and let the agent choose the
narrowest useful proof.

Broaden or repeat verification after failures, new changes, shared-boundary
refactors, release preparation, or unresolved risk. Do not add tests that merely
mirror a reversible low-impact implementation.

## Delegation

When delegation is wanted, state what can run independently, the ownership
boundary, whether the parent must wait, and the result shape. Avoid a standing
instruction to delegate every task: subagents add cost and parallel writers add
coordination risk.

## Style

Specify only meaningful output deltas. Current models may favor detailed Markdown;
ask for concise paragraphs and limited structure when that better fits the user.
Keep personality separate from collaboration and authority rules.
