---
name: codex-prompting
description: Author or tune a prompt/instruction aimed at OpenAI Codex (the CLI/agent powered by GPT-5.x-codex), accounting for Codex's specific harness quirks. Assembles a Codex-directed prompt from per-model reference notes plus fill-in templates, and — critically — teaches Codex to use capabilities it does NOT know about by default (spawning subagents, resuming/forking other sessions). Use when the user wants to "write a prompt for codex", "codex への指示を作る/組み立てる", "codex 向けプロンプト", "codex に渡すプロンプト", tune phrasing for codex, pick a codex model + reasoning effort, or get codex to use subagents/sessions. NOT for designing a full autonomous multi-wave handoff package from repo analysis (that is codex-orchestrator-brief), NOT for Claude driving codex sessions live as orchestrator (that is codex-tdd-orchestration), and NOT for optimizing a generic non-codex prompt (that is prompt-optimizer).
---

# codex-prompting

Codex is not a generic chat model with a code mode; it is a GPT-5.x model that has
been **post-trained for a specific agentic harness** (the Codex CLI / Codex cloud).
It has strong baked-in priors about how to plan, edit files, run tools, write
preambles, and finish a task. A prompt that ignores those priors fights the model;
a prompt that aligns with them gets a senior-engineer-grade result. This skill is
the knowledge base for writing **prompts aimed at Codex** plus templates to assemble
them.

## What this produces

A finished Codex-directed instruction — one of:

- A **task prompt** you paste into a Codex session (or `codex exec "..."`).
- A **developer/system prompt** that customizes Codex's behavior for a project.
- An **AGENTS.md** stanza that Codex auto-injects per directory.
- A **custom subagent** definition (`.codex/agents/*.toml`) when the task should
  fan out into parallel Codex workers.

**Delivery contract:** this skill produces these artifacts **as text to hand to
the user** — it does not write `AGENTS.md`, `.codex/agents/*.toml`, or any file
into the user's repo unless the user explicitly asks you to place it. Default to
emitting the content in chat; only create files on disk on explicit request.

## How to use this skill

1. **Pin the target model and effort.** Codex's lineup moves fast and per-model
   advice differs. Read `references/models.md`, confirm the current id with the
   user (or the live docs), and choose a reasoning effort. Default to the
   recommended interactive setting unless the task is genuinely hard. **If you
   cannot confirm the id** (no user to ask and no live-docs access this turn),
   pin the current default from `references/models.md` and **flag it in the
   deliverable as an unverified assumption** ("model id per the YYYY-MM snapshot
   — re-verify before pinning in config") rather than blocking or guessing.
2. **Load only the references the task needs** (progressive disclosure):
   - `references/codex-harness.md` — always. Codex's harness-level priors:
     planning tool, `apply_patch`, `rg`, AGENTS.md injection, preamble cadence,
     sandbox/approval, "don't make me re-derive the conventions".
   - `references/gpt5-prompting.md` — when phrasing or refactoring **any** codex
     prompt (its outcome-first / contradiction rules apply to task prompts too),
     especially a system/developer prompt, or when the model over/under-acts.
     GPT-5.x prompting principles (eagerness, verbosity, outcome-first,
     instruction contradictions, metaprompting).
   - `references/subagents-and-sessions.md` — when the task is parallelizable or
     should reuse/resume other Codex work. **The must-teach-explicitly feature.**
     Also covers **agmsg**, an external transport for spawning named codex/
     claude-code peers and sending them a goal prompt.
3. **Fill a template** from `templates/` rather than writing from scratch.
4. **Run the pre-send checklist** (below) against the draft.
5. Hand the finished prompt to the user, or — if the user wants it executed —
   note that running codex is `codex-tdd-orchestration`'s job, not this skill's.

## The non-obvious rule that motivates this skill

> **Codex does not advertise its own orchestration powers.** By default a Codex
> session will not spawn subagents, and will not go looking for other sessions to
> resume or fork — even though the harness fully supports it. Per the docs,
> *"Codex only spawns a new agent when you explicitly ask it to do so."* If you
> want Codex to parallelize across subagents, or to pick up and instruct an
> existing conversation, **the prompt has to say so explicitly and name the
> mechanism.** This is the single highest-leverage thing most people omit.

See `references/subagents-and-sessions.md` for the exact config and phrasings.

## Pre-send checklist

Before handing over a Codex prompt, verify:

- [ ] **Model + effort named** and appropriate for difficulty (`models.md`).
- [ ] **Outcome stated, not micro-process.** GPT-5.x is post-trained to plan its
      own steps; over-specifying the procedure degrades it. State the goal,
      success criteria, and hard constraints — let it choose the path.
- [ ] **No absolute rules that aren't true invariants.** `ALWAYS`/`NEVER`/`must`
      should be reserved for real invariants; everything else is a decision rule.
      Contradictory instructions waste reasoning — Codex tries to honor all of them.
- [ ] **Conventions are pointed to, not omitted.** If the repo has AGENTS.md, say
      so; if not, state the conventions inline. Codex conforms hard to whatever
      it's told the house style is.
- [ ] **Stop condition is explicit.** When is the task done? What verification
      counts (tests, build, lint)? Codex is biased to persist end-to-end; tell it
      what "done" means so it neither stops early nor gold-plates.
- [ ] **Subagents/sessions teaching is present IF wanted.** If the task should
      fan out or resume prior work, the prompt explicitly names the mechanism
      (otherwise Codex won't use it).
- [ ] **Preamble/verbosity expectation set** if the default tone is wrong for the
      context (terse automation vs. collaborative pairing).
- [ ] **Sandbox facts and standing guardrails included** when the prompt targets
      a sandboxed/delegated session: inject the "Standing dispatch preamble"
      (ENVIRONMENT FACTS / GUARDRAILS / FINAL REPORT schema) and, for implementer
      work, the author upfront prohibitions — both live canonically in
      **codex-orchestration-core**. Without them, every session re-pays a 2–5-turn
      environment-rediscovery tax and reviewers re-catch the same mechanical
      defect classes.
- [ ] **Verification method is specified, not just demanded.** "Verify your work"
      lets Codex pick the cheapest green signal (measured: 69 false-completion
      incidents). Name the exact commands, the artifact to paste (command output,
      screenshot), and — for UI/bug work — real authenticated browser operation
      of the target screen as the bar.

## Scope boundaries

- This skill writes the **content** of codex-directed prompts. It does not run
  codex and does not design a multi-wave autonomous loop.
- For a full repo-analysis-driven **handoff package** (refactor-instructions.md +
  a `/goal` orchestrator prompt with implementer/verifier waves), use
  **codex-orchestrator-brief** — it can pull phrasing from this skill's references.
- For **Claude itself driving live codex sessions** as orchestrator, use
  **codex-tdd-orchestration**.
- For **non-codex / generic** prompt optimization, use **prompt-optimizer**.

## Keeping the references fresh

The Codex model lineup and harness change frequently. The references are
point-in-time snapshots (dated inside each file) distilled from two primary
sources — keep them as the editable source of truth and re-verify before relying
on a version-specific claim:

- Codex prompting guide: <https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide>
- GPT-5.x prompt guidance: <https://developers.openai.com/api/docs/guides/prompt-guidance>
- Codex models / subagents / CLI: <https://developers.openai.com/codex/models>, `/codex/subagents`, `/codex/cli/reference>`
