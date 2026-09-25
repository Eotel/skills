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
3. When every remaining step is done and verified, promote durable decisions
   into design docs, product specs, ADRs, tests, or lint rules.
4. Delete the plan in the change that completes the work. Record the result and
   verification in the PR description, or in that commit's body when there is
   no PR. After the PR opens, the PR is the resume point for CI and review work.
5. Git history keeps the last committed version of the plan for reviewers.

When the project vendors `check_plans.py` from the agentic-docs skill, run it in
CI or an agent stop hook.

Template: [exec-plans/plan-template.md](exec-plans/plan-template.md)
