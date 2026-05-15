---
name: django
description: Use when working in Django projects on ORM/query placement, custom QuerySet or Manager design, extracting repeated filters/select_related/prefetch_related/orderings from views, GraphQL resolvers, API actions, services, tasks, or query modules, preserving behavior with tests, and verifying Django code with project-local lint/type/boundary checks. Trigger for Django best practices, django-stubs/mypy ORM typing, queryset-scope refactors, model-owned table scopes, read-model query boundaries, and avoiding misplaced business/query logic.
---

# Django

Use this skill as a compact router for Django best practices. Load the relevant
reference file before changing code; keep project-specific architecture rules
from the target repository authoritative.

## Operating Rules

1. Orient on the project first.
   - Read `AGENTS.md`, architecture docs, Django settings/test layout, model
     placement rules, and nearby tests.
   - Prefer established local model/query/service patterns over generic advice.
2. Classify the change before editing.
   - Is it table-owned ORM scope, cross-model read-model composition,
     authorization, mutation/write transition, serialization, or framework
     adapter glue?
   - Move only the part that has a stable owner.
3. Preserve observable behavior.
   - Keep ordering, pagination windows, null handling, access checks, and error
     routing compatible unless the bug and intended contract change are recorded.
4. Verify with the narrowest meaningful proof, then widen.
   - Run focused tests for touched behavior.
   - Run project-local Django checks such as `ruff`, `ruff format --check`,
     `mypy`, import-boundary checks, schema/contract checks, and source hygiene
     when they exist.
   - If full checks have unrelated failures, report them separately and keep
     evidence for the changed path tight.

## Reference Map

- `references/orm-queryset-extraction.md`: use when extracting repeated Django
  ORM query logic into model-owned custom QuerySets or deciding what should
  stay in query/service/API layers.

