---
name: business-logic-extraction
description: Use when refactoring large controllers, request handlers, GraphQL resolvers, Action API endpoints, services, or frontend components so business decisions are extracted into named policies, services, query helpers, lifecycle helpers, hooks, or route-local models. Trigger when the user asks to extract business logic, thin adapters, split giant handlers, move validation/authorization/state transitions/side-effect orchestration out of entrypoints, or execute a multi-batch refactor plan with verification.
---

# Business Logic Extraction

Use this skill to turn hidden business decisions into named, testable homes
without changing observable behavior. The goal is not smaller files by itself;
the goal is entrypoints that read like adapters.

## Core Rule

Extract a branch when it decides business meaning:

- authorization or scoped access
- validation, normalization, defaulting, or backend error mapping that must stay consistent
- state/lifecycle transitions
- token/public access decisions
- side-effect routing, dispatch, persistence, or idempotency
- named actions such as publish, archive, issue, transition, start, end, approve, reject, refine, summarize, seed, or reset
- frontend route/phase/mode/action-availability decisions that are more than render toggles

Keep inline when the code is mechanical adaptation:

- DTO field copying
- framework boilerplate
- simple null/empty guards
- one-off rendering branches
- local formatting with no cross-call consistency requirement

## Workflow

1. Orient on the repo's architecture first.
   - Read `AGENTS.md`, architecture docs, boundary docs, package scripts, and nearby tests.
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

## Placement Guide

Backend:

- Authorization branching belongs in access helpers or policies that still call
  the canonical access service.
- Reusable table scopes belong on querysets or query modules, not endpoint code.
- State transitions belong in service/usecase/lifecycle helpers with invalid
  transition tests.
- Side-effectful actions should usually become a command/result service or usecase.
- Vertical-specific behavior stays in the vertical. Move to core/common only
  when the concept is truly platform-wide and boundary checks allow it.

Frontend:

- Route-specific wizard/phase/mode/error-target rules can live in a route-local
  `model`, `domain`, or `use*` helper.
- Shared user workflows can move to shared hooks or domain helpers.
- Keep components focused on render structure, event wiring, accessibility
  attributes, and state binding.
- Pure helpers deserve unit tests only when the repo has a declared runner for
  that style; otherwise rely on existing component/e2e coverage and record the
  test-runner gap.

## Plan And Goal Shape

For non-trivial extractions, create or update an execution plan that includes:

- purpose and observable non-goals
- architecture constraints
- inventory/scoring criteria
- batch plan with rollback/idempotence notes
- verification commands grouped by backend, frontend, architecture, and hygiene
- progress, surprises, decisions, outcomes, and follow-up opportunities

Keep the active goal concrete: "execute this plan and verify implementation
before reporting completion." Mark it complete only after code, docs, and
verification are done.

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

## Review Checklist

Before finishing, confirm:

- The entrypoint got thinner in meaning, not just in line count.
- The new helper name says the business decision it owns.
- Authorization did not move behind weaker checks.
- API/GraphQL/UI contracts did not drift unless intentionally recorded.
- Error messages and field-routing behavior stayed compatible.
- Tests cover the risky branch, not only the happy path.
- Any removed setup/tooling file is actually unreferenced by the current system.
