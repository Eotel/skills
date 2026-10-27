#!/usr/bin/env python3
"""Pick the PR body template for a checkout, and check a body against its headings.

pick: the repository's own pull request template wins. Otherwise a repository owned by the
signed-in GitHub account or one of its organizations gets review-first, and any other
repository gets show-me, the upstream template.

check: the body must contain the picked template's level-2 headings in order, none empty.

Exit status: 0 ok, 1 the body does not follow the template, 2 the template cannot be decided.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

Kind = Literal["repo", "review-first", "show-me"]
BuiltinKind = Literal["review-first", "show-me"]

ASSETS = Path(__file__).resolve().parent.parent / "assets"
# GitHub reads the default PR template from the root, docs/, or .github/, in any letter case.
TEMPLATE_DIRS = (".", "docs", ".github")
TEMPLATE_NAME = "pull_request_template.md"

_REMOTE_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/[^/]+$")
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# CommonMark fenced code: up to 3 spaces of indent, closed by the same character at least as
# long; an unclosed fence runs to the end.
_FENCE_RE = re.compile(
    r"^ {0,3}(?P<fence>(?P<char>[`~])(?P=char){2,})[^\n]*$.*?"
    r"(?:^ {0,3}(?P=fence)(?P=char)*[ \t]*$|\Z)",
    re.DOTALL | re.MULTILINE,
)
_HEADING_RE = re.compile(r"^## +(?P<title>.+?)[ \t]*$", re.MULTILINE)


class Undecided(Exception):
    """The checkout does not tell which template applies."""


@dataclass(frozen=True)
class Section:
    title: str
    body: str


@dataclass(frozen=True)
class Template:
    kind: Kind
    path: Path

    @property
    def headings(self) -> tuple[str, ...]:
        """Level-2 headings of the template, in order."""
        text = self.path.read_text(encoding="utf-8")
        return tuple(section.title for section in parse_sections(text))


def parse_sections(markdown: str) -> tuple[Section, ...]:
    """Split visible Markdown into level-2 sections; code blocks never open one."""
    text = markdown.replace("\r\n", "\n")
    visible = _FENCE_RE.sub("\0code\0", _COMMENT_RE.sub("", text))
    headings = tuple(_HEADING_RE.finditer(visible))
    ends = (*(heading.start() for heading in headings[1:]), len(visible))
    return tuple(
        Section(heading["title"], visible[heading.end() : end].strip())
        for heading, end in zip(headings, ends, strict=True)
    )


def _run(command: tuple[str, ...], cwd: Path) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as error:
        raise Undecided(f"{command[0]} is not installed") from error
    if result.returncode != 0:
        raise Undecided(f"{' '.join(command)}: {result.stderr.strip()}")
    return result.stdout


def repo_template(repo_dir: Path) -> Path | None:
    """The repository's default PR template, if it has one."""
    found = sorted(
        path
        for directory in (repo_dir / name for name in TEMPLATE_DIRS)
        if directory.is_dir()
        for path in directory.iterdir()
        if path.is_file() and path.name.lower() == TEMPLATE_NAME
    )
    return found[0] if found else None


def github_owner(repo_dir: Path) -> str:
    """Owner of the checkout's origin on GitHub."""
    url = _run(("git", "remote", "get-url", "origin"), repo_dir).strip()
    match = _REMOTE_RE.search(url.removesuffix("/").removesuffix(".git"))
    if match is None:
        raise Undecided(f"origin is not a GitHub repository: {url}")
    return match["owner"]


def own_owners(repo_dir: Path) -> frozenset[str]:
    """The signed-in account and its organizations, lowercased."""
    output = _run(("gh", "api", "user", "--jq", ".login"), repo_dir) + _run(
        ("gh", "api", "user/orgs", "--paginate", "--jq", ".[].login"), repo_dir
    )
    # gh can print debug lines to stdout; keep only lines that are logins.
    return frozenset(
        line.strip().lower()
        for line in output.splitlines()
        if _LOGIN_RE.fullmatch(line.strip())
    )


def builtin(kind: BuiltinKind) -> Template:
    return Template(kind, ASSETS / f"{kind}.md")


def pick(repo_dir: Path) -> Template:
    """Choose the template the body must follow."""
    path = repo_template(repo_dir)
    if path is not None:
        return Template("repo", path)
    owned = github_owner(repo_dir).lower() in own_owners(repo_dir)
    return builtin("review-first" if owned else "show-me")


def check(body: str, template: Template) -> list[str]:
    """Report missing, misordered, and empty template headings."""
    required = template.headings
    if not required:
        raise Undecided(f"{template.path} has no level-2 headings to check against")
    sections = parse_sections(body)
    titles = [section.title for section in sections]
    missing = [title for title in required if title not in titles]
    if missing:
        return [f"missing heading: ## {title}" for title in missing]
    problems = []
    positions = [titles.index(title) for title in required]
    if positions != sorted(positions):
        problems.append(
            "headings out of template order: "
            + " → ".join(f"## {title}" for title in required)
        )
    bodies = {section.title: section.body for section in reversed(sections)}
    problems += [
        f"empty section: ## {title}" for title in required if not bodies[title]
    ]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pick_parser = commands.add_parser("pick", help="print the template as JSON")
    pick_parser.add_argument("--repo-dir", type=Path, default=Path.cwd())
    check_parser = commands.add_parser("check", help="check a body file, - for stdin")
    check_parser.add_argument("body")
    check_parser.add_argument("--repo-dir", type=Path, default=Path.cwd())
    check_parser.add_argument(
        "--kind",
        choices=get_args(BuiltinKind),
        help="use this built-in template instead of picking one",
    )
    args = parser.parse_args(argv)
    try:
        template = (
            builtin(args.kind) if getattr(args, "kind", None) else pick(args.repo_dir)
        )
        if args.command == "pick":
            print(
                json.dumps(
                    {
                        "kind": template.kind,
                        "template": str(template.path),
                        "headings": template.headings,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        body = (
            sys.stdin.read()
            if args.body == "-"
            else Path(args.body).read_text(encoding="utf-8")
        )
        problems = check(body, template)
    except Undecided as error:
        print(f"cannot decide the template: {error}", file=sys.stderr)
        return 2
    print("\n".join(problems) or f"ok: the body follows {template.path}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
