# Detailed workflow

## Phase -1 — Acceptance planning (orchestrator only)

Before any worktree setup or codex launch, turn the user's request into a finite
roadmap. This prevents the orchestrator from micromanaging agents and gives every
agent an acceptance-evaluable target.

Produce:

1. **Acceptance objective**: one `done when ...` sentence with an externally
   checkable end-state.
2. **Acceptance checklist**: 5-9 executable checks. Each check names owner,
   command/evidence, and expected result.
3. **Topic roadmap**: ordered milestones with dependencies, exit criteria, and a
   retry cap. Use `Phase 0 baseline -> T1..Tn -> integration gate -> PR/CI/review
   gate` unless the repo demands a narrower shape.
4. **Transport plan**: for each milestone, choose durable Codex thread, subagent,
   or parent read-only spot check. Default to Codex threads for issue/topic
   implementation slices; default to subagents or parent spot checks for micro
   reviews, short follow-up checks, and narrow verification passes.
5. **Per-agent contract**: for every planned Codex thread or subagent, identify
   the global objective, slice objective, checklist lines it owns, and roadmap
   position.

Do not launch Phase 0 until the roadmap is small enough to burn down and each
milestone has a concrete exit criterion. If the roadmap keeps expanding, stop and
re-scope instead of adding more open-ended agent work.

After launch, do not micromanage waiting agents. Poll on a bounded cadence tied
to roadmap milestones, then intervene only on final status, blocked/stalled
evidence, dirty diff mismatch, retry cap, or explicit user request. Do not send
repeated nudges just because a background thread is quiet.

## Phase 0 — Base preparation (orchestrator + codex baseline)

Before any parallel work, get clean, codex-writable bases for the durable
implementation threads.

### 0a. Orchestrator creates the worktree(s) (do NOT delegate to codex)

Place each durable implementation worktree **inside the project** at
`<repo>/.worktrees/<topic>` so codex's sandbox accepts writes to it (see Pitfall
1 in `pitfalls.md` for why sibling-path worktrees fail). `<topic>` is the
issue/topic identifier (e.g. `permissions-mixin`).

Run [scripts/worktree-setup.sh](../scripts/worktree-setup.sh) from the repo root
for each durable implementation topic:

```bash
<skill-dir>/scripts/worktree-setup.sh <topic> <base-branch> [sibling-dep ...]
```

It creates `.worktrees/<topic>` on branch `refactor/<topic>` (override with
`TOPIC_BRANCH=<branch>`), keeps `/.worktrees/` in `.gitignore` idempotently,
symlinks any listed sibling deps so `pyproject.toml: path = "../<dep>"` resolves
inside the worktree, and prints the absolute worktree path to hand to codex.

### 0b. Codex green-base pass

Run the green-base pass **inside each implementation worktree** before that
thread starts coding:

- Merge the upstream base branch (e.g. `origin/main`) into the topic branch
- Resolve conflicts
- Run the project test/lint/typecheck commands and confirm green
- Push (orchestrator handles push if codex's network is restricted — Pitfall 2)

Do not start Phase 1 for a topic until its base is green. If the base is
unstable, you waste codex turns chasing pre-existing failures.

## Phase 1 — Parallel topics

For each issue/topic implementation slice, use a durable Codex thread/worktree
for the implementer when the work needs isolation or user-owned history. **Topics
can run in parallel between each other** when file ownership is disjoint. Within
one topic, the gates remain serial: implementer → cold review → fix if needed →
cold PASS.

### Implementer codex (background)

Launch with `Agent(subagent_type="codex:codex-rescue", run_in_background=true)`.

The prompt must include:

- Worktree path and a clear "do not cd out of this directory" instruction
- Concrete file list the topic should touch
- TDD steps written out explicitly: write RED test → confirm FAIL → implement → confirm GREEN → refactor
- Scope guard: list what's **out of scope** for this topic (other topics' files)
- "Do not commit" — leave the working tree dirty so the orchestrator can serialize commits
- Required final report shape (status, files_changed, test_result, notes)

### Cold reviewer (subagent, parent spot check, or Codex thread)

Run a cold review after the implementer reports done and the orchestrator has
verified the expected diff shape. Choose the smallest adequate transport:

- Parent read-only spot check for small diffs when the parent did not author the
  change and can judge the diff/evidence cold enough.
- Subagent for bounded read-only review, small follow-up checks, or narrow
  verification passes.
- Codex thread only when the review is broad, stateful, long-running, or needs
  its own isolated worktree/history.

Regardless of transport, the prompt/check must include:

- Path to look at + reference files (existing convention examples to compare against)
- A set of review observations to look for (API design, edge cases, test coverage, convention drift, etc.)
- "Code is read-only" — the reviewer must not edit
- Output as a JSON array of `{file, line, severity, issue, suggestion}` so the orchestrator can pipe it directly into the fixer prompt

### Fixer (smallest adequate author transport)

If review finds required fixes, route them to the smallest adequate author
transport: the implementation Codex thread for local follow-up, a fixer subagent
for a short bounded patch, or a separate Codex thread when the fix is broad or
needs its own dirty worktree/history. The fixer receives the reviewer's JSON
verbatim plus a clear "apply these, re-run tests, don't commit" instruction.
Same TDD discipline applies for each fix.

After each fix, run another cold review or parent read-only spot check before
PASS. After required review/fix passes complete **and the orchestrator has
verified the worktree**, move on to the topic's commit.

## Phase 2 — Integration gate + commits (sequential, orchestrator only)

When all topics have cold-review PASS:

1. Run the full project test suite, full lint, full typecheck. **Also run the CI lint command** if it differs from the local lint (Pitfall 5).
2. Commit one topic at a time so the history reflects the orchestration shape — each commit message references the topic and review pass.
3. Push.
4. Verify CI on the remote and only declare done when CI is green.
5. Remove completed worktree(s).

## Verification gates

Don't skip these. Each one has paid for itself at least once.

| Gate | When | What to check |
|---|---|---|
| 0 | End of Phase -1 | Acceptance objective is externally checkable; roadmap milestones have exit criteria and retry caps |
| 1 | End of Phase 0 | Full `<test>`, `<lint>`, `<typecheck>` all green before launching Phase 1 |
| 2 | After each delegated completion | `git status` + `git diff --stat`. Does the changed-file list match what you asked for? |
| 3 | After each fix pass | Scoped tests for the topic pass and unresolved required findings are gone |
| 4 | Before push (Phase 2) | Full project test + lint + typecheck **+ CI-equivalent format check** (Pitfall 5) |
| 5 | After push | Wait for CI, confirm green. If red, fix and re-push before declaring done |

## Commit serialization

Concurrent author transports in the same worktree can race on `.git/index.lock`. To prevent this:

- **Each author transport is instructed to NOT commit.** They leave the worktree dirty.
- **Orchestrator commits per topic** after cold-review PASS, one at a time.
- This also produces a clean per-topic history: one commit per topic, easy to bisect or revert.

Commit message convention (adapt to project):

```
<type>(<scope>): <one-line summary>

- <change 1>
- <change 2>

Addresses post-review follow-up on PR #<N> (T<topic-id>).
```

## Worktree cleanup

After push + CI green, run [scripts/worktree-teardown.sh](../scripts/worktree-teardown.sh) from the repo root:

```bash
<skill-dir>/scripts/worktree-teardown.sh <name> [topic-branch]
```

It verifies the worktree's `.git` pointer first (Pitfall 4): intact → `git worktree remove`; rewritten by a clone-pivoted codex → `rm -rf` + `git worktree prune`. Either way it deletes the topic branch.

The `.worktrees/` directory itself and the sibling-dep symlinks inside it can be left in place across orchestrations — they're cheap to keep and save the symlink setup next time. `.gitignore` keeps them invisible to git.
