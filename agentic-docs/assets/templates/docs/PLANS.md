# Plans

Last reviewed: YYYY-MM-DD

Create an exec plan under `docs/exec-plans/active/` for large or risky changes.
Small fixes can use the PR description or commit message.

## When To Create An Exec Plan

- Changes crossing multiple subsystems
- Database, schema, API, or contract changes
- Security, authorization, reliability, or migration work
- Multi-turn work that must be resumable without chat history

## Lifecycle

1. Create `docs/exec-plans/active/{yyyy-mm-dd}-{slug}.md`.
2. While working, keep the plan forward-looking: rewrite current facts, delete
   finished steps, and update `Now And Next` before stopping.
3. Record verification results in the PR description, not in the plan.
4. Delete the plan in the change that completes the work. Git history keeps the
   last version for reviewers.
5. Promote durable decisions into design docs, product specs, ADRs, tests, or
   lint rules before deleting the plan.

When the project vendors `check_plans.py` from the agentic-docs skill, run it in
CI or an agent stop hook.

Template: [exec-plans/plan-template.md](exec-plans/plan-template.md)
