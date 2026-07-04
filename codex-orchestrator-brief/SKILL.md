---
name: codex-orchestrator-brief
description: Analyze a repository and produce a complete handoff package for an implementation model (Codex, Opus, etc.) that will run autonomously — an evidence-based refactor-instructions.md spec, a short list of pre-implementation questions, and a /goal-based orchestrator prompt with implementer/verifier subsessions, waves, a checkable rubric, PR merge policy, and a cross-wave memory log. Use whenever the user wants to hand work off to codex or another model via written instructions — phrases like "codex に渡す指示書", "リファクタ指示書を作って", "orchestrator 用のプロンプト", "/goal で完遂させたい", "実装担当モデルへの handoff", "wave で実行させたい", or asks to design implementer/verifier loops. Do NOT use when the user wants Claude to drive codex sessions directly itself (that is codex-tdd-orchestration), wants the refactoring done in this session, or wants an existing prompt/system-prompt improved or optimized (that is prompt-optimizer — this skill only authors new handoff specs from repo analysis).
---

# Codex Orchestrator Brief

> **Load the shared core first.** Invoke `codex-orchestration-core` via the Skill
> tool and read its orchestration-loop invariants (numbered 1–11) before applying
> this skill. They are the single source of truth for the loop's philosophy; this
> skill adds only what is unique to *authoring a written handoff package*. Where a
> step below cites "core #N", that invariant lives in the core skill, not here.

This skill produces the package that lets an implementation model complete a
repo-wide refactor (or similar multi-wave task) **without the author in the loop**.
You write; another model runs. The package has three deliverables:

1. `refactor-instructions.md` — the spec: what the project is, what must not break,
   where the debt is, in what order to fix it.
2. **Pre-implementation questions** — only the decisions that genuinely cannot be
   made from the code. Delivered in chat, not in the files.
3. `codex-goal-prompt.md` — the orchestrator prompt: a `/goal`-driven loop with
   implementer/verifier subsessions, waves, a checkable rubric, and merge gates.

Both files go to the repository root unless the user says otherwise. Write the
deliverables in the repository's primary documentation language (check AGENTS.md /
README); keep code identifiers, commands, and file paths verbatim.

The quality bar to keep in mind throughout: the implementation model will trust
this package completely and cannot ask you anything. Every claim it acts on must
be true, every command must run, and everything dangerous must be fenced off
explicitly (core #10).

## Phase A — Evidence gathering

Never write the spec from priors. The spec's authority comes entirely from
evidence, and one wrong file path or stale claim poisons the implementer's trust
in all of it.

1. Read the repo's own ground rules first: AGENTS.md / CLAUDE.md / README,
   project rule files (`.claude/rules/`), contribution docs, and any tech-debt
   tracker or ADR index. These define vocabulary, prohibitions, and search
   exclusions you must honor in everything you generate.
2. Fan out parallel Explore agents (read-only), one per lens. The standard four:
   - **Backend/core**: structure, entry points, layering rules and violations,
     file-size hotspots, debt signals (TODO/FIXME, swallowed errors, compat shims,
     duplication), test coverage map, security-sensitive boundaries, async/jobs.
   - **Frontend/clients**: structure and conventions, hotspots, dead-code tooling
     state, test gaps per feature, contract/codegen drift risks.
   - **Runtime/infra**: where things actually run vs. where docs say they run,
     containers/compose, queues and long-running work, settings/secrets handling,
     operational scripts.
   - **Verification/docs governance**: the exact canonical commands (lint,
     typecheck, test, boundary checks, docs lint, codegen), which are required
     gates vs. advisory, CI workflows, generated-docs sync, stale plans/docs.
   Adjust lenses to the repo (a CLI tool has no frontend; a monorepo may need one
   agent per package). Tell every agent which paths to exclude.
3. **Verify load-bearing claims yourself** before they enter the spec (core #6).
   Subagents summarize; summaries drift. Anything that becomes a debt item, a
   prohibition, or a rubric line gets a direct check: re-run the `wc -l`, read the
   actual function, confirm the doc reference really is stale, confirm the file
   really is gitignored. A claim you can't verify gets reported as a question, not
   asserted as fact.

## Phase B — Write `refactor-instructions.md`

Use `references/refactor-instructions-template.md` for the section-by-section
skeleton and guidance. The non-negotiable properties:

- **Every debt item carries evidence** — file paths with line numbers, command
  output, or a quoted doc sentence. An item without evidence doesn't ship.
- **Every debt item carries a verdict**: ✅ implement now / 🔍 investigate,
  smallest fix only / 🛑 propose only, implementation forbidden. The 🛑 set is
  exactly core #10's untouchable surfaces; when in doubt, downgrade toward 🛑.
- **Deletion requires proof of deadness**. If you can't prove a file/function/doc
  is unused, it becomes a question, not a deletion task.
- **Phases run smallest-blast-radius first** (core #9), encoded as the spec's
  phase order: a later phase must never be a prerequisite for an earlier one.
- Baseline commands must be the repo's canonical ones (its task runner, not raw
  tool invocations), and you must have confirmed each one exists.

## Phase C — Pre-implementation questions

Collect only questions where the code cannot decide: spec/test contradictions,
ambiguous prohibitions (does "don't do X in process" mean delegation suffices?),
deletion candidates without proof, choices between designs that need product
judgment. For each: state the question, the evidence both ways, which debt items
it blocks, and the safe default the spec already encodes while unanswered. If
everything was decidable from code, say so explicitly — an empty questions
section is a finding, not an omission.

## Phase D — Write the orchestrator prompt

Use `references/orchestrator-prompt-template.md` for the skeleton. The prompt's
job is to encode the core invariants into a self-contained loop another model can
run: a pure-orchestrator main session (core #1) whose first action is `/goal`
(core #5), with a strict author/critic split where the critic is cold (core #2),
an executable rubric (core #3) gated only by critic PASS (core #4), waves mapped
to the spec's phases (core #9), bounded retries (core #7), an orchestrator-owned
memory log (core #8), and hard-stop tripwires for untouchable surfaces (core #10).

What is unique to *this* skill, beyond faithfully transcribing those invariants:

- The prompt is **written for another model to run**, so every primitive it names
  (subsession spawning, message routing, PR operations) must be phrased as
  instructions that model can follow with zero further context from you.
- The rubric must test **exactly what the spec's phases produce** — including that
  the 🛑 items stay untouched. The prompt and the spec are two halves of one
  contract; they cannot disagree.
- Pin the repo's real sandbox/env edges (core #11) into the prompt's templates so
  the running model hits them with the mitigation already in hand.

When the implementation model is **Codex specifically**, pull the codex-targeted
phrasing from the **`codex-prompting`** skill (the core skill's "Related" section
defines the division of labor). One point is load-bearing here: the orchestrator
prompt names subsession spawning as a primitive, and Codex will not spawn a
subsession unless the prompt teaches it explicitly.

## Final checks before handing over

- Re-read both files as if you were the implementer with zero context: is any
  instruction ambiguous, any referenced file nonexistent, any command unverified?
- Confirm the two files don't contradict each other (the prompt's rubric must test
  exactly what the spec's phases produce, including the 🛑 items staying
  untouched).
- Deliver in chat: the questions (Phase C), a short summary of what the analysis
  found, and the one-line invocation the user will give the implementation model
  (e.g. `/goal refactor-instructions.md に書かれたことを完遂しろ`).
