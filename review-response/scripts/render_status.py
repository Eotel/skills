#!/usr/bin/env python3
"""Render caller-supplied review status JSON as a Markdown table."""

import argparse
import json
from pathlib import Path
import re
import sys


REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"


def resolve_repo(value, repos):
    value = repos.get(value, value) if value else None
    return value if value and re.fullmatch(REPO, value) else None


def linkify(text, default_repo=None, repos=None, references=None):
    """Link references using the row repo, aliases and explicit repos in this cell.

    PR#N uses /pull/ and the row repo. Other references use /issues/. Bare #N
    prefers the row repo, then the last explicit repo; /#N continues the latter.
    ASCII word characters before #N block a link; Japanese adjacency is allowed.
    Existing links, inline code and URLs are left intact.
    If supplied, references collects resolved (repo, number) pairs, including
    existing Markdown links to GitHub issues and pull requests.
    """
    repos = repos or {}
    default_repo = resolve_repo(default_repo, repos)
    last_repo = default_repo
    aliases = "|".join(re.escape(key) for key in sorted(set(repos) | {"PR"}, key=len, reverse=True))
    pattern = re.compile(
        r"(?P<protected>\[[^\]\n]*\]\([^\)\n]*\)"
        r"|(?<!`)(?P<backticks>`+)(?!`)[\s\S]*?(?<!`)(?P=backticks)(?!`)"
        r"|https?://[^\s<>]+)"
        rf"|(?<![A-Za-z0-9_.\[/-])(?P<named>{REPO}|{aliases})#(?P<number>[0-9]+)"
        r"|(?<=/)#(?P<continuation>[0-9]+)"
        r"|(?<![A-Za-z0-9_\[/#])#(?P<bare>[0-9]+)"
    )

    def replace(match):
        nonlocal last_repo
        reference = match[0]
        if match["protected"]:
            if references is not None:
                destination = re.match(
                    r"\[[^\]\n]*\]\(\s*(?:<([^<>\n]+)>|([^\s()]+))",
                    reference,
                )
                linked = re.fullmatch(
                    rf"https://github.com/(?P<repo>{REPO})/(?:issues|pull)/(?P<number>[0-9]+)(?:[/?#][^\s<>]*)?",
                    destination[1] or destination[2],
                ) if destination else None
                if linked:
                    references.append((linked["repo"], linked["number"]))
            return reference
        kind = "issues"
        if match["named"]:
            named = match["named"]
            number = match["number"]
            if named == "PR":
                repo, kind = default_repo, "pull"
            else:
                repo = resolve_repo(named, repos)
        elif match["continuation"]:
            repo, number = last_repo, match["continuation"]
        else:
            repo, number = default_repo or last_repo, match["bare"]
        if not repo:
            return reference
        last_repo = repo
        if references is not None:
            references.append((repo, number))
        return f"[{reference}](https://github.com/{repo}/{kind}/{number})"

    return pattern.sub(replace, text)


def read_tracker(path):
    """Read and validate task records before any status output is written."""
    tasks = []
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            task = json.loads(line)
        except ValueError:
            raise ValueError(f"Invalid tracker JSON at line {number}") from None
        if not isinstance(task, dict):
            raise ValueError(f"Invalid tracker record at line {number}: expected a task with boolean completed")
        if task.get("url") in (None, ""):
            continue
        if not isinstance(task.get("completed"), bool):
            raise ValueError(f"Invalid tracker record at line {number}: expected a task with boolean completed")
        for field in ("url", "assignee", "permalink_url"):
            if task.get(field) is not None and not isinstance(task[field], str):
                raise ValueError(f"Invalid tracker record at line {number}: {field} must be text or null")
        tasks.append(task)
    return tasks


def render_status(data, done_prefix="done", tracker=None):
    units = data["units"]
    repos = data.get("repos", {})
    done = sum(unit["state"].startswith(done_prefix) for unit in units)
    lines = [f"# Review response: {done}/{len(units)} done", "",
             "| ID | Repo | Target | What | State | Next |",
             "| --- | --- | --- | --- | --- | --- |"]
    if tracker is not None:
        lines[2] += " Tracker |"
        lines[3] += " --- |"
        tasks = {}
        for task in tracker:
            if task.get("url"):
                tasks.setdefault(task["url"].rstrip("/"), []).append(task)
    for unit in units:
        cells = []
        references = []
        for key in ("id", "repo", "target", "what", "state", "next"):
            cell_references = references if tracker is not None and key in ("target", "what", "next") else None
            cell = linkify(str(unit[key]), unit["repo"], repos, cell_references)
            cells.append(cell.replace("|", r"\|").replace("\r\n", "\n").replace("\n", "<br>"))
        if tracker is not None:
            tracker_entries = []
            for repo, number in dict.fromkeys(references):
                url = f"https://github.com/{repo}/pull/{number}".rstrip("/")
                for task in tasks.get(url, []):
                    label = f"{repo}#{number}"
                    if task.get("permalink_url"):
                        label = f"[{label}]({task['permalink_url']})"
                    assignee = task.get("assignee") or "unassigned"
                    state = "done" if task["completed"] else "open"
                    tracker_entries.append(f"{label} {assignee} · {state}")
            cell = "<br>".join(tracker_entries) or "—"
            cells.append(cell.replace("|", r"\|").replace("\r\n", "\n").replace("\n", "<br>"))
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(["", "Out of scope:"])
    lines.extend("- " + linkify(item, repos=repos) for item in data.get("out_of_scope", []))
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog='JSON keys: units (id/repo/target/what/state/next), out_of_scope, repos (e.g. {"api": "acme/api"}).',
    )
    parser.add_argument("status_file", type=Path, help="input JSON file (read only)")
    parser.add_argument("-o", "--output", type=Path, help="write Markdown here instead of stdout")
    parser.add_argument("--done-prefix", default="done", help="states starting with this prefix count as done (default: done)")
    parser.add_argument("--tracker", type=Path, metavar="FILE", help="add a Tracker column from task JSON lines")
    args = parser.parse_args(argv)
    try:
        if args.output and (
            args.output.resolve() == args.status_file.resolve()
            or (args.output.exists() and args.output.samefile(args.status_file))
        ):
            raise ValueError("--output must not refer to the input JSON file")
        tracker = None
        if args.tracker:
            tracker = read_tracker(args.tracker)
        rendered = render_status(json.loads(args.status_file.read_text(encoding="utf-8")), args.done_prefix, tracker)
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"render_status: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
