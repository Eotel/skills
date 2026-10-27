#!/usr/bin/env python3
"""Pick the PR body template for a checkout, and check a body against its headings.

pick: the repository's own pull request template wins. Otherwise the repository a PR goes to
(a fork's parent, else origin) gets review-first when the signed-in GitHub account or one of its
organizations owns it, and show-me, the upstream template, when anyone else does.

check: the body must contain the template's top-level headings in order, none empty.

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

_REMOTE_RE = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/]+)$")
_REPO_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# CommonMark fenced code: up to 3 spaces of indent, closed by the same character at least as
# long; an unclosed fence runs to the end. A backtick fence line holds no other backtick.
_FENCE_RE = re.compile(
    r"^ {0,3}(?P<fence>(?P<char>[`~])(?P=char){2,})(?!(?<=`)[^\n]*`)[^\n]*$.*?"
    r"(?:^ {0,3}(?P=fence)(?P=char)*[ \t]*$|\Z)",
    re.DOTALL | re.MULTILINE,
)
_HEADING_RE = re.compile(
    r"^ {0,3}(?P<marks>#{1,6})[ \t]+(?P<title>.+?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE
)


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
    def level(self) -> int | None:
        """The shallowest heading level the template uses, if it has headings."""
        levels = [len(heading["marks"]) for heading in _headings(self.text)]
        return min(levels, default=None)

    @property
    def headings(self) -> tuple[str, ...]:
        """The template's headings at its shallowest level, in order."""
        level = self.level
        if level is None:
            return ()
        return tuple(section.title for section in parse_sections(self.text, level))

    @property
    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")


def _visible(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n")
    return _FENCE_RE.sub("\0code\0", _COMMENT_RE.sub("", text))


def _headings(markdown: str) -> tuple[re.Match[str], ...]:
    return tuple(_HEADING_RE.finditer(_visible(markdown)))


def parse_sections(markdown: str, level: int) -> tuple[Section, ...]:
    """Sections headed at `level`; a deeper heading stays inside its section.

    Code blocks never open a section.
    """
    visible = _visible(markdown)
    headings = tuple(
        heading
        for heading in _HEADING_RE.finditer(visible)
        if len(heading["marks"]) <= level
    )
    ends = (*(heading.start() for heading in headings[1:]), len(visible))
    return tuple(
        Section(heading["title"], visible[heading.end() : end].strip())
        for heading, end in zip(headings, ends[: len(headings)], strict=True)
        if len(heading["marks"]) == level
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


def target_owner(repo_dir: Path) -> str:
    """Owner of the repository a PR from this checkout goes to: a fork's parent, else origin."""
    url = _run(("git", "remote", "get-url", "origin"), repo_dir).strip()
    match = _REMOTE_RE.search(url.removesuffix("/").removesuffix(".git"))
    if match is None:
        raise Undecided(f"origin is not a GitHub repository: {url}")
    output = _run(
        ("gh", "api", f"repos/{match['repo']}", "--jq", '.parent.full_name // ""'),
        repo_dir,
    )
    # gh can print debug lines to stdout; keep only an owner/name line.
    parents = [
        line.strip() for line in output.splitlines() if _REPO_RE.fullmatch(line.strip())
    ]
    return (parents[0] if parents else match["repo"]).split("/")[0]


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
    owned = target_owner(repo_dir).lower() in own_owners(repo_dir)
    return builtin("review-first" if owned else "show-me")


def check(body: str, template: Template) -> list[str]:
    """Report missing, misordered, and empty template headings."""
    level = template.level
    if level is None:
        raise Undecided(f"{template.path} has no headings to check against")
    required = template.headings
    marks = "#" * level
    sections = parse_sections(body, level)
    titles = [section.title for section in sections]
    missing = [title for title in required if title not in titles]
    if missing:
        return [f"missing heading: {marks} {title}" for title in missing]
    problems = []
    positions = [titles.index(title) for title in required]
    if positions != sorted(positions):
        problems.append(
            "headings out of template order: "
            + " → ".join(f"{marks} {title}" for title in required)
        )
    bodies = {section.title: section.body for section in reversed(sections)}
    problems += [
        f"empty section: {marks} {title}" for title in required if not bodies[title]
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
