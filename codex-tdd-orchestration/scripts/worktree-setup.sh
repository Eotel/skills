#!/usr/bin/env bash
# Usage: worktree-setup.sh <name> <base-branch> [sibling-dep ...]
# Run from the repo root. Creates <repo>/.worktrees/<name> on branch
# refactor/<name> (override with TOPIC_BRANCH=<branch>), keeps .gitignore
# up to date, and symlinks sibling deps so `path = "../<dep>"` resolves.
# WORKTREE_LOCAL_FILES: whitespace-separated repo-relative files to copy.
# WORKTREE_SMOKE: optional command to run in the new worktree; failure aborts.
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

worktree="$repo/.worktrees/$name"
if [[ -n "${WORKTREE_LOCAL_FILES:-}" ]]; then
  while IFS= read -r local_file; do
    [[ -n "$local_file" ]] || continue
    case "$local_file" in
      /*|..|../*|*/../*|*/..)
        echo "WORKTREE_LOCAL_FILES requires repo-relative paths without '..': $local_file" >&2
        exit 1
        ;;
    esac
    if [[ -f "$repo/$local_file" ]]; then
      mkdir -p "$(dirname "$worktree/$local_file")"
      cp "$repo/$local_file" "$worktree/$local_file"
    else
      echo "WORKTREE_LOCAL_FILES missing file: $local_file (skipped)" >&2
    fi
  done < <(printf '%s\n' "$WORKTREE_LOCAL_FILES" | tr '[:space:]' '\n')
fi

if [[ -n "${WORKTREE_SMOKE:-}" ]]; then
  if (cd "$worktree" && bash -o errexit -o pipefail -c "$WORKTREE_SMOKE"); then
    :
  else
    smoke_status=$?
    echo "WORKTREE_SMOKE failed in $worktree (exit $smoke_status)" >&2
    exit "$smoke_status"
  fi
fi

echo "$worktree"
