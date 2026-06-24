---
name: codex-tdd-orchestration
description: Orchestrate multiple parallel Codex CLI sessions for multi-topic implementation work where Claude itself does no coding. Use when the user wants to delegate 2+ independent fix/refactor/feature topics to codex with TDD enforcement, adversarial review, and a remediation pass per topic. Trigger on phrases like "codex で並列に", "orchestrator として", "codex に任せて", "implementer/reviewer/fixer に分けて", "並列で codex を回して", and on multi-topic post-review fix work where you would otherwise be tempted to edit code yourself. Strongly prefer this skill over one-shot single-codex delegation whenever there are 2+ topics that benefit from independent adversarial review.
---

# codex-tdd-orchestration

> **Load the shared core first.** Invoke `codex-orchestration-core` via the Skill
> tool and read its orchestration-loop invariants (numbered 1–11) before applying
> this skill. They are the single source of truth for the loop's philosophy; this
> skill adds only what is unique to *Claude driving codex sessions live*. Where a
> step below cites "core #N", that invariant lives in the core skill, not here.

## What this is

A workflow where Claude acts purely as an **orchestrator** (core #1) and all
coding is delegated to Codex CLI via `Agent(subagent_type="codex:codex-rescue")`.
Each topic gets three independent codex sessions in sequence — implementer (TDD),
adversarial reviewer, fixer — and topics run in parallel. Claude's job is to plan,
delegate, **verify** (core #6), commit, and handle operations codex's sandbox
cannot. Unlike codex-orchestrator-brief, which writes a handoff package for
another model to run later, here Claude runs the loop live in this session.

## Why this shape

Codex sessions give three things a single Claude context cannot: **independence
between author and reviewer** (core #2 — the reviewer can't see the implementer's
internal rationalizations, only the diff), **parallelism across topics**, and
**isolation from Claude's working context**. Splitting authorship from critique
catches design and edge-case issues an author tends to defend rather than
acknowledge.

## When to use

- The user explicitly asks Claude to orchestrate codex
- There are 2+ topics that can be designed to touch non-overlapping files (core #9)
- Each topic produces code worth independently reviewing before merging
- The user has indicated Claude should not edit code directly

## When not to use

These exclusions **override the "When to use" triggers** — including the case
where the user named this skill by hand. When both fire, do not orchestrate;
surface the recommended alternative to the user instead.

- Single-file changes — overhead isn't worth it. **Alternative: do it inline or
  dispatch one `codex:codex-rescue` session.**
- Tasks where adversarial review adds nothing (typo fixes, dep bumps,
  formatter-only diffs). **Alternative: one codex call, or have the user do it
  directly.**
- Tasks that fit in one PR description's worth of context. **Alternative: one
  codex call.**

## Core principles (this skill's application of the shared invariants)

1. **Claude edits no code** (core #1). Worktree setup, git commits, push,
   `gh pr edit` are operations — those are fine. Anything that touches application
   source is delegated.
2. **One topic = three codex sessions.** Implementer → reviewer → fixer, in
   separate sessions, so the reviewer is genuinely cold (core #2) and the fixer's
   output is re-judged rather than self-approved.
3. **TDD is enforced in the implementer prompt.** RED test first, confirm FAIL,
   then implement to GREEN, then refactor. The RED→GREEN command is this skill's
   executable acceptance contract (core #3).
4. **Topics are designed to be file-disjoint** (core #9). Disjoint topics run in
   parallel safely in the same worktree. If the user-provided topics overlap on
   files, two moves are available, in this order of preference:
   - (a) **Re-scope**: redesign the topic boundaries so each owns a different
     surface (e.g., extract a new module that two of the topics will then
     consume). **Re-scoping materially changes deliverables — surface the new
     boundaries to the user as a proposal before launching; fall back to (b) if
     the user rejects.**
   - (b) **Serialize**: if re-scoping isn't feasible, run the overlapping topics
     one-at-a-time (still implementer → reviewer → fixer per topic), with an
     explicit ordering rationale. Inter-topic parallelism is dropped; intra-topic
     phasing stays.
5. **The orchestrator verifies, doesn't trust** (core #6). After every codex
   completion, look at `git diff --stat` against the worktree before believing the
   codex's status line.
6. **Commits are serialized through the orchestrator** to avoid git index races
   between concurrent codex sessions.

## The 3-phase workflow

- **Phase 0** — Base preparation: orchestrator creates worktree at
  `<repo>/.worktrees/<name>` (inside the project so codex's sandbox accepts
  writes, per core #11); then 1 codex session merges upstream base + confirms green
- **Phase 1** — Parallel topics: for each topic, run implementer → reviewer →
  fixer codex sessions; topics parallel between each other
- **Phase 2** — Integration gate + commits (sequential, orchestrator only): full
  test/lint/typecheck + CI-equivalent checks + per-topic commit + push + CI verify

Detailed steps, verification gates, and timing live in
[references/workflow.md](references/workflow.md).

## Codex prompt templates

Reusable skeletons for implementer / reviewer / fixer prompts live in
[references/prompts.md](references/prompts.md). Use them as starting points and
fill in the topic-specific scope.

For the codex-specific phrasing that makes those skeletons land — harness quirks
(AGENTS.md injection, the internal planning tool, preamble cadence, `apply_patch`),
model/effort selection, outcome-first framing, and the explicit subagent/session
teaching Codex needs — consult the **`codex-prompting`** skill. Fold its principles
into the implementer/reviewer/fixer prompts here: `codex-prompting` governs how to
talk to codex; this skill governs the orchestration loop around those sessions.

**Transport note.** This skill's default transport is
`Agent(subagent_type="codex:codex-rescue")`. An alternative is **agmsg**: spawn a
*named* peer (`/agmsg spawn codex implementer`) and `/agmsg send` it the goal
prompt — useful when you want addressable, long-lived implementer/reviewer/fixer
agents you can message across turns (and to mix in `claude-code` peers). The loop
rules are unchanged: the reviewer is still a separate cold session, PASS still
gates merge. See `codex-prompting` → `references/subagents-and-sessions.md` (C).

## Pitfalls (read this before starting)

The shared sandbox/env edges are core #11; their concrete symptoms, root causes,
and mitigations for *this live workflow* live in
[references/pitfalls.md](references/pitfalls.md):

1. Codex sandbox blocks `git worktree add` AND rejects writes to sibling-path
   worktrees → orchestrator pre-creates the worktree at `<repo>/.worktrees/<name>`
   so it lives inside writable_roots; sibling-dep symlinks handle
   `path = "../<dep>"` resolution
2. Codex sandbox blocks `github.com` → push/gh operations fail
3. "Forwarded to Codex companion" ≠ done; companion can die silently
4. Failed codex can rewrite the worktree's `.git` pointer
5. Local lint passes, CI lint fails (`ruff format --check` etc.)

Read [references/pitfalls.md](references/pitfalls.md) before launching Phase 1.
The mitigations are cheap; the bugs are expensive.

## Task tracking

This orchestration is long-running (1–3 h wallclock per topic, 8+ codex sessions
for a 2-topic run is normal). Use **both** of the following — they serve different
purposes:

### `/goal` — durable session anchor (set this FIRST)

`/goal` is the durable anchor (core #5). Set it **before launching the first
background codex**, because this orchestration runs codex sessions via
`run_in_background=true` and watches them via Monitor — both long-running and
asynchronous, and the orchestrator's context can be compacted, resumed, or
interrupted mid-flight. `/goal` is the only state that reliably survives those
edges:

| Tracking surface | Survives compaction? | Survives session resume? | Survives Stop? |
|---|---|---|---|
| TaskCreate state | partial (in conversation context) | yes if state persisted, but list view is rebuilt | yes |
| Monitor output | no — streaming, ephemeral | no | no |
| `/goal` | yes | yes | enforced (see below) |

Without `/goal`, a session compacted halfway through Phase 1 may resume not
knowing which topics it launched, which background tasks are its own, or what
"done" looks like. With `/goal`, the next instance reads it on resume and
re-anchors on intent before consulting TaskCreate / Monitor for current position.

Write the goal as a **verifiable end-state** (core #5), not a vague intent. The
Stop hook checks the goal against transcript evidence and blocks termination if it
isn't satisfied, so vague goals trap you in a stop loop you can't escape from your
side.

- Good: `codex-tdd-orchestration: T1 add-type-hints + T2 rename-class on myproj — done when both PRs are CI-green and merged to main`
- Bad: `do the refactor` (unverifiable; Stop hook can't find evidence)
- Bad: `(test 呼び出し)` (literal test string; Stop hook has nothing to match)

Update `/goal` only when the *intent* changes (a topic dropped, a follow-up topic
added, or aborting mid-flight — in which case clear it so the next session start
isn't blocked). Per-step progress goes into TaskCreate, not `/goal`.

**Critical: `/goal` is a passive gate, not an active driver** (core #5). It blocks
Stop until satisfied, but it doesn't itself make anything happen. Only set `/goal`
when *something is actively pushing toward it* — running background codex sessions,
a Monitor stream watching them, or a `/loop` polling for completion. Setting
`/goal` in an otherwise-idle session creates a deadlock: Stop refuses to close,
but no work is being done to satisfy it. The pairing rule:

- ✓ `/goal` + at least one `run_in_background=true` codex agent + Monitor watching it
- ✓ `/goal` + a `/loop` periodically checking CI / PR status
- ✗ `/goal` alone, with no live agent or loop — clear the goal or don't set it

If you discover an idle session is goal-locked, the only escape is user-side
`/goal clear` (the assistant cannot call it).

### TaskCreate — per-step structure (granular, dependency graph)

Use TaskCreate at the start of orchestration to make the *structure* visible:

- Task per phase (`Phase 0: merge base`, `Phase 2: integration + push`)
- Task per topic (`T1: <name>`, `T2: <name>`, ...)
- `addBlockedBy` to encode the dependency graph (T1..N blocked by Phase 0; Phase 2
  blocked by all topics)
- Mark `in_progress` when a topic's first codex launches, `completed` only after
  the orchestrator has **verified** the fixer's output (core #6 — not when the
  codex reports done)

`/goal` answers "what is this session doing?"; TaskCreate answers "where in the
plan are we right now?". The user sees both without having to ask.

## Project-specific env

Some macOS Nix/devenv projects need `DYLD_LIBRARY_PATH=/opt/homebrew/lib` for
`pytest` to load `libharfbuzz` etc. (core #11). Pin such env into every codex
prompt template — codex sandbox does not inherit devenv shell activation reliably.
See [references/pitfalls.md](references/pitfalls.md) for the canonical macOS
symptom.
