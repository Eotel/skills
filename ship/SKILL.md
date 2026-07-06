---
name: ship
description: Standard end-to-end shipping pipeline for a change — implement (TDD) → verify the fix in the real environment → simplify → open a PR → watch CI to green → handle every review comment, then declare done only when re-shipping is a no-op. Use when the user runs /ship or asks to ship / 出荷 / 仕上げて出す a change end-to-end. Runs in both Claude Code and Codex; each step names the skill to use where one exists and gives an agent-neutral fallback where it does not.
---

# ship

Run the standard shipping pipeline below. Before starting, set the whole pipeline
— termination condition: further work is a no-op and the change can ship — as a
**session-scoped completion gate**:

- **Claude Code**: set a verifiable end-state with `/goal` and let the Stop hook
  gate completion; watch CI with the Monitor tool.
- **Codex**: use the goal mechanism if available; otherwise track all 7 steps as
  an explicit TODO/checklist and do not stop until every step is done; watch CI
  with `gh run watch` / `gh pr checks`.

## Arguments

Interpret `$ARGUMENTS`:

- Contains `codex` → delegate implementation to Codex via the
  **codex-tdd-orchestration** skill and stay in the orchestrator/verifier role.
  (If the driver is already Codex, implement using that skill's
  implementer → reviewer → fixer split.)
- Contains `oracle` → after opening the PR, review it with the **oracle** skill
  and fix findings until none remain.
- Anything else → treat as the implementation target (issue URL / plan file /
  feature description).

## Pipeline

1. **Implement** — use the **tdd** skill: red → green → refactor, vertical
   slices. If a plan is given (`docs/exec-plans/active/*.md`), follow it to
   completion.
2. **Verify in the real environment** — do not stop at green tests. Re-confirm
   yourself that the originally-reported symptom / requested behavior actually
   holds in the real environment (real DB / real browser / real run). For UI,
   diff a screenshot of the whole target area against the mock. Use the
   **real-browser-verify** skill where it applies.
3. **Simplify (max)** — review the diff and apply reuse / simplification /
   efficiency cleanups. **Never cut scope (what is built)**; surface any cut
   candidate as an issue instead.
   - Claude: run the `simplify` skill at max.
   - Codex: no such skill — refactor the diff yourself against the same criteria.
4. **Open a PR** — dedicated worktree, explicit staging (no `git add -A`),
   conventional commit (lowercase subject). If it resolves an issue, put
   `Closes #N` in the body. Assign to Eotel.
5. **Watch CI** — after push, watch CI until green; if red, fix and re-push
   yourself.
   - Claude: Monitor tool (or the `ci-monitor` skill).
   - Codex: poll `gh run watch` / `gh pr checks` without blocking sleeps.
6. **Handle review** — collect every review comment (oracle / Copilot / human)
   and triage each claim against the source in one pass (valid → fix, invalid →
   rebut with evidence). Do not dribble fixes across rounds.
   - Claude: use the `fix-review` skill / flow.
   - Codex: fetch all comments with `gh pr view <N> --comments` and
     `gh api repos/<owner>/<repo>/pulls/<N>/comments`, then address every one.
7. **Declare done** — only after confirming from ground truth that further work
   is a no-op: the **original symptom is gone**, CI is green, and zero review
   comments are unaddressed. Green tests / green CI alone are not the completion
   condition.
