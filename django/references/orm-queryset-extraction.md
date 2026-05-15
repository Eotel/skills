# ORM QuerySet Extraction

Use this reference when a Django codebase repeats table-level ORM logic in
views, GraphQL resolvers, API actions, services, tasks, or query modules.

## Goal

Move reusable Django ORM table scopes into the owning model's custom QuerySet
or the repository's equivalent query-scope layer.

Good outcomes:

- Callers use named, chainable scopes such as `active()`, `for_user(...)`,
  `for_project(...)`, `with_project()`, `with_account_relations()`,
  `for_access_token(...)`, or `ordered_recent()`.
- Entry points stop spelling the same `filter(...)`, `select_related(...)`,
  `prefetch_related(...)`, ordering, and candidate-selection logic inline.
- Query/service modules still own read-model assembly, pagination, DTO
  construction, and cross-model composition.

Non-goals:

- Do not ban all direct ORM calls outside models.
- Do not hide API response shaping or request-specific behavior in managers.
- Do not move write-side state transitions, bulk updates, or command boundary
  fetches merely to reduce line count.
- Do not introduce migrations or schema changes for a source-only refactor.

## Classification

Extract to a QuerySet when the logic is:

- repeated for one model across multiple callers
- a stable table-owned scope such as active/visible/submitted/public/current
- a relation-loading shape for one model such as `with_project()` or
  `with_profile_and_account()`
- a reusable lookup such as `for_slug(...)`, `for_access_token(...)`,
  `for_project_id(...)`, or `for_user(...)`
- a reusable ordering or existence candidate set
- chainable with other scopes

Keep outside the model QuerySet when the logic is:

- one-off boundary resolution such as a mutation `get(pk=...)`
- command/write-side transition logic
- bulk update/delete behavior with side effects or state-machine meaning
- pagination, limit windows, cursor construction, or DTO construction
- cross-model read-model assembly, aggregation, deduplication, or response
  shaping
- framework-specific GraphQL, REST, admin, serializer, or request vocabulary
- a low-repeat lookup whose manager method would make the model API noisier

If only part of a query is table-owned, extract that base scope and keep the
composition in the query/service module.

## Implementation Pattern

Follow the nearest existing project pattern first. When the project has no
pattern, use a custom `QuerySet` exposed through `as_manager()`.

```python
from __future__ import annotations

from django.db import models


class ProjectQuerySet(models.QuerySet["Project"]):
    def active(self) -> "ProjectQuerySet":
        return self.filter(is_active=True)

    def for_tenant_slug(self, slug: str) -> "ProjectQuerySet":
        return self.filter(tenant__slug=slug)

    def with_tenant(self) -> "ProjectQuerySet":
        return self.select_related("tenant")

    def ordered_recent(self) -> "ProjectQuerySet":
        return self.order_by("-created_at", "id")


class Project(models.Model):
    tenant = models.ForeignKey("Tenant", on_delete=models.CASCADE)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ProjectQuerySet.as_manager()
```

Callers should read in domain terms:

```python
projects = (
    Project.objects.active()
    .for_tenant_slug(slug)
    .with_tenant()
    .ordered_recent()
)
```

Naming guidance:

- Prefer model/domain vocabulary: `for_project`, `for_user`, `submitted`,
  `with_application`, `ordered_latest`.
- Avoid adapter vocabulary: `for_graphql_card`, `for_rest_response`,
  `for_page_payload`, `for_admin_screen`.
- Use `with_*` for relation loading, `for_*` for filters by owner/key/token,
  and `ordered_*` for stable orderings.
- Keep methods small and chainable. Return a QuerySet, not a list, DTO, page,
  or serialized shape.

## Workflow

1. Inventory direct ORM logic.

   ```bash
   rg -n "objects\\.(filter|get|select_related|prefetch_related)|\\.select_related\\(|\\.prefetch_related\\(|\\.exists\\(|\\.first\\(|\\.update\\(" .
   rg -n "class .*QuerySet|as_manager\\(" .
   ```

2. Classify every candidate.
   - Model-owned reusable scope
   - One-off boundary lookup
   - Cross-model read-model composition
   - Authorization/access policy
   - Write-side transition or bulk update
   - Framework adapter/serializer concern

3. Choose a small batch.
   - Start with high-repeat, low-contract-risk scopes.
   - Prefer areas with existing focused tests.
   - Avoid changing several app boundaries in one batch.

4. Characterize before risky rewrites.
   - Add or identify tests for ordering, relation loading assumptions, public
     token access, permission boundaries, missing rows, and null cases.
   - For mechanical replacements with strong existing coverage, focused
     regression tests may be enough.

5. Add QuerySet methods on the owning model.
   - Keep the method body equivalent to the old inline query.
   - Preserve ordering and relation loading exactly unless recording an
     intentional bug fix.
   - Keep annotations and aggregates out of the model layer unless they are a
     stable table-owned concept.

6. Rewrite callers.
   - Replace repeated inline scopes with named methods.
   - Keep read-model assembly in query/service modules.
   - Keep mutation and command boundary fetches inline unless a repeated scope
     emerges.

7. Verify and record.
   - Run focused tests first.
   - Run format, lint, type, import-boundary, architecture, and schema checks
     that the project treats as authoritative.
   - Record intentionally remaining direct ORM calls so future agents do not
     "clean them up" into worse abstractions.

## Typing And django-stubs

- Match the project's existing Django typing style.
- Annotate custom QuerySet methods with the concrete QuerySet class when that
  is what nearby code does.
- Be careful with helpers that erase custom QuerySet types. Filter helpers,
  access-control helpers, and `apply_filterset(...)` often return a generic
  `QuerySet[Model]`; call custom scope methods before those helpers or narrow
  with a local annotation/cast when necessary.
- Avoid pushing request-specific annotations into the model QuerySet just to
  reuse a line of code. Complex `annotate(...)` calls can leak annotated model
  types through django-stubs and make unrelated callers harder to type-check.

## Verification Ladder

Use the project's commands, but keep this order:

1. Focused tests for touched query behavior.
2. Existing endpoint, resolver, task, or service tests that exercise callers.
3. `ruff check` and `ruff format --check` for touched Python projects.
4. `mypy` when the project uses django-stubs or plugin-heavy typing.
5. Import-boundary checks such as `tach`, `import-linter`, or repo architecture
   linters.
6. GraphQL/OpenAPI/schema drift checks when API surfaces are affected.
7. `git diff --check` and `git status --short`.

Avoid parallel DB-backed test runs unless the project explicitly supports
isolated databases per process.

## Acceptance Checklist

- Repeated table-owned scopes have named QuerySet methods.
- Methods are chainable and avoid request/API/DTO vocabulary.
- Callers are thinner in meaning, not merely shorter.
- Direct ORM calls that remain have a reason: boundary lookup, write-side
  transition, aggregate composition, reverse-manager operation, or low-repeat
  lookup.
- Focused tests prove behavior that could have drifted.
- Type and boundary checks pass, or unrelated failures are reported with
  evidence.

