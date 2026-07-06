---
name: worker-contract
description: Standard prompt contract for parallel worker agents sharing a codebase — role, file-ownership allowlist, do-not-revert-others rule, and structured findings format. Use when spawning 2+ concurrent implementation agents (Codex sessions, Claude subagents) that touch the same repository, or when the user asks for 並列worker / worker を分けて / サブエージェント最大展開.
---

# Worker Contract

The standard contract for any concurrently-running worker that edits a shared
codebase. Retyped near-verbatim dozens of times before codification; its absence
produced the worst incidents on record (concurrent agents deleting each other's
files — three severity-3 corrections in one session).

## The contract (inject into every worker prompt)

```
You are <Worker NAME> working on <one-line role>.

OWNERSHIP — you may create/edit ONLY these files:
- <explicit path list — derive programmatically: git status --porcelain + prefix
  filter, or the topic's planned file set. Hand-enumeration misses entries.>

YOU ARE NOT ALONE IN THE CODEBASE:
- Other agents are editing other files concurrently. Files you did not author
  are NOT noise: do not delete, revert, or "fix" them — even if they look wrong,
  even if they break your build. Report the conflict instead and STOP.
- Untracked files belong to someone. Never remove them.
- If an import you need is missing because another worker owns that file,
  report the dependency; do not implement it yourself.

BOUNDARIES:
- Never cd outside <worktree>. Never run git add/commit/checkout/push,
  never --no-verify. The orchestrator owns git.
- Formatters/linters in check-only mode; do not let autofix touch files outside
  your ownership list.

FIRST COMMAND (mandatory): cd <worktree> && pwd && git log -1 --oneline
— STOP immediately if this is not <expected worktree/branch>.

FINAL REPORT (fixed schema, short):
status: GREEN | RED | BLOCKED
files_changed: [...]
commands_run: <command + pasted output tail>
blocked_by: <or "none">
notes: <1-3 lines>
```

## Orchestrator-side obligations

- One worker per topic, topics file-disjoint (serialize if sets intersect).
- Verify every report against `git -C <worktree> diff --stat` — empty diff +
  GREEN is a hard failure, redispatch.
- Serialize commits/merges through the orchestrator.
- Default one delegated session per discovered bug; share a session only when
  the bugs' file scopes are adjacent (user-memorized nuance).
- Prepend the standing dispatch preamble from `codex-orchestration-core` for
  Codex sandbox sessions.
