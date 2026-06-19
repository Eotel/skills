# Detailed workflow

## Phase 0 — Base preparation (orchestrator + 1 codex)

Before any parallel work, get a clean, codex-writable base.

### 0a. Orchestrator creates the worktree (do NOT delegate to codex)

Place the worktree **inside the project** at `<repo>/.worktrees/<name>` so codex's sandbox accepts writes to it (see Pitfall 1 in `pitfalls.md` for why sibling-path worktrees fail). Throughout this section `<name>` is the topic identifier (e.g. `permissions-mixin`) — substitute the same value into the branch name.

```bash
mkdir -p .worktrees
# Idempotent: only append the line if it's not already there.
grep -qxF '/.worktrees/' .gitignore 2>/dev/null || echo '/.worktrees/' >> .gitignore

git fetch origin <base-branch>
git worktree add .worktrees/<name> origin/<base-branch> -b refactor/<name>
```

If the project uses local sibling deps (`pyproject.toml: path = "../<dep>"`), set up symlinks once so `uv sync` / `poetry install` works inside the worktree:

```bash
for dep in <sibling-dep-1> <sibling-dep-2> ...; do
  ln -sfn ../../$dep .worktrees/$dep
done
```

### 0b. Codex green-base pass (1 session)

A single codex session, run **inside the new worktree**, establishes the green baseline:

- Merge the upstream base branch (e.g. `origin/main`) into the topic branch
- Resolve conflicts
- Run the project test/lint/typecheck commands and confirm green
- Push (orchestrator handles push if codex's network is restricted — Pitfall 2)

Do not start Phase 1 until the base is green. Subsequent topics build on this; if the base is unstable, you waste codex turns chasing pre-existing failures.

## Phase 1 — Parallel topics

For each topic, run three codex sessions in this order. **Topics can run in parallel between each other**, but within one topic the three roles are strictly serial.

### Implementer codex (background)

Launch with `Agent(subagent_type="codex:codex-rescue", run_in_background=true)`.

The prompt must include:

- Worktree path and a clear "do not cd out of this directory" instruction
- Concrete file list the topic should touch
- TDD steps written out explicitly: write RED test → confirm FAIL → implement → confirm GREEN → refactor
- Scope guard: list what's **out of scope** for this topic (other topics' files)
- "Do not commit" — leave the working tree dirty so the orchestrator can serialize commits
- Required final report shape (status, files_changed, test_result, notes)

### Reviewer codex (background, separate session)

Launch as a fresh agent after the implementer reports done. Prompt must include:

- Path to look at + reference files (existing convention examples to compare against)
- A set of review observations to look for (API design, edge cases, test coverage, convention drift, etc.)
- "Code is read-only" — the reviewer must not edit
- Output as a JSON array of `{file, line, severity, issue, suggestion}` so the orchestrator can pipe it directly into the fixer prompt

### Fixer codex (background, separate session)

Receives the reviewer's JSON verbatim plus a clear "apply these, re-run tests, don't commit" instruction. Same TDD discipline applies for each fix.

After all three sessions complete **and the orchestrator has verified the worktree**, move on to the next topic's commit.

## Phase 2 — Integration gate + commits (sequential, orchestrator only)

When all topics are fixer-complete:

1. Run the full project test suite, full lint, full typecheck. **Also run the CI lint command** if it differs from the local lint (Pitfall 5).
2. Commit one topic at a time so the history reflects the orchestration shape — each commit message references the topic and review pass.
3. Push.
4. Verify CI on the remote and only declare done when CI is green.
5. Remove the worktree.

## Verification gates

Don't skip these. Each one has paid for itself at least once.

| Gate | When | What to check |
|---|---|---|
| 1 | End of Phase 0 | Full `<test>`, `<lint>`, `<typecheck>` all green before launching Phase 1 |
| 2 | After each codex completion | `git status` + `git diff --stat`. Does the changed-file list match what you asked for? |
| 3 | After each fixer | Scoped tests for the topic pass |
| 4 | Before push (Phase 2) | Full project test + lint + typecheck **+ CI-equivalent format check** (Pitfall 5) |
| 5 | After push | Wait for CI, confirm green. If red, fix and re-push before declaring done |

## Commit serialization

Concurrent codex sessions in the same worktree can race on `.git/index.lock`. To prevent this:

- **Each codex is instructed to NOT commit.** They leave the worktree dirty.
- **Orchestrator commits per topic** after fixer verification, one at a time.
- This also produces a clean per-topic history: one commit per topic, easy to bisect or revert.

Commit message convention (adapt to project):

```
<type>(<scope>): <one-line summary>

- <change 1>
- <change 2>

Addresses post-review follow-up on PR #<N> (T<topic-id>).
```

## Worktree cleanup

After push + CI green:

```bash
# Verify worktree pointer integrity first (see Pitfalls #4)
cat <repo>/.worktrees/<name>/.git
# expected: gitdir: <repo>/.git/worktrees/<name>

git -C <repo> worktree remove .worktrees/<name>
git -C <repo> branch -D <topic-branch>
```

If the pointer is wrong (codex pivoted to clone during work), use `rm -rf <repo>/.worktrees/<name>` and `git worktree prune` instead — see `pitfalls.md`.

The `.worktrees/` directory itself and the sibling-dep symlinks inside it can be left in place across orchestrations — they're cheap to keep and save the symlink setup next time. `.gitignore` keeps them invisible to git.
