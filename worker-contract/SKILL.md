---
name: worker-contract
description: Define ownership, safety, and reporting rules for concurrent agents editing one repository. Use with two or more implementation workers.
---

# Worker Contract

Use this contract for concurrent writers in a shared repository.

## The contract (inject into every worker prompt)

```
You are <Worker NAME> working on <one-line role>.

OWNERSHIP — you may create/edit ONLY these files:
- <explicit path list derived from the planned slice and current repository state>

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
  the file ownership and fix are adjacent.
- Add only environment facts and guardrails relevant to the current worker.
