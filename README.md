# Eotel Skills

Reusable agent skills packaged for APM. Each skill keeps selection metadata in a
short description, shared workflow decisions in `SKILL.md`, and conditional
detail in references or scripts.

## Install

Check `apm install --help` for the installed APM release. A typical shared install
is:

```bash
apm install -g Eotel/skills/agentic-docs --target agent-skills
```

Install only the skill directories needed by the target workflow. The Codex
orchestration callers also require `codex-orchestration-core`.

## Authoring

Authoring rules, script and test conventions, and validation commands live in
[`AGENTS.md`](AGENTS.md).

## Skills

- `agentic-docs`: create or reorganize agent-readable repository documentation.
- `apm-usage`: operate APM manifests, packages, targets, and lockfiles.
- `business-logic-extraction`: move framework-neutral business decisions into
  named domain units.
- `class-sweep`: find sibling occurrences of a plausibly repeated defect class.
- `devenv-init`: scaffold supported devenv.sh project environments.
- `django`: refactor Django ORM/query boundaries and DRF-owned policies.
- `markdown-lint-setup`: configure remark and textlint for repository Markdown.
- `orca-session-triage`: sweep Orca agent sessions for finished or stalled
  worktrees, waiting questions, and agents that never got a task; batch the
  answers and next steps, clear the done cards, and close the finished tabs
  and worktrees.
- `plan-exec`: create and execute durable repo-local implementation plans.
- `exec-plan-migration` (temporary): move a repository from the legacy
  `completed/` plan archive to forward-only plans. Requires `plan-exec` and
  `agentic-docs`. Remove it once the remaining repositories have migrated.
- `real-browser-verify`: verify changed UI behavior in an authenticated browser.
- `repo-local-git-hooks`: repair repository hooks bypassed by global Git config.
- `review-response`: handle reviews that came back on many PRs and drive each PR
  to its end state. Uses `codex-orchestration-core` for implementation work.
- `ship`: carry a change through implementation, PR, CI, and review resolution.

### Codex prompts and orchestration

- `codex-prompting`: write a task prompt, system/developer prompt, `AGENTS.md`
  instruction, or custom-agent definition.
- `codex-orchestration-core`: shared safety and acceptance contracts for the two
  orchestration workflows below.
- `codex-orchestrator-brief`: create a repository-backed implementation spec and
  multi-wave handoff prompt for another agent to run.
- `codex-tdd-orchestration`: run live multi-agent Codex implementation with
  isolated ownership, TDD, cold review, and integration gates.
- `worker-contract`: define file ownership and reporting for concurrent writers.
