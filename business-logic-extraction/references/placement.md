# Placement guide

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
