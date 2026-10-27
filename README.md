# Eotel Skills

Reusable agent skills packaged for APM. Each skill keeps selection metadata in a
short description, shared workflow decisions in `SKILL.md`, and conditional
detail in references or scripts.

## Install

For Eotel's shared Mac setup, declare the skill directories in
[`chezmoi-dotfiles/dot_apm/apm.yml`](https://github.com/Eotel/chezmoi-dotfiles/blob/main/dot_apm/apm.yml)
and follow its [APM recovery and propagation guide](https://github.com/Eotel/chezmoi-dotfiles/blob/main/docs/skills-pipeline.md).
The guard backs up the installation, then refreshes the shared skills and
rebuilds the deployment receipt. Install only the directories needed by the target
workflow. The Codex orchestration callers also require
`codex-orchestration-core`.

## Authoring

Authoring rules, script and test conventions, and validation commands live in
[`AGENTS.md`](AGENTS.md).

## Python checks

Python files are checked with Ruff's `ANN` and `PTH` rules and with ty. Run the
same checks as CI with `.githooks/pre-commit` after installing `uv`.

Install the reviewed hook into Git's directory for each worktree, then enable
that copy. A later branch checkout cannot change the installed hook:

```sh
hook_dir="$(git rev-parse --absolute-git-dir)/python-hooks"
mkdir -p "$hook_dir"
cp .githooks/pre-commit "$hook_dir/pre-commit"
chmod +x "$hook_dir/pre-commit"
git config extensions.worktreeConfig true
git config --worktree core.hooksPath "$hook_dir"
```

The file-specific exceptions in `ruff.toml` and `ty.toml` record existing
violations tracked in [issue #78](https://github.com/Eotel/skills/issues/78).
New Python files are checked without exceptions.

## Skills

- `adoption-triage`: classify retrospective improvements, record review items, and read adoption decisions.
- `agentic-docs`: create or reorganize agent-readable repository documentation.
- `apm-usage`: operate APM manifests, packages, targets, and lockfiles.
- `ast-grep-practice`: create structural lint rules and use the shared Node import guard.
  Eotel maintains the MIT-licensed copy from mizchi/skills.
- `business-logic-extraction`: move framework-neutral business decisions into
  named domain units.
- `class-sweep`: find sibling occurrences of a plausibly repeated defect class.
- `claude-cloud-dispatch`: send a self-contained task to a Claude Code cloud
  session (`claude --cloud`), prepare a repository's cloud environment, and
  bring the session's PR back reviewed.
- `devenv-init`: scaffold supported devenv.sh project environments.
- `diagnosing-bugs`: reproduce and repair hard bugs and performance regressions.
  Eotel maintains the MIT-licensed copy from mattpocock/skills.
- `django`: refactor Django ORM/query boundaries and DRF-owned policies.
- `markdown-lint-setup`: configure remark and textlint for repository Markdown.
- `natural-japanese`: review and revise human-facing Japanese word choices;
  flag expressions for contextual review with a Python scanner.
- `yomiyasu`: revise AI-written Japanese while preserving meaning and facts.
  This is the MIT-licensed `nanaism/yomiyasu` v2.4.0 skill at commit
  `ae258d7a57b335673e30b88a018ae08d4f72c449`. Its 42 files match that
  tag byte for byte. The `.codex-plugin/plugin.json` metadata is omitted because
  APM 0.35.0 treats it as a native Codex plugin and refuses the shared
  `agent-skills` target. `yomiyasu_check.py` stores copies of the original and
  final text in `.yomiyasu/` by default, with private file permissions and a
  local `.gitignore`.
- `orca-session-triage`: sweep Orca agent sessions for finished or stalled
  worktrees, waiting questions, and agents that never got a task; batch the
  answers and next steps, clear the done cards, and close the finished tabs
  and worktrees.
- `orca-row-classify`: class the rows of an `orca-session-triage` review queue
  in a fresh Sonnet context (invoked by `orca-session-triage`, not directly).
- `plan-exec`: create and execute durable repo-local implementation plans.
- `exec-plan-migration` (temporary): move a repository from the legacy
  `completed/` plan archive to forward-only plans. Requires `plan-exec` and
  `agentic-docs`. Remove it once the remaining repositories have migrated.
- `pr`: write a PR body in the template the repository calls for (its own, the
  review-first template for repositories of the account and its organizations, or the
  upstream template elsewhere) and check its headings.
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
