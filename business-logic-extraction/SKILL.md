---
name: business-logic-extraction
description: Extract business decisions from handlers or components into named, testable domain units. Use when a handler, resolver, or component carries authorization, validation, lifecycle, or side-effect decisions that belong behind a named operation.
---

# Business Logic Extraction

Use this skill to turn hidden business decisions into named, testable homes
without changing observable behavior. The goal is not smaller files by itself;
the goal is entrypoints that read like adapters.

> **Finding the branches at scale:** to locate the decision points below across a
> big handler or many files, prefer structural search over text grep — invoke the
> **`ast-grep`** skill to match by AST shape, and to apply mechanical codemods once
> a pattern is confirmed. Text search misses overloads, reformatting, and aliases.

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

## Where

- `references/workflow.md`: orienting, inventorying candidates, choosing a
  batch, characterizing, extracting, and the verification ladder. Read before
  the first extraction.
- `references/placement.md`: which backend or frontend layer owns an extracted
  rule. Read when choosing where a decision goes.

## Done

- The entrypoint got thinner in meaning, not just in line count.
- The new helper name says the business decision it owns.
- Authorization did not move behind weaker checks.
- API/GraphQL/UI contracts did not drift unless intentionally recorded.
- Error messages and field-routing behavior stayed compatible.
- Tests cover the risky branch, not only the happy path.
- Any removed setup/tooling file is actually unreferenced by the current system.
