---
name: django
description: Refactor Django ORM/query boundaries and DRF-owned business logic. Use for QuerySet or Manager extraction, adapter thinning, and Django-specific policy placement.
---

# Django

Use this skill as a compact router for Django best practices. Load the relevant
reference file before changing code; keep project-specific architecture rules
from the target repository authoritative.

> **Locating patterns:** when finding repeated filters/`select_related` chains or
> status/role decision branches across many views, serializers, or permissions,
> use the **`ast-grep`** skill for AST-shape search (and codemods) rather than text
> grep — it catches call variants, kwargs reordering, and aliased imports.

## Operating Rules

1. Orient on the affected Django surface.
   - Read applicable `AGENTS.md`, nearby tests, and the settings or architecture
     docs that govern the change; do not load unrelated project documentation.
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
- `references/business-logic-extraction.md`: use when extracting business
  decisions (status/ordinal/role/freezed branches, `view.action`-keyed
  permission dispatch, deputy/proxy resolution, lifecycle transitions) out
  of DRF viewsets, serializers, or permission classes into named predicates,
  rosters, and transition methods on the owning model. Use the generic
  **business-logic-extraction** skill instead when Django-specific placement is
  not the deciding concern.
