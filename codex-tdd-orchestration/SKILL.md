---
name: codex-tdd-orchestration
description: >-
  This skill should be used when the user asks Claude to orchestrate Codex CLI
  sessions for implementation work — trigger phrases include "codex で並列に",
  "orchestrator として", "codex に任せて", "implementer/reviewer/fixer に分けて",
  "並列で codex を回して", "orchestrate codex", "run codex in parallel" — and on
  multi-topic post-review fix work where Claude would otherwise edit code itself.
  Invocation starts with the Scope Fit Gate in the body — issue/topic-level
  implementation routes to durable Codex threads, while micro reviews, short
  follow-up checks, and narrow verification passes use the smallest independent
  transport: a subagent, or a Codex thread only when an isolated worktree/history
  is needed. Claude itself does no coding in either mode.
---

# codex-tdd-orchestration

> **Load the shared core first.** Invoke `codex-orchestration-core` via the Skill
> tool and read its orchestration-loop invariants (numbered 1–11) before applying
> this skill. They are the single source of truth for the loop's philosophy; this
> skill adds only what is unique to *Claude driving codex sessions live*. Where a
> step below cites "core #N", that invariant lives in the core skill, not here.

## Scope Fit Gate (run before launching anything)

Using this skill does **not** mean full orchestration is approved. It means:
first decide whether full orchestration fits, then either launch it or route to a
lighter TDD path. The gate overrides everything — including the case where the
user named this skill by hand.

Launch durable Codex-thread orchestration only when **all** of these are true:

- The user explicitly asks Claude to orchestrate codex and wants the main agent
  to avoid application-code edits.
- The work decomposes into issue/topic-level implementation slices, each
  producing code worth independently reviewing before merge.
- Topic file ownership is disjoint (core #9, disjoint parallelism), or the
  topics are still worth serializing with an explicit ordering rationale.
- Each implementation topic needs an isolated worktree or user-owned thread.
- Each topic benefits from a cold adversarial reviewer before merge; the review
  transport is a subagent, or a Codex thread when the review needs its own
  isolated worktree/history. The reviewer runs in a fresh context that never saw
  the author's report (core #2) — the orchestrator itself does not qualify.

When **all** gates pass, the route is full orchestration: one durable Codex
thread/worktree per issue/topic implementation slice (Phase 0/1/2 below).

If **any** gate fails, do **not** run Phase 0/1/2. Report the lighter route that
matches the scope shape and then use it:

| Scope shape | Lighter route (gate did not pass) |
|---|---|
| One GitHub issue, one coherent topic, or a diff that fits in one PR description | One Codex TDD worker, or inline TDD if delegation is unavailable |
| Micro cold review, short follow-up check, or narrow verification pass | A subagent (fresh cold context); do not create a user-visible Codex thread |
| Typo, dependency bump, formatter-only, or obvious mechanical edit | One Codex call or inline edit |
| User also asks for PR, Oracle, Copilot review handling, or "can ship" | Treat those as post-implementation gates, not as extra orchestration topics |
| Two topics with overlapping files | Re-scope with a user-facing proposal, otherwise serialize; do not parallelize |

When the lighter route applies, preserve the valuable parts: RED → GREEN →
REFACTOR, cold review before acceptance, focused verification after each fix,
PR/review handling by the main agent, and one broader final gate before
"can ship." Avoid repeated full-suite runs between small review fixes unless the
finding touches shared behavior.

## Acceptance Objective and Roadmap (full orchestration only)

After the Scope Fit Gate passes and before launching Phase 0, write a finite
roadmap that the orchestrator will burn down. This is the antidote to
micromanagement: the orchestrator drives toward a verifiable end-state, clears
milestones, and intervenes only on evidence, blockers, or retry caps.

Four artifacts, produced in Phase -1 — the full definition of each lives in
[references/workflow.md](references/workflow.md):

1. **Acceptance objective** — one externally checkable `done when ...` sentence.
2. **Acceptance checklist** — 5-9 executable checks, each naming owner,
   command/evidence, and expected result.
3. **Topic roadmap** — dependency-ordered milestones, each with an exit
   criterion, transport choice, and retry cap.
4. **Agent contracts** — every Codex thread or subagent prompt receives the
   global objective, its slice objective, its checklist lines, and its roadmap
   position.

Progress policy: advance by clearing milestones, and verify each exit criterion
before starting the next. Classify any new subtask (in-scope / new topic /
out-of-scope) before launching more work — do not silently expand the roadmap.
Default retry cap is 2 per milestone; after the cap, stop and surface the
blocker with the evidence. Keep the roadmap visible in TaskCreate and mark items
complete only after orchestrator verification, not after an agent status line.

Intervention policy: let waiting agents work. Check roadmap milestones on a
bounded cadence and intervene only on final status, blocked/stalled evidence,
dirty diff mismatch, retry cap, or explicit user request. Do not send repeated
"any update?" nudges merely because a background thread is quiet.

## What this is

When the Scope Fit Gate passes, this is a workflow where Claude acts purely as an
**orchestrator** (core #1, no-code orchestrator) and all coding is delegated to
Codex CLI via `Agent(subagent_type="codex:codex-rescue")` when the work needs an
isolated worktree or user-owned thread. For a parent issue with sub-issues, the
durable split is one Codex thread per issue/topic implementation slice. Cold
review and follow-up fixes still gate acceptance, but their transport is sized
to the work: a subagent for a bounded pass, or a Codex thread only when the
review/fix needs its own isolated worktree or history. The cold reviewer runs in
a fresh context; the orchestrator's own read-only diff check is verification
(core #6), not the cold-review gate (core #2). Topics run in parallel when file
ownership is disjoint. Claude's job is to plan, delegate, **verify**
(core #6, verify-not-trust), commit, and handle operations codex's sandbox
cannot. Unlike codex-orchestrator-brief, which writes a handoff package for
another model to run later, here Claude runs the loop live in this session.

## Why this shape

Durable Codex threads give three things a single Claude context cannot:
**isolated implementation history**, **parallelism across topics**, and
**user-visible ownership for issue/topic slices**. Review independence is still
mandatory (core #2, cold critic): the reviewer runs in a fresh context that never
saw the author's conversation or report. A subagent satisfies this cheaply without
a user-visible thread — independence is about information separation, not about
another user-visible thread. The orchestrator itself is **not** a valid cold
reviewer: it watched the implementer's report, so using it as the critic
reintroduces the author's bias through the side door (core #2, core #6). Its
read-only diff check is core #6 verification — a supplement to, never a substitute
for, the cold-review PASS. Splitting authorship from critique catches design and
edge-case issues an author tends to defend rather than acknowledge.

## Core principles (this skill's application of the shared invariants)

1. **Claude edits no code** (core #1, no-code orchestrator). Worktree setup, git commits, push,
   `gh pr edit` are operations — those are fine. Anything that touches application
   source is delegated.
2. **One issue/topic = one durable implementation thread when isolation is
   needed.** The implementer is a Codex thread for issue-level work. Reviewer and
   fixer transport is chosen by size/risk: a subagent for small bounded passes;
   a Codex thread only when the review or fix is large, stateful, or needs its own
   dirty worktree/history. The cold reviewer runs in a fresh context and must not
   receive the author's report or hidden reasoning — which rules out the
   orchestrator itself as the reviewer — and every fix is re-judged before PASS
   (core #2, cold critic; core #4, PASS gate).
3. **TDD is enforced in the implementer prompt.** RED test first, confirm FAIL,
   then implement to GREEN, then refactor. The RED→GREEN command is this skill's
   executable acceptance contract (core #3, executable contract).
4. **Topics are designed to be file-disjoint** (core #9, disjoint parallelism). Disjoint topics run in
   parallel safely as separate durable worktrees/threads. If the user-provided
   topics overlap on files, two moves are available, in this order of preference:
   - (a) **Re-scope**: redesign the topic boundaries so each owns a different
     surface (e.g., extract a new module that two of the topics will then
     consume). **Re-scoping materially changes deliverables — surface the new
     boundaries to the user as a proposal before launching; fall back to (b) if
     the user rejects.**
   - (b) **Serialize**: if re-scoping isn't feasible, run the overlapping topics
     one-at-a-time (still author → cold review → fix as needed → cold PASS per
     topic), with an explicit ordering rationale. Inter-topic parallelism is
     dropped; intra-topic phasing stays.
5. **The orchestrator verifies, doesn't trust** (core #6, verify-not-trust). After every
   delegated completion, look at `git diff --stat` against the worktree before
   believing an agent's status line.
6. **Commits are serialized through the orchestrator** to avoid git index races
   between concurrent codex sessions.

## The 3-phase workflow

- **Acceptance planning** — before Phase 0, define the acceptance objective,
  executable checklist, topic roadmap, transport choices, and per-agent
  contracts above.
- **Phase 0** — Base preparation: orchestrator creates a worktree at
  `<repo>/.worktrees/<topic>` for each durable implementation thread via
  [scripts/worktree-setup.sh](scripts/worktree-setup.sh) (inside the project so
  codex's sandbox accepts writes, per core #11, sandbox edges); then each
  implementation worktree confirms the upstream base is green before coding
- **Phase 1** — Parallel topics: for each topic, run the durable Codex
  implementer, then cold review and fix passes using the smallest adequate
  transport; topics parallel between each other when files are disjoint
- **Phase 2** — Integration gate + commits (sequential, orchestrator only):
  absorb each topic onto one integration branch, re-run full
  test/lint/typecheck + CI-equivalent checks after each (core #9) + per-topic
  commit + push + CI verify

Detailed steps, verification gates, and timing live in
[references/workflow.md](references/workflow.md).

## Codex prompt templates

Reusable skeletons for implementer / reviewer / fixer prompts live in
[references/prompts.md](references/prompts.md). Use them as starting points and
fill in the topic-specific scope. The skeletons are role contracts, not a
mandate to create three Codex threads for every topic.

**Mandatory template inserts** (from the core skill — do not restate, inject
verbatim from `codex-orchestration-core` § "Standing dispatch preamble"):
every implementer/fixer prompt gets the ENVIRONMENT FACTS + GUARDRAILS +
FINAL REPORT blocks and the "Author upfront prohibitions"; every reviewer prompt
gets the "Critic hardening" additions. Every session's first command is the
cwd-verify-and-STOP + write-probe (core #11); no session commits (core #11 —
the orchestrator commits in Phase 2). After every session, run
`git -C <worktree> diff --stat` before believing its report (core #6).
Round-1 fix prompts demand the defect *class*; a class recurring 3× escalates
out of the loop (core #7).

For the codex-specific phrasing that makes those skeletons land, and for
transport alternatives (agmsg peers instead of `Agent` calls), see the core
skill's "Related" section and the **`codex-prompting`** skill it points to:
`codex-prompting` governs how to talk to codex; this skill governs the
orchestration loop around those sessions.

## Model-specific prompt guides

Keep the common orchestration rules in this file. Load **at most one** model guide
when the target execution model is known or the user names it; otherwise skip this
section and use the common rules only.

- Claude Opus 4.8: [references/model-opus-4.8.md](references/model-opus-4.8.md)
- Claude Opus 5: [references/model-opus-5.md](references/model-opus-5.md)
- Claude Sonnet 5: [references/model-sonnet-5.md](references/model-sonnet-5.md)
- Claude Fable 5: [references/model-fable-5.md](references/model-fable-5.md)
- GPT-5.5: [references/model-gpt-5.5.md](references/model-gpt-5.5.md)
- GPT-5.6: [references/model-gpt-5.6.md](references/model-gpt-5.6.md)

Model guides may tune verbosity, effort, tool eagerness, subagent behavior, and
prompt shape. They must not override the Scope Fit Gate, acceptance objective,
reviewer/fixer separation, verification gates, or retry caps in this file.

## Pitfalls (read this before starting)

The shared sandbox/env edges are core #11 (sandbox edges); their concrete symptoms, root causes,
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

This orchestration can be long-running (1–3 h wallclock per topic is normal when
implementation is substantial). Use **both** of the following — they serve
different purposes:

### `/goal` — durable session anchor (set this FIRST)

`/goal` is the durable anchor and a passive gate (core #5, goal anchor: set it
first, write it as a verifiable end-state, and set it only in a session where
something is actively pushing toward it — otherwise it deadlocks Stop). What is
specific to this workflow: codex sessions run via `run_in_background=true` and
are watched via Monitor — both long-running and asynchronous — and the
orchestrator's context can be compacted, resumed, or interrupted mid-flight.
Set `/goal` **before launching the first background codex**; it is the only
tracking surface that reliably survives those edges:

| Tracking surface | Survives compaction? | Survives session resume? | Survives Stop? |
|---|---|---|---|
| TaskCreate state | partial (in conversation context) | yes if state persisted, but list view is rebuilt | yes |
| Monitor output | no — streaming, ephemeral | no | no |
| `/goal` | yes | yes | enforced (see below) |

A session compacted halfway through Phase 1 resumes not knowing which topics it
launched or what "done" looks like. On resume, read the goal first to re-anchor
on intent, then consult TaskCreate / Monitor for current position.

The Stop hook checks the goal against transcript evidence and blocks
termination until it is satisfied, so an unverifiable goal traps the session in
a stop loop that only user-side `/goal clear` can release (the assistant cannot
call it):

- Good: `codex-tdd-orchestration: T1 add-type-hints + T2 rename-class on myproj — done when both PRs are CI-green and merged to main`
- Bad: `do the refactor` (unverifiable; the Stop hook can't find evidence)
- Bad: `(test 呼び出し)` (literal test string; the Stop hook has nothing to match)

Update `/goal` only when the *intent* changes (a topic dropped, a follow-up topic
added, or aborting mid-flight — in which case clear it so the next session start
isn't blocked). Per-step progress goes into TaskCreate, not `/goal`. The core #5
pairing rule, applied to this workflow:

- ✓ `/goal` + at least one `run_in_background=true` codex agent + Monitor watching it
- ✓ `/goal` + a `/loop` periodically checking CI / PR status
- ✗ `/goal` alone, with no live agent or loop — clear the goal or don't set it

### TaskCreate — per-step structure (granular, dependency graph)

Use TaskCreate at the start of orchestration to make the *structure* visible:

- Task per phase (`Phase 0: merge base`, `Phase 2: integration + push`)
- Task per topic (`T1: <name>`, `T2: <name>`, ...)
- `addBlockedBy` to encode the dependency graph (T1..N blocked by Phase 0; Phase 2
  blocked by all topics)
- Mark `in_progress` when a topic's first codex launches, `completed` only after
  the orchestrator has **verified** cold-review PASS and any required fixes (core
  #6, verify-not-trust — not when the codex reports done)

`/goal` answers "what is this session doing?"; TaskCreate answers "where in the
plan are we right now?". The user sees both without having to ask.
