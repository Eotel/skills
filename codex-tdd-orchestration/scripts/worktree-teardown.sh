#!/usr/bin/env bash
# Usage: worktree-teardown.sh <name> [topic-branch]
# Run from the repo root after push + CI green. Verifies the worktree's .git
# pointer before removing: a mismatch means a codex session pivoted to a clone
# (Pitfall 4), in which case the directory is force-removed and pruned instead.
set -euo pipefail

name="${1:?usage: worktree-teardown.sh <name> [topic-branch]}"
branch="${2:-refactor/$name}"

repo="$(git rev-parse --show-toplevel)"
wt="$repo/.worktrees/$name"
expected="gitdir: $repo/.git/worktrees/$name"

if [ ! -e "$wt" ]; then
  echo "no worktree at $wt" >&2
  exit 1
fi

actual="$(cat "$wt/.git" 2>/dev/null || true)"
if [ "$actual" = "$expected" ]; then
  git worktree remove "$wt"
else
  echo "pointer mismatch — expected '$expected', got '${actual:-<.git is a directory: clone pivot>}'" >&2
  echo "removing via rm -rf + prune; any stray gitdir it pointed to is left for manual cleanup" >&2
  rm -rf "$wt"
  git worktree prune
fi

git branch -D "$branch"
