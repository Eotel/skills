# Plans

Last reviewed: YYYY-MM-DD

Create an exec plan under `docs/exec-plans/active/` for large or risky changes.
Small fixes can use the PR description or commit message.

## When To Create An Exec Plan

- Changes crossing multiple subsystems
- Database, schema, API, or contract changes
- Security, authorization, reliability, or migration work
- Multi-turn work that must be resumable without chat history
- Decisions that future contributors will need to audit

## Lifecycle

1. Create `docs/exec-plans/active/{yyyy-mm-dd}-{slug}.md`.
2. Update progress, surprises, decisions, and unresolved questions while working.
3. Record verification results before completion.
4. Move the plan to `docs/exec-plans/completed/`.
5. Promote durable decisions into design docs, product specs, ADRs, tests, or lint rules.

Template: [exec-plans/plan-template.md](exec-plans/plan-template.md)
