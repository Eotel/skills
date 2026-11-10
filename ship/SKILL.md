---
name: ship
description: Ship a change end to end through implementation, verification, PR, CI, and review resolution. Use when the user explicitly asks to ship.
---

# Ship

Carry the requested change to a review-ready, CI-green pull request. Use the
harness's durable progress mechanism for long work, but do not add a goal or
approval gate when the request is already authorized and can complete in one
turn.

## Pipeline

1. **Implement.** Follow an approved exec plan when present. Use TDD when the
   requested behavior or defect benefits from a regression test; do not create a
   test that only mirrors a low-impact mechanical edit.
2. **Verify.** Run focused checks capable of detecting the requested failure.
   Broaden for integration, shared-boundary refactors, or repository release
   policy. Use `real-browser-verify` for changed UI behavior. When the diff
   touches code that runs only inside a desktop app shell, also operate that app
   behind the user's windows with its desktop script.
3. **Review the diff.** Run `code-review`, then `simplify`, on the same
   [pre-commit review inputs](references/pre-commit-review.md). Address valid
   `code-review` findings before running `simplify`. Where `simplify` is not
   available (Codex), review that diff for reuse, simplification, efficiency, and
   altitude yourself and apply the fixes. Every requested behavior survives the
   cleanup; a cut it would need goes to an issue instead.
4. **Prepare the change.** Delete the completed exec plan after promoting
   durable decisions, put its verification results in the PR description, stage
   explicit paths, create conventional commits from this agent's changes, and
   preserve unrelated worktree state. From here on, the PR is the resume point.
5. **Open the PR.** Push, assign Eotel, include `Closes #N` for resolved issues,
   and describe the verification evidence.
6. **Clear remote gates.** Monitor required checks to green. Diagnose and fix
   failures caused by the change, then rerun the affected local check before
   pushing again.
7. **Resolve review.** When the PR is open, follow
   [review completion](references/review-completion.md).

## Completion boundary

Shipping is complete when the requested outcome holds in the relevant
environment, required CI is green, configured automated reviews have finished
on the current head, and no required review feedback remains unresolved.
Report blocked real-environment or production verification separately; do not
substitute a mock or weaker check.

Opening a PR is authorized by the ship request. Merge, close, production writes,
and destructive cleanup still require explicit user authorization.
