# Refactor Instructions Template

Adapt the sections to the actual refactor. Do not manufacture content to fill an
irrelevant heading.

```markdown
# <Refactor title>

Date: <date>
Repository: <repo, branch, base commit>
Audience: implementation workers and independent verifiers

## Outcome

<Observable result and explicit non-goals.>

## Context

<Product and architecture facts the executor needs. Link authoritative docs
instead of copying them.>

## Behavior and contracts to preserve

- <behavior, mechanism, location, and existing proof>

## Boundaries

- May change: <paths and contracts>
- Proposal only: <security, schema, public API, generated, or product surfaces>
- Out of scope: <adjacent work>

## Baseline

<Canonical commands, expected results, and known pre-existing failures.>

## Work items

### <ID> <title>

- Evidence: <file:line, command output, or verified observation>
- Outcome: <what becomes true>
- Files: <expected ownership>
- Risk: <behavior or boundary at risk>
- Verification: <command/artifact and expected result>
- Decision: implement / investigate with bound / proposal only

## Phases

<Dependency-ordered groups with an exit criterion for each phase.>

## Final verification

<Checks for the integrated result, including negative diff assertions.>

## Reporting

<Required evidence, blocked items, decisions, and remaining work.>
```

Every claim that authorizes a change must be supported by repository evidence.
Questions replace guesses when behavior, ownership, or deletion cannot be proven.
