#!/usr/bin/env python3
"""Print the rm_check.py arguments for a merged PR, read with gh on a Mac where gh works.

A remote Mac whose gh cannot read GitHub runs rm_check.py with these arguments:
`--pr-head` is the merged PR's head and `--head-ahead-of-pr` how far the checkout's
HEAD is past it on GitHub. Nothing is printed for a PR that is not MERGED or for a
HEAD that GitHub does not have.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

REPO = re.compile(r"[\w.-]+/[\w.-]+")
SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def gh_json(*args: str) -> dict:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="GitHub repository as owner/name")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument("--head", required=True, help="full SHA of the checkout's HEAD on the other Mac")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo):
        parser.error("--repo must be owner/name")
    if not SHA.fullmatch(args.head):
        parser.error("--head must be a full commit SHA")
    try:
        pr = gh_json("pr", "view", str(args.pr), "-R", args.repo, "--json", "state,headRefOid")
        if pr.get("state") != "MERGED":
            print(f"PR #{args.pr} is {pr.get('state')}, not MERGED: rm_check.py needs no PR head from here",
                  file=sys.stderr)
            return 1
        compare = gh_json("api", f"repos/{args.repo}/compare/{pr['headRefOid']}...{args.head}")
    except subprocess.CalledProcessError as error:
        print(f"gh failed: {(error.stderr or '').strip() or error}; a HEAD GitHub does not have is unpushed work",
              file=sys.stderr)
        return 1
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
        print(f"gh failed: {error}", file=sys.stderr)
        return 1
    print(f"--pr-head {pr['headRefOid']} --head-ahead-of-pr {int(compare['ahead_by'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
