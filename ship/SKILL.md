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
   policy. Use `real-browser-verify` for changed UI behavior.
3. **Review the diff.** Remove accidental complexity, scope drift, dead code, and
   duplicated rules without cutting requested behavior.
4. **Prepare the change.** Update completed exec-plan state, stage explicit paths,
   create conventional commits from this agent's changes, and preserve unrelated
   worktree state.
5. **Open the PR.** Push, assign Eotel, include `Closes #N` for resolved issues,
   and describe the verification evidence.
6. **Clear remote gates.** Monitor required checks to green. Diagnose and fix
   failures caused by the change, then rerun the affected local check before
   pushing again.
7. **Resolve review.** Collect all current review comments, address valid findings,
   and respond with evidence when a finding does not apply. Recheck comments and
   CI after updates.

## Completion boundary

Shipping is complete when the requested outcome holds in the relevant
environment, required CI is green, and no required review comment remains
unresolved. Report blocked real-environment or production verification
separately; do not substitute a mock or weaker check.

Opening a PR is authorized by the ship request. Merge, close, production writes,
and destructive cleanup still require explicit user authorization.
