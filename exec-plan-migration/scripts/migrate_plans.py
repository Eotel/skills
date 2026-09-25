#!/usr/bin/env python3
"""Inventory legacy exec plans and remove completed plans once unreferenced."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

PLANS_DIR = "docs/exec-plans"
COMPLETED_DIR = f"{PLANS_DIR}/completed/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root. Defaults to cwd.",
    )
    parser.add_argument(
        "--base",
        default="HEAD",
        help="Ref that branches are compared with. Defaults to HEAD.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="git rm completed plans. Refuses while references remain.",
    )
    return parser.parse_args()


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def tracked_files(root: Path) -> list[str]:
    return git(root, "ls-files", "-z").split("\0")[:-1]


def read_texts(root: Path, files: list[str]) -> dict[str, list[str]]:
    texts = {}
    for relative in files:
        try:
            texts[relative] = (root / relative).read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
    return texts


def find_references(
    texts: dict[str, list[str]], patterns: list[str], exclude: list[str]
) -> list[str]:
    return [
        f"{relative}:{number}: {line.strip()}"
        for relative, lines in texts.items()
        if relative not in exclude
        for number, line in enumerate(lines, start=1)
        if any(pattern in line for pattern in patterns)
    ]


def last_commit_date(root: Path, path: str) -> str:
    return git(root, "log", "-1", "--format=%cs", "--", path).strip() or "uncommitted"


def branches_touching_plans(root: Path, base: str) -> dict[str, list[str]]:
    refs = git(
        root,
        "for-each-ref",
        f"--no-merged={base}",
        "--format=%(refname:short)",
        "refs/heads",
        "refs/remotes",
    ).split()
    touched: dict[str, list[str]] = {}
    for ref in refs:
        if ref.endswith("/HEAD"):
            continue
        changed = git(root, "diff", "--name-only", f"{base}...{ref}", "--", PLANS_DIR)
        for path in changed.split():
            touched.setdefault(path, []).append(ref)
    return touched


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    files = tracked_files(root)
    completed = [path for path in files if path.startswith(COMPLETED_DIR)]

    active = [path for path in files if path.startswith(f"{PLANS_DIR}/active/")]
    texts = read_texts(root, files)

    print(f"## Completed plans: {len(completed)}")
    print("\n## References to completed plans")
    names = [Path(plan).name for plan in completed]
    references = find_references(texts, [COMPLETED_DIR, *names], completed)
    for hit in references:
        print(hit)

    print(f"\n## Active plans: {len(active)}")
    for plan in active:
        print(f"\n### {plan} (last commit {last_commit_date(root, plan)})")
        for hit in find_references(texts, [Path(plan).name], [plan]):
            print(f"- {hit}")

    print("\n## Unmerged branches touching plans")
    for path, refs in sorted(branches_touching_plans(root, args.base).items()):
        print(f"{path}: {', '.join(refs)}")

    if args.apply and references:
        print("\nRefusing --apply: rewrite the references above first.")
        return 1
    if args.apply and completed:
        git(root, "rm", "-q", "--", *completed)
        print(f"\nRemoved {len(completed)} completed plans with git rm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
