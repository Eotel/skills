# Eotel Skills

Reusable agent skills packaged for APM.

## Install

```bash
apm install -g Eotel/skills/agentic-docs --target claude
apm install -g Eotel/skills/devenv-init  --target codex
```

Codex can use shared Claude skills through a symlink from
`~/.codex/skills/<name>` to `~/.claude/skills/<name>`.

## Model-specific prompt guides

The best prompt differs per execution model. Larger behavior-driving skills
therefore keep model-neutral rules in `SKILL.md` and ship per-model files as
`references/model-{opus-4.8,sonnet-5,fable-5,gpt-5.5}.md` — load at most one,
only when the execution model is known. Two forms exist, chosen by who reads
the file:

- **Self-execution skills** (`plan-exec`, `business-logic-extraction`,
  `apm-usage`, `markdown-lint-setup`): the reader is the executing model, so
  each guide is the model-optimized prompt itself — an `## Instructions`
  block to apply directly (or paste into a subagent prompt when delegating)
  plus `## Caller notes` for caller-side knobs (effort, sampling, token
  limits). No self-description; description costs tokens without steering.
- **Prompt-authoring skills** (`codex-tdd-orchestration`,
  `codex-orchestrator-brief`): the reader composes prompts for *another*
  model, so guides stay descriptive (Fit, tendencies) with a pasteable
  Prompt Patch.

Guides never override a skill's core rules. Small single-reading skills
intentionally ship no model guides.

## Skills

- **`agentic-docs`** — bootstrap, audit, and maintain repository-local
  documentation systems for agentic software work.
- **`business-logic-extraction`** — plan, execute, and verify refactors that
  move hidden business decisions out of adapters into named services, policies,
  query helpers, lifecycle helpers, hooks, or route-local models.
- **`django`** — apply Django ORM/query placement guidance, including
  model-owned QuerySet extraction and focused verification.
- **`devenv-init`** — scaffold a per-language devenv.sh project from
  [Eotel/devenv-templates](https://github.com/Eotel/devenv-templates) with
  toggleable features (direnv, delta, treefmt, git-hooks, postgres, mysql,
  redis, lsp, strict-types) and Python version patching.
- **`repo-local-git-hooks`** — detect and repair global `core.hooksPath`
  overrides that bypass repository-local pre-commit or pre-push hooks.
- **`plan-exec`** — write repo-local execution plans under `docs/exec-plans/active/`, get approval, and keep progress updated through implementation.
- **`markdown-lint-setup`** — bootstrap `remark` + `textlint` (with
  `@textlint-ja/textlint-rule-preset-ai-writing`) in a Node project; always
  excludes AI agent context dirs (`.agents`, `.claude`, `.codex`,
  `.impeccable`, `.serena`, `.github`) from both linters.

- **`codex-prompting`** — author or tune a prompt aimed at Codex itself,
  accounting for its harness quirks (AGENTS.md injection, `apply_patch`, the
  planning tool, preamble cadence) and GPT-5.x prompting principles. Ships
  per-model reference notes (`references/models.md`) and fill-in templates
  (task prompt, system prompt, AGENTS.md stanza, subagent TOML), and teaches
  Codex the capabilities it ignores by default — spawning subagents and
  resuming/forking sessions, and using **agmsg** to spawn named codex/claude-code
  peers and send them a goal prompt. Complements the orchestration bundle below:
  this writes *what to say to codex*; those run *the loop*.

### Codex orchestration bundle

Three skills that share one orchestration-loop philosophy (orchestrator edits no
code; cold author/critic split; executable rubric; critic-PASS-only gate; `/goal`
anchor; verified memory log; smallest-blast-radius waves; sandbox/env pitfalls).
The shared invariants live in **one** skill so the philosophy can't drift between
the two callers. **Install all three together** — APM installs skills individually
and does not resolve the dependency, so a caller installed without the core skill
cannot load it.

- **`codex-orchestration-core`** — the shared orchestration-loop invariants.
  A dependency loaded (via the Skill tool) by the two skills below; not a
  standalone task skill.
- **`codex-orchestrator-brief`** — analyze a repo and author a written handoff
  package (`refactor-instructions.md` + a `/goal` orchestrator prompt +
  pre-implementation questions) for *another* model to run autonomously.
- **`codex-tdd-orchestration`** — Claude drives codex sessions *live* in this
  session: per topic, implementer → reviewer → fixer, topics in parallel, with
  TDD enforcement and orchestrator-side verification.
