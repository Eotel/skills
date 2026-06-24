---
name: codex-orchestration-core
description: Shared orchestration-loop invariants for delegated, autonomous multi-wave implementation work — the principles common to codex-orchestrator-brief (which authors a written handoff package) and codex-tdd-orchestration (in which Claude drives codex sessions live). This is a dependency loaded by those two skills, not a standalone task skill. Invoke it (via the Skill tool) only when codex-orchestrator-brief or codex-tdd-orchestration instructs you to load it, or when the user explicitly wants to read or edit the shared orchestration principles themselves. For an actual task, do NOT invoke this — use codex-orchestrator-brief (produce instructions for another model to run) or codex-tdd-orchestration (run the orchestration in this session now).
---

# Codex Orchestration Core

The orchestration-loop invariants shared by **codex-orchestrator-brief** and
**codex-tdd-orchestration**. Both skills delegate all coding to an implementation
model and keep the orchestrator out of the editor; they differ only in *who runs
the loop* — `brief` writes a handoff package that **another model** later runs,
`tdd-orchestration` has **Claude itself** run it live. Everything they agree on
lives here so a change to the shared philosophy is made once and cannot drift
between the two.

> **This is the single source of truth for the principles below.** Do not restate
> them in the caller skills — caller skills load this and then add only their
> own delta. If you are about to copy a principle from here into `brief` or
> `tdd-orchestration`, stop: edit it here instead.

## How this is loaded

A caller skill's first instruction is to invoke `codex-orchestration-core` via
the Skill tool, read these invariants, and then apply its own skill-specific
section. The three skills ship as a **bundle** — installing a caller without this
core breaks that first instruction (APM installs skills individually and does not
resolve this dependency for you).

## Vocabulary (neutral terms ↔ per-skill mapping)

The two callers use different local words for the same roles. The invariants
below use the neutral term; map it to the caller's vocabulary as you read.

| Neutral term (here) | codex-orchestrator-brief | codex-tdd-orchestration |
|---|---|---|
| **author** (writes code/docs) | implementer | implementer (then fixer) |
| **critic** (judges the author's output cold) | verifier | reviewer |
| **unit of parallel work** | wave | topic |
| **acceptance contract** | rubric | integration gate + review |

`brief` runs a 2-role loop (author → critic); `tdd-orchestration` runs a 3-role
loop (author → critic → fixer). The fixer is still an author under these rules,
and its output is re-judged by a cold critic — never self-approved.

## The orchestration-loop invariants

1. **The orchestrator edits no code.** Planning, scheduling, routing messages,
   git/worktree/branch/PR operations, and owning the memory log are orchestrator
   work. Anything that touches application source is delegated. Delegation is
   what keeps the orchestrator's context clean enough to run the whole loop.

2. **Author and critic are separated, and the critic is cold.** The critic runs
   in a fresh context that has never seen the author's conversation and receives
   only the diff (or the produced artifact), the spec, and its slice of the
   acceptance contract. Self-critique inside the author's own context is reliably
   too lenient; forwarding the author's own report to the critic reintroduces the
   same bias through the side door, so do not do it.

3. **The acceptance contract is executable, never subjective.** Every line is a
   command plus its expected result, runnable by the critic itself. "Tests pass"
   counts only as the literal command that proves it. Subjective lines ("code is
   cleaner") are forbidden — they give the model nothing to hillclimb on.

4. **Critic PASS is the only merge/accept gate.** No self-merge, no orchestrator
   discretion, no "the author says it's done." This is the single rule that makes
   it safe to let the loop run autonomously.

5. **`/goal` is the durable anchor, and it is a passive gate.** Set it first,
   written as a *verifiable end-state* (not a vague intent), with the termination
   condition defined as "every contract line judged PASS by an independent
   critic." It survives compaction/resume and blocks Stop until satisfied — so it
   only belongs in a session where something is actively pushing toward it (a
   live background agent, a Monitor stream, or a polling loop). `/goal` alone in
   an idle session is a deadlock; vague `/goal` is a stop-loop trap.

6. **Verify; do not trust the status line.** After every delegated completion,
   look at the actual evidence (`git diff --stat`, the real command output, the
   produced file) before believing the agent's report. Agent reports cannot be
   fully trusted, and "forwarded to the companion" is not "done."

7. **Bounded retries, then escalate.** Cap retries per unit (default 3). On
   exhaustion, escalate to a human-facing question — never an unbounded fix loop.

8. **The memory log is orchestrator-owned and admits only verified facts.** Keep
   a log (e.g. `orchestration-log.md`) with a Distilled Rules section populated
   only by the cycle fail → investigate → verify → distill. Paste the distilled
   rules into every new author so the same failure never recurs. An unverified
   hunch does not enter the log.

9. **Smallest blast radius first; parallelize only when disjoint.** Order
   waves/topics so an earlier unit is never a prerequisite of a later one
   (baseline → safety nets → docs/comment fixes → mechanical extractions →
   structural splits → proposals-only). Units may run in parallel only when their
   file sets do not intersect; if they overlap, re-scope to make them disjoint or
   serialize them with an explicit ordering rationale. Merges are serialized, and
   an in-flight branch re-verifies after absorbing the integration base.

10. **Untouchable surfaces are fenced off, and breaching one is a hard stop.**
    Security boundaries (authz, audit, redaction, signing, sandbox/isolation,
    payment, external integrations), public API contracts, DB schema/migrations,
    generated files, and anything needing a product decision are propose-only.
    When in doubt, downgrade toward propose-only — an over-cautious run wastes a
    little time; an over-permissive one ships a regression. If such a surface
    appears in a diff (generated-file drift, a migration, an out-of-scope path),
    it stops **all** units, not just the offending one.

11. **The codex sandbox and host env have known sharp edges.** Budget for them:
    `git worktree add` may be blocked and sibling-path worktrees may be unwritable
    (pre-create the worktree inside the repo's writable root, e.g.
    `<repo>/.worktrees/<name>`, with sibling-dep symlinks for `path = "../<dep>"`);
    `github.com` may be blocked (push/`gh` operations fail from the sandbox);
    a failed session can rewrite the worktree's `.git` pointer; local lint can
    pass while CI lint fails (`ruff format --check` etc.); and the sandbox does
    not inherit devenv/Nix activation, so pin env like
    `DYLD_LIBRARY_PATH=/opt/homebrew/lib` into every prompt that runs `pytest`.

## When these apply

These invariants hold for any delegated, critic-gated, multi-wave implementation
loop, regardless of which model runs it. `brief` encodes them into a written spec
and an orchestrator prompt for another model; `tdd-orchestration` enacts them live
with Claude as the orchestrator. Read the caller skill next for the part that is
unique to its mode.

## Related: phrasing the codex-directed text

Both callers ultimately emit prompts **aimed at Codex** — `brief` writes an
orchestrator prompt another model runs; `tdd-orchestration` composes live
implementer/reviewer/fixer session prompts. For *how to phrase that text well* —
Codex's harness quirks (AGENTS.md injection, the internal planning tool, preamble
cadence, `apply_patch`), GPT-5.x prompting principles, model/effort selection, and
the **must-teach-explicitly** subagent/session feature (Codex won't fan out or
resume sessions unless the prompt names the mechanism) — consult the
**`codex-prompting`** skill and its templates. Division of labor: these invariants
govern the *loop*; `codex-prompting` governs the *content* of the codex-directed
prompts. Keep loop philosophy here; pull phrasing from there.

On **transport** (how the codex-directed text actually reaches a codex process):
`brief` hands a written prompt to a human/launcher; `tdd-orchestration` spawns
sessions via `Agent(codex:codex-rescue)`. A third option is **agmsg** — spawn a
*named* codex (or claude-code) peer and `send` it the goal prompt over a local bus,
when you want addressable, long-lived agents you message across turns. See
`codex-prompting`'s `references/subagents-and-sessions.md` (section C). Transport
choice does not change these invariants — the cold-critic split and PASS-only gate
still hold whichever way the prompt is delivered.
