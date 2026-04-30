# Agentic Docs Structure

Use this reference when bootstrapping a docs system or changing the shape of an
existing `docs/` directory.

## Entry Points

Keep entry points small.

- `AGENTS.md`: agent instructions and pointers only.
- `ARCHITECTURE.md`: repository map and architecture entry points.
- `docs/index.md`: system of record for detailed docs.

## Knowledge Placement

Place knowledge by durability.

| Knowledge type | Default location |
|---|---|
| Stable architecture boundary | `docs/design-docs/` |
| User-facing behavior | `docs/product-specs/` |
| Multi-step work state | `docs/exec-plans/active/` |
| Completed implementation history | `docs/exec-plans/completed/` |
| Generated contract snapshot | `docs/generated/` |
| External framework reference | `docs/references/` |
| Broad unresolved debt | `docs/exec-plans/tech-debt-tracker.md` |

Do not create a new top-level doc category unless the existing categories fail
to answer where future agents should look.

## Minimal Bootstrap

For an empty or weakly documented project, start with this shape and remove
anything that is not useful:

```text
AGENTS.md
ARCHITECTURE.md
docs/
  index.md
  design-docs/
    index.md
    core-beliefs.md
  product-specs/
    index.md
  exec-plans/
    active/
    completed/
    plan-template.md
    tech-debt-tracker.md
  generated/
    README.md
  references/
    README.md
```

## Connection Rules

- Update `docs/index.md` for every durable docs category.
- Update `docs/design-docs/index.md` for design docs.
- Update `ARCHITECTURE.md` only for repository-level entry points.
- Add `Last reviewed: YYYY-MM-DD` when the repo already uses freshness markers
  or when introducing that convention.

## Mechanical Enforcement

Documentation explains intent. Checks detect drift.

When adding a durable rule, identify how it is enforced:

- tests for behavioral contracts
- schema or generated snapshots for API contracts
- type checks for data shape and module boundary assumptions
- lint or ast-grep rules for architecture and import constraints
- textlint or markdownlint for prose conventions
- scripts for repeatable repository maintenance

If enforcement is not practical, say that the rule remains a judgment call.

## Quality Bar

A good agentic docs system is small, linked, and falsifiable.

- Small: entry points are maps, not manuals.
- Linked: every durable doc is reachable from an index.
- Falsifiable: important rules identify their verification path.
- Resumable: active plans preserve progress, surprises, decisions, and
  verification results.
- Local: agents do not need private chat history or external memory to proceed.
