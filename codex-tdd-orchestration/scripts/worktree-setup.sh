#!/usr/bin/env bash
# Usage: worktree-setup.sh <name> <base-branch> [sibling-dep ...]
# Run from the repo root. Creates <repo>/.worktrees/<name> on branch
# refactor/<name> (override with TOPIC_BRANCH=<branch>), keeps .gitignore
# up to date, and symlinks sibling deps so `path = "../<dep>"` resolves.
# Prints the absolute worktree path on success.
set -euo pipefail

name="${1:?usage: worktree-setup.sh <name> <base-branch> [sibling-dep ...]}"
base="${2:?usage: worktree-setup.sh <name> <base-branch> [sibling-dep ...]}"
shift 2
branch="${TOPIC_BRANCH:-refactor/$name}"

repo="$(git rev-parse --show-toplevel)"
cd "$repo"

mkdir -p .worktrees
grep -qxF '/.worktrees/' .gitignore 2>/dev/null || echo '/.worktrees/' >> .gitignore

git fetch origin "$base"
git worktree add ".worktrees/$name" "origin/$base" -b "$branch"

for dep in "$@"; do
  ln -sfn "../../$dep" ".worktrees/$dep"
done

echo "$repo/.worktrees/$name"
