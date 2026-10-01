# Extraction workflow

1. Orient on the affected architecture.
   - Read applicable `AGENTS.md`, nearby tests, and only the boundary documents
     that govern the change.
   - Identify dependency boundaries before moving code.
   - Prefer local patterns over inventing a new layer.
2. Inventory candidates.
   - Search handlers/resolvers/components for branches with business terms:
     `permission`, `authorize`, `status`, `phase`, `mode`, `token`, `validate`,
     `publish`, `archive`, `transition`, `start`, `end`, `issue`, `refine`,
     `summar`, `approve`, `reject`.
   - Classify each candidate as adapter-only, named decision, query scope,
     authorization policy, lifecycle transition, side-effect orchestration, or frontend model/hook.
3. Choose a small batch.
   - Pick behavior with existing coverage or cheap characterization tests.
   - Avoid crossing several architecture boundaries in one edit.
   - Split large ORM/query-scope work into its own plan if it needs model/queryset design.
4. Characterize before or alongside extraction.
   - Add focused tests for denial, invalid input, invalid transition, idempotency,
     or error routing.
   - Use endpoint/e2e tests only when the extracted behavior depends on integration.
5. Extract to the owning layer.
   - Keep controllers/resolvers/components as adapters: parse, authorize narrow
     scope, call the named operation, adapt the result.
   - Preserve old branches inside the new helper before simplifying.
6. Verify the touched surface.
   - Run focused tests first, then lint/format/type/boundary checks.
   - If a full check has unrelated failures, name the unrelated blocker and keep
     proof tight for changed paths.
7. Update the execution record.
   - Keep the plan/status/current decisions current while working.
   - Record surprises, especially wrong assumptions about tools, package managers,
     test runners, or generated files.

## Verification Ladder

Run the narrowest proof first, then widen:

1. Focused characterization tests for the moved rule.
2. Existing endpoint/component/e2e regression that exercises the old path.
3. Lint and format for touched files.
4. Type checks for touched modules.
5. Architecture/dependency boundary checks.
6. Generated contract drift checks when APIs, schemas, or docs generators are affected.
7. Source hygiene: `git diff --check` and `git status --short`.

Avoid running DB-backed pytest commands in parallel unless the repo explicitly
supports isolated test databases per process.
