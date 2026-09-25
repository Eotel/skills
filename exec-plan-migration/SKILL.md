---
name: exec-plan-migration
description: Temporary. Migrate a repository from legacy exec plans (a completed/ archive, Progress or Outcomes logs) to forward-only plans. Use only when explicitly asked to migrate exec plans.
---

# Exec Plan Migration

Move one repository to the forward-only lifecycle in `plan-exec`: active plans
hold current state and next steps, and completed plans are deleted with their
results recorded in the PR. This skill requires `plan-exec` and `agentic-docs`
(`scripts/check_plans.py`). Delete this skill once the repositories that still
use the legacy shape have migrated.

## Inventory

Work in a dedicated worktree from the default branch. Run:

```bash
python <skill>/scripts/migrate_plans.py --root . --base origin/<default-branch>
```

The report lists the completed plans, every tracked line that references them
(code, Dockerfiles, and config included), each active plan with its last commit
date and its references, and unmerged branches that touch plan files. Only
tracked files are inventoried; untracked plans belong to in-flight sessions, so
leave them alone.

## Migrate

1. **Retire legacy enforcement.** Find what requires the old lifecycle: agent
   rules, `AGENTS.md`/`CLAUDE.md`, the plan template, `PLANS.md`, audit scripts,
   CI jobs, pre-commit hooks, and lint config that expect `completed/`. Replace
   the rules with the `plan-exec` lifecycle and remove the checks, including
   their tests. Keep checks that still hold, such as filename format.
2. **Resolve references to completed plans.** Read only the referenced completed
   plans; a reference is the signal that something still depends on them. Move
   any knowledge still needed into its durable home (ADR, design doc, test, or
   the referring comment itself), then repoint or remove the reference. Never
   leave a path to a deleted plan. Unreferenced completed plans go to git
   history unread.
3. **Triage active plans.** Classify each one from evidence in code, git log,
   and issue or PR state, not from its checkboxes:
   - done: promote durable knowledge, then delete;
   - abandoned or superseded: delete, and open an issue only for real remaining
     work;
   - live: rewrite into the current plan template.

   Leave a live plan in the legacy shape when an unmerged branch touches it; the
   `plan-exec` resume rule converts it when that work continues. Resolve
   references to deleted active plans the same way as in step 2.
4. **Delete completed plans.** Run the script with `--apply`. It refuses while
   references remain and otherwise removes the plans with `git rm`.
5. **Verify.** Run `check_plans.py`, then the repository's docs lint, link
   check, and tests touched by removed tooling. Adopt `check_plans.py` in CI
   only when it passes, or when the remaining failures are live plans deferred
   in step 3.

## Deliver

Open one PR per repository. In the description, list each deleted active plan
with its classification and evidence, each rewritten reference, the retired
checks, and the unmerged branches that will hit modify/delete conflicts on
deleted plans.
