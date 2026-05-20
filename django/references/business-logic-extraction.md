# Business Logic Extraction (Django)

Django-specific placement guidance for extracting business decisions out of
DRF viewsets, serializers, and permission classes into named, testable units
on the owning model.

For the generic methodology (when to extract, batch sizing, verification
ladder, review checklist), use the **business-logic-extraction** skill.
This reference only adds the Django-flavored delta.

## When this reference applies

The user wants to refactor branches whose meaning is hidden behind:

- Implicit `ordinal == 0`, `status == X`, `freezed`, role/`view.action`
  comparisons in viewset actions, permission classes, or serializer mixins.
- Duplicated guard blocks across sibling viewsets (e.g. `DefApprover` and
  `Approver` insert/replace/remove).
- DRF permission classes whose `has_object_permission` is a long
  `view.action`-keyed `if/elif` chain that mixes roster construction with
  status gates.
- Serializer `validate(...)` methods that combine field validation with
  authorization, lifecycle transition, or deputy/proxy resolution.

If the refactor is about repeated `objects.filter(...).select_related(...)`
shapes instead, switch to `orm-queryset-extraction.md`.

## Placement Map

Use the most specific owner that already exists in the project.

| Decision lives on... | When |
|---|---|
| Abstract model (`defs.py`) | Predicate reads only fields declared on the abstract class (e.g. `is_applicant_slot` from `ordinal`); shared between `DefX` and `X` siblings. |
| Concrete model (`models.py`) | Needs relations (FK, GenericForeignKey, reverse manager) or other concrete-only fields. |
| Model classmethod / staticmethod | Predicate over a payload value before any instance exists (e.g. `is_applicant_slot_ordinal(ordinal: int)` for insert pre-checks). |
| Instance method on concrete model | Bundled transition that owns a `transaction.atomic()` block (e.g. `remove_from_chain` = delete + reorder). |
| Service module (`services.py` or `usecases/`) | Crosses multiple aggregates, calls external systems, or composes several models' transitions. Only create when no existing model is a natural owner. |
| Serializer mixin | Field-level validation rule reused across serializers (e.g. `ApprovalStatusMixin.check_approval_status`). Keep the mixin generic; do not push DRF concepts into the model. |
| Permission class | Only adapter glue: read `request.user` / `view.action`, delegate to `obj.can_user_*()` predicates. No business rule inside. |
| ViewSet action | Adapter: parse → predicate guard → named operation → response. Owns the HTTP error envelope, not the rule. |

Do not put `view.action` strings, `request.user`, or HTTP status codes into
model methods. Pass already-resolved primitives (user, deputies set) so the
model layer stays testable without DRF.

## Naming Conventions

- `is_*` — read-only boolean state predicate (`is_applicant_slot`,
  `is_last_in_chain`, `is_open`, `is_ready_for_action`).
- `can_user_*(user, ...)` — authorization predicate; takes the acting user
  plus any precomputed sets (deputies, peers) the model cannot derive itself.
- `*_roster()` — returns a `set[User]` of authorized users for one action;
  named after the action (`metadata_editor_roster`, `action_user_roster`).
  Keep `exec`/`act`/`approve` rosters separate when they differ.
- `is_*_slot` / `is_*_slot_ordinal(value)` — table-position predicates; the
  `_ordinal` variant is the static/classmethod form used by payload checks.
- Transition verbs as method names: `remove_from_chain`, `skip_before`,
  `trigger_signal`, `freeze`, `withdraw`. The verb captures the business
  intent; the implementation chains the primitive ORM calls.
- Avoid adapter vocabulary in model methods: no `for_drf_*`, `for_request_*`,
  `for_action_X`.

## DRF Adapter Patterns

**ViewSet action template** after extraction:

```python
@decorators.action(methods=["delete"], detail=True)
def remove(self, request, pk=None):
    instance = self.get_object()
    if instance.is_applicant_slot:
        return _bad_request("ordinal", ERR_REMOVE_APPLICANT_SLOT)
    if instance.freezed:
        return _bad_request("freezed", ERR_REMOVE_FREEZED)
    instance.remove_from_chain()
    return response.Response(status=status.HTTP_204_NO_CONTENT)
```

- Error message strings stay byte-identical to pre-refactor when preserving
  API contract. Hoist them to module-level `ERR_*` constants so the viewset
  and tests reference the same source.
- A small `_bad_request(field, message)` helper removes the 4-line
  `Response({...}, status=...)` boilerplate without hiding the field-routing.

**Permission class template** after extraction:

```python
class ApproverPermission(DjangoObjectPermissions, DeputyUserMixin):
    METADATA_EDIT_ACTIONS = ("partial_update", "replace")
    ACT_ACTIONS = ("exec",)
    DELETE_ACTIONS = ("destroy", "remove")

    def has_object_permission(self, request, view, obj):
        if view.action in self.METADATA_EDIT_ACTIONS:
            deputies = self.get_deputy_set(obj.creator, obj.approval)
            return obj.can_user_edit_metadata(request.user, deputies)
        if view.action in self.ACT_ACTIONS:
            return obj.can_user_act(request.user)
        if view.action in self.DELETE_ACTIONS:
            deputies = self.get_deputy_set(obj.creator, obj.approval)
            return obj.can_user_remove(request.user, deputies)
        return super().has_object_permission(request, view, obj)
```

- The permission class owns only the `view.action` → predicate dispatch.
- Pre-resolve dependencies the model cannot reach on its own (deputies,
  external scopes) and pass them in.
- Preserve pre-existing per-action quirks. For example, if `replace` checked
  freezed at the viewset (returning 400) while `remove` checked it in
  permissions (returning 403), do not "normalize" them in the refactor.
  Status-code drift is a separate, recorded behavior change.

## Tests

- Predicates that read only declared fields can be tested **without**
  `@pytest.mark.django_db`:

  ```python
  class TestIsApplicantSlotPure:
      def test_true_for_zero(self):
          assert models.Approver(ordinal=0).is_applicant_slot is True
  ```

  Use a plain class (no `TestCase`); construct the instance with field
  kwargs and read the property. No `.save()`, no DB.

- pytest only collects classes whose name starts with `Test*`. A class
  named `*TestCase` that does **not** inherit from `unittest.TestCase`
  will be silently skipped. Either inherit from `TestCase` (DB-backed) or
  rename to `Test*` (pure).

- Predicates that traverse relations (`approval.rosters`,
  `approver_set.last()`) need `@pytest.mark.django_db` and the minimum set
  of related rows. Build them via existing factories rather than full
  end-to-end setup.

- Roster predicates: cover (a) member, (b) non-member, (c) deputy member,
  (d) status-gate denial. These are the four risky branches.

- Lifecycle methods (`remove_from_chain`, `freeze`, ...): assert both the
  state of `self` and the side effect on siblings (compaction, ordinal
  values). One test per direction.

## Verification

Run in this order; stop widening once a stage fails:

1. `uv run pytest src/<app>/tests/test_<new_predicates>.py` — the new
   focused tests for the extracted predicates.
2. `uv run pytest src/<app>/tests/<existing endpoint tests>` — the legacy
   tests that exercise the refactored adapter path; status codes and error
   message strings must match.
3. `uv run pytest` — full suite to catch ripple effects in signals,
   migrations, and GraphQL surfaces.
4. `uv run ruff check <touched>` and `uv run ruff format --check <touched>`.
5. `mypy` / `ty` only on touched modules when the project runs them in CI.
   Pre-existing django-stubs FK-narrowing warnings on unrelated lines are
   not blockers; report them separately.

## Common Pitfalls

- **Leaking DRF into the model.** If the new predicate needs `request` or
  `view.action`, the rule does not belong on the model — keep it in the
  permission class or serializer.
- **Combining `can_user_*` predicates too eagerly.** It is fine to have
  three predicates whose logic overlaps (`can_user_edit_metadata`,
  `can_user_remove`) when the difference is a single business gate
  (`freezed`). Express the second in terms of the first plus the gate;
  do not collapse them, or you lose a test boundary.
- **Hidden inconsistencies frozen by tests.** Before "fixing" a permission
  class that returns 400 in one action and 403 in another, search tests for
  the specific status codes; treat the inconsistency as locked-in contract
  unless the user asks for a behavior change.
- **Mass-renaming `freezed` and similar typo-fields.** They are real
  database columns; a refactor must not change column names without a
  migration plan.
- **Touching abstract `defs.py` casually.** Adding a property is safe;
  adding a field requires migrations across all hosts that inherit it.

## Worked Example

A realistic batch of extractions for a DRF viewset that had duplicated
`insert`/`replace`/`remove` actions across two sibling models:

- Add `is_applicant_slot` (property) and `is_applicant_slot_ordinal`
  (staticmethod) on the abstract approver base. Pure-function tests, no DB.
- Add `remove_from_chain()` on the concrete model. Bundles `delete()` and
  `reorder_after_delete()` under one `transaction.atomic()`. DB test.
- Add roster + `can_user_*` predicates on the concrete model. Pass deputies
  as a precomputed argument so the model stays DRF-free.
- Rewrite the viewset action as a 6-line adapter; hoist error strings to
  `ERR_*` constants; add `_bad_request(field, message)` helper.
- Rewrite the permission class as a `view.action` → predicate dispatch.
- Add a `test_<app>_policies.py` file with: 6 pure tests for slot
  predicates, 14 DB tests for chain operations and roster/policy
  predicates.
- Verify with focused tests → legacy endpoint tests → full suite → ruff.
  Confirm status codes and error message strings are byte-identical.
