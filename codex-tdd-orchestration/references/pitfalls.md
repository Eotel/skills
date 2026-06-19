# Pitfalls

Real failure modes encountered when running parallel codex orchestrations. Each one cost time; learn from them.

## Pitfall 1: Codex sandbox blocks `git worktree add` AND rejects writes to worktrees outside its writable_roots

**Symptom A** (worktree creation by codex): Codex starts the task, then silently produces a working tree at the requested path but `git worktree list` from the main repo doesn't show it — codex created a clone instead.

**Symptom B** (writes to a sibling-path worktree): codex reports something like:

```
Blocked by sandbox/write-scope mismatch.
- Requested worktree: /Users/<you>/ghq/.../wt-<name>
- Writable project root for `apply_patch`: /Users/<you>/ghq/.../<repo>
- `apply_patch` rejected edits to the requested worktree as "outside of the project".
```

`apply_patch` is strictly sandboxed to `writable_roots = [<project>, /private/tmp, ...]`. Shell-level writes (`tee`, heredoc, `python -c 'open(...).write(...)'`) may still slip through, but the patch tool refuses. Some codex sessions in the same orchestration will discover the shell workaround on their own; others will give up — you can't rely on it.

**Root cause for A**: Codex sandbox can refuse to write lock files into `.git/refs/heads/`. The codex then **silently pivots to cloning the repo into the worktree path**, breaking the worktree linkage. The clone has its own `.git` directory and remote, so subsequent pushes go to the right place but the orchestrator's view (`git worktree list`) gets stale.

**Root cause for B**: A worktree placed at the canonical `<repo>/../<wt-name>` location is a **sibling** of the project root, not a subdirectory, so codex's writable_roots check fails. The orchestrator sees a valid git worktree; codex sees a forbidden path.

### Mitigation: place worktrees inside the project at `<repo>/.worktrees/<name>`

**Orchestrator creates the worktree** (codex never calls `git worktree add`), and places it **inside the project root** so codex's `apply_patch` accepts it:

```bash
# One-time per repo (idempotent — safe to re-run):
mkdir -p .worktrees
grep -qxF '/.worktrees/' .gitignore 2>/dev/null || echo '/.worktrees/' >> .gitignore

# Per topic / orchestration:
git fetch origin <base-branch>
git worktree add .worktrees/<name> origin/<base-branch> -b <topic-branch>
# Hand the absolute path of the worktree to codex.
# `<repo>` below stands for the actual absolute repo path,
# e.g. /Users/<you>/projects/<repo-name>/.worktrees/<name>.
# Codex's writable_roots must contain that absolute path.
```

Why `.worktrees/` and not `.claude/worktrees/`: `.claude/` is Claude Code's own settings/agents/hooks/commands directory. Dumping a full project tree underneath it confuses harness file scanning (e.g., agent discovery walks `.claude/agents/`). Keep `.worktrees/` at project root so the two concerns stay separate.

Why not `/tmp/<name>` (an earlier version of this skill recommended it): `/private/tmp` IS in codex's writable_roots, so writes succeed, but `..` from `/tmp/<wt>/pyproject.toml` resolves to `/tmp/`, which doesn't contain the project's sibling repos. Local-deps installs (`uv sync` with `path = "../<dep>"` references) then fail and need symlink workarounds. Placing the worktree inside the project sidesteps the relative-path question entirely — see below.

### uv / Poetry local-deps consideration

Many Python projects use local sibling dependencies in `pyproject.toml`:

```toml
[tool.uv.sources]
my-lib = { path = "../my-lib", editable = true }
```

From a worktree at `<repo>/.worktrees/<name>/pyproject.toml`, `../my-lib` resolves to `<repo>/.worktrees/my-lib` — which doesn't exist. Two ways to handle this:

**Option 1 (recommended): symlink siblings into `.worktrees/` once**

```bash
# At <repo>/, run once. The double-`../` makes the symlinks point to the
# actual sibling repos regardless of which worktree under .worktrees/ uses them.
for dep in <sibling-dep-1> <sibling-dep-2> ...; do
  ln -sfn ../../$dep .worktrees/$dep
done
```

After this, any worktree under `.worktrees/` resolves `path = "../<dep>"` to `<repo>/.worktrees/<dep>` → symlink → `<repo>/../<dep>` → the real sibling repo. `uv sync` then succeeds.

**Option 2: rewrite paths in `pyproject.toml`**

Switch `path = "../<dep>"` to absolute paths or `${PROJECT_ROOT}` env-var substitution. Heavier; affects all developers, not just the orchestration.

If codex still complains about lock files even with the worktree inside the project, that's a clearer signal to debug than a silent pivot.

## Pitfall 2: Codex sandbox often blocks `github.com`

**Symptom**: codex reports `error connecting to api.github.com` or `git push` fails with DNS error. `gh pr view/edit/create` fails.

**Root cause**: Codex sandbox restricts network access. `api.github.com` and `github.com` are commonly blocked.

**Mitigation**: **The orchestrator runs all network-bound git/gh operations**. Codex stays inside the worktree doing local work. The orchestrator is responsible for:

- `git push origin <branch>` (and `git push origin <local>:<remote>` if branch names differ)
- `gh pr view/edit/create`
- Anything that needs to talk to github.com / a private registry / external services

Phrase it explicitly in the codex prompt: *"Do not attempt to push. Report when ready; orchestrator will push."*

## Pitfall 3: "Forwarded to Codex companion" ≠ done

**Symptom**: The `Agent` tool result reads:

```
Task forwarded to Codex companion (background ID: xxxx).
Output streaming to <file>.
```

The Claude harness treats the agent as completed. But the actual codex job may still be running, **or may have died silently mid-work without notifying anyone**.

**Root cause**: The `codex:codex-rescue` subagent wraps a codex-companion runtime. The wrapper agent completes when it hands off; the companion's lifecycle is independent. There is no automatic notification when the companion finishes or aborts.

**Mitigation**: When you see this output shape, **don't proceed based on the message alone**. Verify by:

1. Read the target file(s) and check whether the expected changes are present
2. Check `git diff --stat` in the worktree — does the changed-file list match the topic scope?
3. If the companion is still writing, the output file size will grow; if stalled with expected changes absent, re-launch a fresh codex with a "previous session died mid-work" prompt that lists what's already there and what's missing.

**Heuristic**: if the codex output file hasn't grown in ~5 minutes AND the expected changes aren't visible, treat it as dead. Re-launch.

**When re-launching**: state explicitly that a previous session died, list which RED tests are already present, and tell the new codex to focus on the GREEN phase. Don't make it redo work that's already on disk.

## Pitfall 4: Failed codex can rewrite the worktree's `.git` pointer

**Symptom**: After codex work finishes, `git worktree remove` fails with:

```
fatal: validation failed, cannot remove working tree:
'<path>' does not point back to '.git/worktrees/<name>'
```

**Root cause**: If codex pivoted to cloning (Pitfall 1), the worktree's `.git` file now points to a freshly-created standalone gitdir rather than `<main repo>/.git/worktrees/<name>`. The main repo's worktree registration is stale.

**Mitigation**: After the work is done, before cleanup, verify the pointer:

```bash
cat <repo>/.worktrees/<name>/.git
# expected: gitdir: <repo>/.git/worktrees/<name>
# if instead: gitdir: /tmp/<random>  → codex pivoted, use rm -rf flow
```

If the pointer is wrong:

```bash
rm -rf <repo>/.worktrees/<name> /tmp/<random-gitdir-path>
git -C <repo> worktree prune
git -C <repo> branch -D <topic-branch>
```

The commits and push still went through if the standalone clone had the right remote; the cleanup is just unusual.

## Pitfall 5: Local lint passes, CI lint fails

**Symptom**: Orchestrator declares "lint PASS" locally, push succeeds, then CI fails with `Would reformat: <file>.py` or similar formatter errors.

**Root cause**: Project-level lint recipes (e.g. `just lint-all`) often run `ruff check` (style + lint rules) but **do not run `ruff format --check`**. CI usually does both.

**Mitigation**: **Before the final push, run the CI-equivalent format check yourself**, not just whatever the local `just` recipe runs. For ruff-based projects:

```bash
uvx ruff format --check <repo>
```

For other ecosystems: identify the exact CI lint command (read the workflow file) and run it locally before push. Don't trust that the project's `lint-all` recipe is complete.

If CI fails after push: apply `ruff format` (or equivalent), commit as `style: apply <formatter>`, push.

**Better fix**: propose extending the project's `lint-all` recipe to include the formatter check, so the divergence doesn't recur.

## Project-specific env

### macOS Nix/devenv + harfbuzz

Some projects (e.g. those using `weasyprint` for PDF rendering) need `DYLD_LIBRARY_PATH=/opt/homebrew/lib` for `pytest` to load `libharfbuzz`. Codex sandbox does not inherit devenv shell activation reliably.

**Canonical symptom**:

```
OSError: ctypes.util.find_library() did not manage to locate a library called 'libharfbuzz-0'
```

**Mitigation**: pin the env into every codex prompt template:

```
## 検証
DYLD_LIBRARY_PATH=/opt/homebrew/lib just test <scope>
DYLD_LIBRARY_PATH=/opt/homebrew/lib just lint-all
```

When unsure, run one test command early and check for this OSError. If you see it, add the env var.

## General troubleshooting heuristic

When codex reports "DONE" but you can't tell if the work is real:

1. **What did it change?** `git diff --stat` in the worktree
2. **Does that match what you asked?** Compare to the topic's "対象ファイル" list
3. **Do the tests it cited actually exist and pass?** Run them yourself from the orchestrator side
4. **Is the report shape complete?** A truncated report = a truncated task; the codex may have hit a sandbox limit mid-output

Two of those four failing = treat as not-done, re-launch with a "previous session was incomplete" prompt.
