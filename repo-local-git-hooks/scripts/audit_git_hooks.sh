#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: audit_git_hooks.sh [--fix]

Audits whether Git is using this repository's .git/hooks directory.
With --fix, runs the repo's hook installer and writes a repo-local
core.hooksPath override to the Git common hooks directory.
USAGE
}

fix=false
if [ "${1:-}" = "--fix" ]; then
  fix=true
elif [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
elif [ $# -gt 0 ]; then
  usage >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$repo_root" ]; then
  echo "not a git repository" >&2
  exit 2
fi

cd "$repo_root"

git_common_dir="$(git rev-parse --git-common-dir)"
expected_hooks_dir="${git_common_dir}/hooks"
effective_hooks_dir="$(git rev-parse --git-path hooks)"

echo "repo: $repo_root"
echo "expected hooks dir: $expected_hooks_dir"
echo "effective hooks dir: $effective_hooks_dir"
echo
echo "core.hooksPath values:"
git config --show-origin --get-all core.hooksPath 2>/dev/null || echo "  <none>"
echo

bypassed=false
if [ "$effective_hooks_dir" != "$expected_hooks_dir" ]; then
  bypassed=true
  echo "FAIL: Git is not using this repo's hooks directory."
else
  echo "OK: Git resolves hooks to this repo's hooks directory."
fi

if git config --show-origin --get-all core.hooksPath 2>/dev/null \
  | grep -Eq '(/nix/store|\.config/git/config|/etc/gitconfig)'; then
  echo "note: core.hooksPath looks like a global Nix/home-manager hook override."
  echo "note: this is Git config state, not a repo devenv hook definition."
fi

expects_pre_push=false
if [ -f ".pre-commit-config.yaml" ] && grep -q 'pre-push' ".pre-commit-config.yaml"; then
  expects_pre_push=true
elif [ -f ".pre-commit-config.yml" ] && grep -q 'pre-push' ".pre-commit-config.yml"; then
  expects_pre_push=true
elif [ -f "lefthook.yml" ] && grep -q '^pre-push:' "lefthook.yml"; then
  expects_pre_push=true
elif [ -f ".lefthook.yml" ] && grep -q '^pre-push:' ".lefthook.yml"; then
  expects_pre_push=true
fi

if [ -f ".pre-commit-config.yaml" ] || [ -f ".pre-commit-config.yml" ]; then
  expects_pre_commit=true
else
  expects_pre_commit=false
fi

if [ "$expects_pre_commit" = true ] && [ ! -f "$expected_hooks_dir/pre-commit" ]; then
  echo "WARN: $expected_hooks_dir/pre-commit is missing."
fi

if [ "$expects_pre_push" = true ] && [ ! -f "$expected_hooks_dir/pre-push" ]; then
  echo "WARN: $expected_hooks_dir/pre-push is missing."
fi

if [ "$fix" = false ]; then
  if [ "$bypassed" = true ]; then
    exit 1
  fi
  exit 0
fi

echo
echo "fix: installing repo-local hooks"

git config --local --unset-all core.hooksPath || true

if command -v just >/dev/null 2>&1 && just --list 2>/dev/null | grep -q '^    install-hooks'; then
  GIT_CONFIG_GLOBAL=/dev/null just install-hooks
elif command -v just >/dev/null 2>&1 && just --list 2>/dev/null | grep -q '^    pre-commit-install'; then
  GIT_CONFIG_GLOBAL=/dev/null just pre-commit-install
elif command -v prek >/dev/null 2>&1; then
  GIT_CONFIG_GLOBAL=/dev/null prek install
elif command -v pre-commit >/dev/null 2>&1; then
  GIT_CONFIG_GLOBAL=/dev/null pre-commit install
  GIT_CONFIG_GLOBAL=/dev/null pre-commit install --hook-type pre-push
elif command -v uvx >/dev/null 2>&1; then
  GIT_CONFIG_GLOBAL=/dev/null uvx pre-commit install
  GIT_CONFIG_GLOBAL=/dev/null uvx pre-commit install --hook-type pre-push
else
  echo "no supported hook installer found: need just, prek, pre-commit, or uvx" >&2
  exit 1
fi

git config --local core.hooksPath "$expected_hooks_dir"

echo
echo "post-fix effective hooks dir: $(git rev-parse --git-path hooks)"
test "$(git rev-parse --git-path hooks)" = "$expected_hooks_dir"
