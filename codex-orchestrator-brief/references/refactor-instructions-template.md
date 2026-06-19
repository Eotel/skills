# refactor-instructions.md — Template and Section Guidance

Eleven sections, in this order. Per-section notes explain what makes the section
load-bearing for an implementer that cannot ask questions. Write in the repo's
documentation language; keep this structure.

```markdown
# refactor-instructions.md

作成日 / Date: <date>
対象リポジトリ / Target: <org/repo> (<branch>, HEAD `<short-sha>`)
読者 / Audience: 実装担当モデル。この文書に書かれた範囲だけを、書かれた順序で実施すること。
```

Pinning the HEAD sha matters: the implementer can detect that the repo moved under
it and re-validate instead of acting on stale line numbers.

## 1. Objective

Numbered list of concrete outcomes. Always include an anti-goal sentence:
"見た目を綺麗にすることは目的ではない" / each phase independently mergeable /
big design changes are proposals only. This is the implementer's tiebreaker when
two readings of an instruction conflict.

## 2. Project Understanding

What the implementer must know to not break things it never looked at:

- What the product is (one paragraph, link the authoritative docs — don't restate them).
- Component map with **entry points** (binaries, ASGI/WSGI, route composition roots,
  schema composition roots) and the layering rule + the tool that enforces it.
- Main data flow in one paragraph (user input → ... → output).
- Canonical task runner and the search-exclusion rules from the repo's own docs.
- End with: "推測で補完しないこと" — read the linked doc when unsure.

## 3. Behaviors To Preserve

Numbered list (aim for 8–15) of behaviors that must survive every phase. Each entry
names the mechanism AND its location: not "keep auth working" but "all Action API
endpoints call `require_permission()` at handler top; 401/403 envelope shape is
fixed". Include: deterministic business logic that must not move to LLMs, security
invariants (path traversal guards, HMAC/signing, redaction), API/event contracts and
the test files that pin them, compat shims with their locations, idempotency
semantics, isolation hardening flags, and a blanket "no migrations in this scope"
if true.

## 4. Non-Negotiables

Working-style constraints, not product constraints: check `git status --short`
first; never mix in pre-existing uncommitted changes; record baseline results before
any edit; small revertible units; no drive-by formatting or opportunistic fixes
(found bugs are reported, not fixed); regression test required before any bug fix;
commit message convention; doc-lint commands to run when docs are touched.

## 5. Stop And Ask Conditions

Numbered, concrete triggers (typically ~7) under which the implementer halts that
item, records a question, and moves on if possible: public API / schema / stored
data impact; test-vs-implementation contradictions; unprovable deletion candidates;
security boundary contact; ambiguous acceptance state of an artifact it's about to
move; can't tell whether a failing test means "test was coupled to implementation"
or "I changed behavior"; infra/runtime config changes appearing necessary.

## 6. Baseline Commands

The exact canonical commands, in a copy-pasteable block, with a note on which are
required gates vs. advisory (and where that's documented). State explicitly that
pre-existing baseline failures are not the implementer's to fix but must be
reported. Note environment-dependent suites (Docker, browsers) as run-if-available.

## 7. Debt Map

The heart. One subsection per item, `D<n> <verdict> <title>`, verdict legend at top:

```
✅ = 本指示書で実装する / 🔍 = 調査して最小修正のみ / 🛑 = 提案のみ(実装禁止)
```

Each item, in order:

- **根拠 (Evidence)**: file:line, command output, quoted doc text. Verified by you,
  not just by a subagent.
- **なぜ負債か (Why it's debt)**: one or two sentences, tied to a repo rule or a
  concrete failure mode — never "it's ugly".
- **影響範囲 / リスク (Blast radius / risk)**.
- **改善案 (Remedy)**: for ✅, concrete enough to execute (target filenames, what
  to re-export, what NOT to touch); for 🛑, what the proposal should cover.
- **検証 (Verification)**: the specific commands/tests that prove behavior held.
- For 🛑 items, also state *why* implementation is forbidden (security boundary,
  needs ADR/exec-plan, managed-debt tracker says don't, product decision pending).

Order items so the phase mapping in §8 reads naturally (docs → tests → mechanical →
structural → proposals).

## 8. Implementation Phases

Map debt items to phases, smallest blast radius first:

- Phase 0 — Baseline: run §6, record results, branch discipline.
- Phase 1 — Docs/comment-only fixes (zero behavior change).
- Phase 2 — Safety nets: behavior tests for untested areas Phase 3+ will touch.
  Production code untouched.
- Phase 3 — Convention restoration / mechanical moves (one unit = one commit).
- Phase 4 — Structural splits, slice by slice, with re-exports preserving import
  and patch paths. Spell out the slice order and the "don't split further if the
  target is met" stop rule.
- Phase 5 — Proposals only (the 🛑 items), as report sections, not as plan files.

Add: phase N must pass verification before N+1 starts; if context/time runs out,
stop at a phase boundary, never mid-phase.

## 9. Verification Requirements

Re-run everything from §6 after all phases; zero regressions vs. baseline. List the
repo-specific tripwires as commands: generated-docs diff must be empty, migration
check must report no changes, coverage gates must hold. Note which suites are
environment-gated and that their skip must be reported.

## 10. Reporting Format

Required final-report contents: baseline results; per-phase summary (commits,
files, verification output tails); skipped/stopped items with evidence; unknown
problems found-but-not-fixed; the proposal sections for 🛑 items (each with
purpose / scope / steps / verification / rollback); final gate re-run vs. baseline.

## 11. Out-of-scope Items

Explicit negative list: schema/migrations, public contracts, infra changes,
permission/security model changes, managed debt items tracked elsewhere, excluded
directories, dependency additions, and anything awaiting an answer from the
pre-implementation questions ("回答があるまで触らない" with the item named).
