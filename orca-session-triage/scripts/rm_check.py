#!/usr/bin/env python3
"""Check that an Orca worktree is safe to remove."""

import argparse
import filecmp
import json
import os
from pathlib import Path
import subprocess
import sys

CACHE_PARTS = {
    "node_modules", ".venv", ".devenv", ".direnv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".svelte-kit", "apm_modules", ".import_linter_cache", ".moon", "dist", "build",
    "coverage", "test-results", "playwright-report",
}
CACHE_SUFFIXES = (".pyc", ".mo", ".tsbuildinfo")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def is_cache(relpath):
    parts = Path(relpath.rstrip("/")).parts
    return bool(CACHE_PARTS.intersection(parts)) or relpath.endswith(CACHE_SUFFIXES)


def same_tree(left, right):
    if left.is_symlink() or right.is_symlink():
        return left.is_symlink() and right.is_symlink() and os.readlink(left) == os.readlink(right)
    if left.is_file():
        return right.is_file() and filecmp.cmp(left, right, shallow=False)
    if not (left.is_dir() and right.is_dir()):
        return False
    names = {p.name for p in left.iterdir()} | {p.name for p in right.iterdir()}
    return all(same_tree(left / n, right / n) for n in names)


def ignored_for_review(worktree, main):
    """Ignored paths that are neither caches nor identical copies of the main checkout."""
    review = []
    for line in git(worktree, "status", "--porcelain", "--ignored").stdout.splitlines():
        if not line.startswith("!! ") or is_cache(line[3:]):
            continue
        rel = line[3:]
        source, copy = Path(worktree) / rel, Path(main) / rel
        if not copy.exists():
            review.append(rel + " (not in main checkout)")
        elif not same_tree(source, copy):
            review.append(rel + " (differs from main checkout)")
    return review


def compose_in(worktree, compose_projects):
    """Compose projects (from `docker compose ls --all --format json`) defined inside the worktree."""
    prefix = str(Path(worktree).resolve()) + "/"
    found = []
    for project in compose_projects:
        files = [f for f in (project.get("ConfigFiles") or "").split(",") if os.path.isabs(f)]
        if any(str(Path(f).resolve()).startswith(prefix) for f in files):
            found.append({"project": project.get("Name"), "status": project.get("Status")})
    return found


def check(worktree, main, base, compose_projects, pr=None):
    """Return {"ok", "blockers", "review", "containers"} for removing `worktree`."""
    blockers = []
    changes = [line for line in git(worktree, "status", "--porcelain").stdout.splitlines() if line]
    if changes:
        blockers.append("%d uncommitted changes" % len(changes))
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if base is None:
        blockers.append("no base ref found (%s); pass --base" % ", ".join(BASE_CANDIDATES))
    elif git(worktree, "merge-base", "--is-ancestor", head, base).returncode != 0:
        if not pr:
            blockers.append("HEAD %s is not in %s" % (head[:8], base))
        elif pr.get("headRefOid") != head or pr.get("state") != "MERGED":
            blockers.append("HEAD %s is not in %s and PR #%s is %s"
                            % (head[:8], base, pr.get("number"), pr.get("state")))
    review = ignored_for_review(worktree, main)
    return {"ok": not blockers and not review, "blockers": blockers, "review": review,
            "containers": compose_in(worktree, compose_projects)}


def main_checkout(worktree):
    for line in git(worktree, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):]
    return None


BASE_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master")


def default_base(worktree):
    """The first existing remote default branch, or None when the repository has none."""
    for ref in BASE_CANDIDATES:
        if git(worktree, "rev-parse", "--verify", "--quiet", ref).returncode == 0:
            return ref
    return None


def pull_request(worktree, number):
    """Return (pr, error): the PR's number/state/head, or why `gh` could not tell."""
    try:
        out = subprocess.run(["gh", "pr", "view", str(number), "--json", "number,state,headRefOid"],
                             cwd=worktree, capture_output=True, text=True, check=True).stdout
        return json.loads(out), None
    except subprocess.CalledProcessError as error:
        return None, (error.stderr or "").strip() or str(error)
    except (OSError, json.JSONDecodeError) as error:
        return None, str(error)


def compose_projects():
    try:
        out = subprocess.run(["docker", "compose", "ls", "--all", "--format", "json"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return json.loads(out or "[]")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, help="worktree path to check")
    parser.add_argument("--main", help="main checkout to compare ignored files with (default: first `git worktree list` entry)")
    parser.add_argument("--base", help="ref that merged work must be in (default: origin/HEAD)")
    parser.add_argument("--pr", type=int, help="PR number; a MERGED PR whose head is HEAD counts as merged (squash merges)")
    parser.add_argument("--no-docker", action="store_true", help="skip the docker compose project lookup")
    args = parser.parse_args(argv)

    worktree = str(Path(args.worktree).resolve())
    if git(worktree, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        json.dump({"worktree": worktree, "ok": False, "blockers": ["not a git worktree"]}, sys.stdout)
        print()
        return 2
    main = str(Path(args.main or main_checkout(worktree)).resolve())
    base = args.base or default_base(worktree)
    pr, pr_error = pull_request(worktree, args.pr) if args.pr else (None, None)
    projects = [] if args.no_docker else compose_projects()
    result = check(worktree, main, base, projects or [], pr)
    if pr_error:
        result["blockers"].append("PR #%d lookup failed: %s" % (args.pr, pr_error))
        result["ok"] = False
    if projects is None:
        result["notes"] = ["docker compose unavailable: containers not checked"]
    json.dump({"worktree": worktree, "main": main, "base": base, **result}, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
