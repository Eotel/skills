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

Callers cite these as `core #N (short name)` so a renumbering here is caught on
read. Canonical short names: #1 no-code orchestrator, #2 cold critic,
#3 executable contract, #4 PASS gate, #5 goal anchor, #6 verify-not-trust,
#7 bounded retries, #8 memory log, #9 disjoint parallelism, #10 untouchable
surfaces, #11 sandbox edges. Inserting or renumbering an invariant must update
both caller skills in the same change.

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
   Three hard sub-rules (2026-07 retrospective, 107 delegated failures):
   - **Empty diff + success report = hard failure.** Run `git -C <worktree> diff
     --stat` immediately after every author/fixer session; a "done" with no diff
     means the fix never landed (observed twice in a row on the same task) —
     restart with the evidence re-pasted, do not proceed.
   - **GREEN on static checks alone is rejected.** Require pasted command output;
     for any behavioral change require a test proven to fail against pre-fix code
     (load-bearing). Typecheck+lint GREEN has shipped HIGH-severity security and
     data-integrity bugs.
   - **Watchdog stalled sessions.** No tool activity for 5–10 min → kill and
     redispatch (a wrong-cwd reviewer once stalled 19 hours). Every session's
     mandatory FIRST command is `cd <worktree> && pwd && git log -1 --oneline`,
     and it must STOP if the output is not the expected worktree/branch.

7. **Bounded retries, then escalate.** Cap retries per unit (default 3). On
   exhaustion, escalate to a human-facing question — never an unbounded fix loop.
   Two loop-collapse rules (the worst observed loop ran 25–30 review→fix rounds
   over 1.5 days):
   - **Round 1 demands the class, not the instance.** For any sanitization /
     parity / traversal / coverage task, the author prompt says: *"Fix the general
     defect class, not the literal example. State the general rule your fix
     enforces; enumeration-of-examples fixes are prohibited."*
   - **Same defect class recurring 3× across rounds = stop looping.** Escalate to
     a structural redesign (allowlist / anchored fullmatch / different layer) or a
     human design checkpoint. "One more finding" is not a strategy.

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
    Launch-layer rules distilled from ~45 environmental failures (42% of all
    delegated failures — every one predictable and preventable):
    - **Codex never commits.** A linked worktree's `.git/worktrees/<name>/`
      metadata lives in the parent repo outside the sandbox's writable roots, so
      `git add/commit/checkout` fails with `index.lock` EPERM *after* all the work
      is done (10+ sessions per batch lost their final step; 1Password
      `op-ssh-sign` hangs non-interactive commits too). Standing clause in every
      author/fixer prompt: *"Do NOT run git add/commit/checkout — leave changes
      uncommitted; the orchestrator commits outside the sandbox."* Drop any
      separate-RED/GREEN-commit contract under this constraint.
    - **Point the session cwd at the target worktree at launch.** Writable roots
      are fixed by launch cwd, not by prompt text ("don't cd out" does not bind
      the sandbox); `apply_patch` resolves paths from launch cwd, so a wrong cwd
      silently lands edits in the wrong repo.
    - **Write-probe before dispatch.** The session's first command after the
      cwd-verify is `touch <worktree>/.codex-write-probe` — STOP on failure.
      (All 5 parallel implementers of one wave once produced zero work because
      the worktree was outside the writable roots.)
    - **Read-only vs `--write` is an explicit, mandatory dispatch field** — a fix
      task was re-sent into a read-only sandbox 3× before anyone added `--write`.
    - **Pre-provision deps at worktree creation** (install or symlink a populated
      sibling `node_modules`/`.venv`, run codegen); installs are network-blocked
      in-session. If deps are absent, say so upfront: "do not attempt install;
      verify statically and report the gap."

## Standing dispatch preamble (single source of truth)

Inject this block (adapted per repo) into **every** author/critic/fixer prompt.
It eliminates the per-session rediscovery tax measured at 2–5 wasted turns per
session across hundreds of sessions:

```
ENVIRONMENT FACTS (do not rediscover):
- GitNexus is NOT available in this sandbox. Do not probe for it (npx gitnexus
  hangs on blocked network). Use rg / git grep for blast-radius analysis.
- ENV PIN: UV_CACHE_DIR, UV_TOOL_DIR, MYPY_CACHE_DIR, XDG_DATA_HOME,
  TMPDIR=/private/tmp/<task>. Use `uv run --frozen -- <tool>`, never `uvx`.
  `tach` needs `--offline`. <repo-specific dylib pins, e.g.
  DYLD_LIBRARY_PATH=/opt/homebrew/lib for WeasyPrint>.
- Blocked test runners here: <e.g. MongoMemoryServer (listen EPERM), vitest via
  pnpm (hangs — call the binary directly)>. Accepted fallback verification:
  <exact typecheck+lint commands>. Report blocked runs as environment
  constraints, not code findings.
- Known flaky baseline: <repo's known-flaky tests>. Re-run in isolation before
  calling one a regression.
- Emit reports/JSON as your final stdout block. Do NOT write them with
  apply_patch (the path is outside the worktree and will be rejected).
GUARDRAILS (non-negotiable):
- No git add/commit/checkout/push. No --no-verify. No error-silencing
  (.catch(()=>{}), --forceExit). No reintroducing mocks removed by prior topics.
- Never cd outside the worktree. Never edit files outside your MAY-touch list;
  ignore (do not fix, do not revert) other dirty files you encounter.
- Run formatters/linters in CHECK-ONLY mode; if autofix is unavoidable, revert
  any out-of-scope file it touched before finishing.
FINAL REPORT: use this fixed schema, in this order, keep it short:
status / files_changed / commands_run (with pasted tail of output) /
blocked_by (or "none") / notes.
```

When a mid-run workaround is discovered (a new cache path, a new hang), update
this preamble and **all fixed command blocks at once** — a workaround applied to
only one command block was rediscovered from scratch by later sessions.

### Author (implementer/fixer) upfront prohibitions

Roughly half of all recurring critic catches are mechanical and belong in the
author prompt as standing prohibitions (the critic then spends its budget on the
reasoning-heavy half — races, idempotency, representation-bypass, runtime-type
mismatches):

```
UPFRONT RULES (violations = automatic FAIL):
- Touch one CRUD path → update its siblings (create AND update AND bulk) and
  grep every producer/consumer of a changed contract.
- New non-null / denormalized column → RunPython (or equivalent) backfill in the
  same change.
- Never use `x or y` where an explicit empty list / 0 / False / "" is a valid
  value; never use truthiness to test "unset".
- Validation must hold at the persistence layer, not only in clean()/form-layer
  (objects.create / bulk_create must not bypass it).
- A presence/config check is not a capability check — exercise the actual
  runtime path (real import, real bind, real conversion).
- Do not mock the unit under test. Assertions must be load-bearing
  (toStrictEqual + toHaveBeenCalledTimes; a test passing for an incidental
  reason — empty set, unconditional handler — is a defect). Prove new tests fail
  on pre-fix code.
- A feature you build must be wired in (imported/mounted/registered) — built-but-
  unintegrated components are defects.
- No secrets/tokens in logs or error strings. No URL validation via
  startsWith/string ops. Fail closed on invalid upstream responses.
- Every externally-influenced input gets a byte/line budget; no unbounded
  pagination or user-supplied regex without anchoring.
- Docs-only tasks touch zero runtime files. No blanket lint disables or
  fmt-off regions to silence findings.
```

### Critic (reviewer/verifier) hardening

Append to every critic prompt:

- New files may legitimately be **untracked** in the worktree — untracked status
  alone is not a finding.
- Grep the **whole test suite** for tests asserting the old/removed behavior,
  not just the diff (a stale pre-existing test slipped past both author and
  critic).
- Grep shared hooks/utils for residual mock fallbacks — not only changed files.
- For fixer dispatches, frame as *"fix EXACTLY these N findings — no redesign,
  no extra hardening"* with an explicit MAY-touch whitelist; loose "fix all P1s"
  framing reopens design and caused scope creep.
- Well-specified findings should include the **exact expected code** — one-shot
  clean fixes measurably beat freeform prose descriptions.

## When these apply

These invariants hold for any delegated, critic-gated, multi-wave implementation
loop, regardless of which model runs it. `brief` encodes them into a written spec
and an orchestrator prompt for another model; `tdd-orchestration` enacts them live
with Claude as the orchestrator. Read the caller skill next for the part that is
unique to its mode.

## Model-specific tuning lives in the callers

These invariants are deliberately model-agnostic and stay that way. Per-model
prompt tuning (verbosity, effort, tool eagerness, autonomy framing) lives in
each caller skill's `references/model-*.md` guides — load at most one there
when the execution model is known. Do not add model-conditional text here.

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
