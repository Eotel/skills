<!-- Keep this file short: it is loaded for every task under its directory. -->

# Project rules

## Context pointers

- Read `[architecture doc]` when changing service or package boundaries.
- Read `[schema doc]` when changing stored data or migrations.
- Read `[deployment doc]` when preparing or validating a deployment.

## Invariants

- [Protected contracts, files, or security rules that apply throughout this tree.]
- [Repository convention the code alone does not reveal.]

## Verification

- Use `[canonical command or task runner]` for repository checks.
- Run checks capable of detecting failure in the changed behavior. Broaden after
  failures, shared-boundary changes, or release preparation.

## Authority

- Carry authorized, reversible local work through to completion.
- Ask before [repository-specific destructive or external actions].
