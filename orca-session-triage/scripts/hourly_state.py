#!/usr/bin/env python3
"""Keep decisions shared by successive hourly triage sessions in one JSON file.

Use --state-file for an isolated copy. Mutations lock beside the file and replace
it atomically, so a waiting session and a new run cannot lose each other's edits.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

import scan_sessions

DEFAULT_STATE = "~/.cache/orca-session-triage/hourly-state.json"
# Beside the state file: {"repos": ["owner/name", ...]} whose open issues become requests.
ISSUE_REPOS = "issue-repos.json"
REPO_SLUG = re.compile(r"[\w.-]+/[\w.-]+")


@dataclass(frozen=True)
class Issue:
    repo: str
    number: int
    title: str
    url: str
    closing_prs: frozenset[int]
    assignees: tuple[str, ...]


@dataclass(frozen=True)
class Checkout:
    """An Orca worktree and the GitHub issues and PRs Orca links to it."""
    identity: str
    repo: str  # owner/name, or Orca's repository name when the owner is unknown
    issues: frozenset[int]
    prs: frozenset[int]

    def works_on(self, issue: Issue) -> bool:
        same_repo = self.repo == issue.repo or ("/" not in self.repo and self.repo == issue.repo.split("/")[1])
        return same_repo and (issue.number in self.issues or bool(self.prs & issue.closing_prs))


def issue_repos(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, list) or not all(isinstance(repo, str) and REPO_SLUG.fullmatch(repo) for repo in repos):
        raise ValueError(f'{path} must hold {{"repos": ["owner/name", ...]}}')
    return repos


def gh_open_issues(repo: str) -> list[dict]:
    out = subprocess.run(["gh", "issue", "list", "-R", repo, "--state", "open", "--limit", "1000",
                          "--json", "number,title,url,assignees,closedByPullRequestsReferences"],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out)


def orca_worktrees(orca: str) -> list[dict]:
    return scan_sessions.collect_worktrees_and_terminals(orca)[0]


def origin_slug(path: str) -> str | None:
    try:
        origin = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return scan_sessions.github_slug(origin.stdout.strip()) if origin.returncode == 0 else None


def checkout_of(worktree: dict) -> Checkout:
    linked = worktree.get("linkedPR") or {}
    found = scan_sessions.PR_URL.search(linked.get("url") or "")
    slug = found.group(1) if found else None if worktree.get("_environment") else origin_slug(worktree["path"])
    items = [item for item in worktree.get("linkedItems") or [] if isinstance(item, dict)]
    return Checkout(
        identity=scan_sessions.row_identity({"environment": worktree.get("_environment"), "path": worktree["path"]}),
        repo=slug or worktree.get("repo") or "",
        issues=frozenset([*([worktree["linkedIssue"]] if worktree.get("linkedIssue") else []),
                          *(item.get("number") for item in items if item.get("type") == "issue")]),
        prs=frozenset([*([linked["number"]] if linked.get("number") else []),
                       *(item.get("number") for item in items if item.get("type") == "pr")]))


def issue_intake(state: Path, orca: str) -> tuple[list[Issue], list[Checkout]] | None:
    """Open issues of the listed repositories and the checkouts working on issues, or None when none are listed.

    A repository gh cannot read is left out with a warning; without Orca's worktrees nothing is imported.
    """
    repos = issue_repos(state.parent / ISSUE_REPOS)
    if not repos:
        return None
    try:
        checkouts = [checkout_of(worktree) for worktree in orca_worktrees(orca)]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        print(f"hourly state: open issues not imported, Orca worktrees unreadable: {error}", file=sys.stderr)
        return None
    issues = []
    for repo in repos:
        try:
            issues += [Issue(repo=repo, number=item["number"], title=item["title"], url=item["url"],
                             closing_prs=frozenset(pr["number"] for pr in item.get("closedByPullRequestsReferences") or []),
                             assignees=tuple(person["login"] for person in item.get("assignees") or []))
                       for item in gh_open_issues(repo)]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            print(f"hourly state: open issues of {repo} not imported: {error}", file=sys.stderr)
    return issues, checkouts


def import_issues(data: dict, issues: list[Issue], checkouts: list[Checkout]) -> None:
    """Record each open issue as a request once; an ownerless one gets the checkout that works on it."""
    recorded = {request["source"].get("url"): request for request in data["requests"].values()
                if isinstance(request.get("source"), dict)}
    for issue in issues:
        owner = next((checkout.identity for checkout in checkouts if checkout.works_on(issue)), None)
        request = recorded.get(issue.url)
        if request is None:
            data["requests"][uuid.uuid4().hex[:16]] = {
                "original": f"{issue.title}\n{issue.url}", "symptom": f"{issue.repo}#{issue.number} is open",
                "owner": owner, "opened_at": stamp(), "closed_at": None, "closure_evidence": None,
                "source": {"kind": "github_issue", "url": issue.url, "assignees": list(issue.assignees)}}
        elif request["closed_at"] is None and request["owner"] is None and owner:
            request["owner"] = owner


def read(path):
    if not path.exists():
        return {"version": 1, "left": {}, "questions": {}, "handoffs": {}, "requests": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or any(
            not isinstance(data.get(key), dict) for key in ("left", "questions", "handoffs")) or not isinstance(
                data.get("requests", {}), dict):
        raise ValueError(f"invalid hourly state: {path}")
    data.setdefault("requests", {})
    return data


def save(path, data):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as output:
        temp = Path(output.name)
        try:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    temp.replace(path)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recorded_at(value):
    if value is None:
        return stamp()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs a timezone offset")
    return parsed.isoformat()


def identifier(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:16]


def file_text(path):
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"empty text file: {path}")
    return value


def owner_name(value):
    owner = value.strip()
    environment, separator, path = owner.partition("::")
    if not owner or not Path(path if separator else owner).is_absolute() or (separator and not environment):
        raise ValueError("owner must be a scan row identity: PATH or ENVIRONMENT::PATH")
    return owner


def change(data, args):
    if args.action == "request-open":
        key = uuid.uuid4().hex[:16]
        data["requests"][key] = {"original": file_text(args.original_file),
                                 "symptom": file_text(args.symptom_file),
                                 "owner": owner_name(args.owner) if args.owner is not None else None,
                                 "opened_at": recorded_at(args.opened_at),
                                 "closed_at": None, "closure_evidence": None}
        return {"id": key, **data["requests"][key]}
    if args.action == "request-assign":
        request = data["requests"][args.id]
        if request["closed_at"] is not None:
            raise ValueError("request is already closed")
        request["owner"] = owner_name(args.owner)
        return {"id": args.id, **request}
    if args.action == "request-close":
        request = data["requests"][args.id]
        evidence = file_text(args.evidence_file)
        if request["closed_at"] is not None and request["closure_evidence"] != evidence:
            raise ValueError("request already closed; inspect the state before changing it")
        if request["closed_at"] is None:
            request["closed_at"] = recorded_at(args.closed_at)
            request["closure_evidence"] = evidence
        return {"id": args.id, **request}
    if args.action == "leave":
        data["left"][args.row] = {"final_message": file_text(args.final_message_file), "left_at": stamp()}
        return data["left"][args.row]
    if args.action == "asked":
        message = file_text(args.final_message_file)
        question = file_text(args.question_file)
        decision = args.decision.strip()
        if not decision:
            raise ValueError("decision is empty")
        key = identifier(args.row, decision)
        data["questions"].setdefault(key, {"row": args.row, "decision": decision,
                                           "final_message": message,
                                           "question": question, "asked_at": recorded_at(args.asked_at),
                                           "owner_pane": os.environ.get("ORCA_PANE_KEY"),
                                           "answer": None})
        return {"id": key, **data["questions"][key]}
    if args.action == "answered":
        question = data["questions"][args.id]
        answer = file_text(args.answer_file)
        if question["answer"] is not None and question["answer"] != answer:
            raise ValueError("answer already recorded; inspect the state before changing it")
        if question["answer"] is None:
            question.update(answer=answer, answered_at=recorded_at(args.answered_at))
        return {"id": args.id, **question}
    if args.action == "promise":
        key = identifier(args.from_row, args.to_row, args.condition, args.task)
        data["handoffs"].setdefault(key, {"from": args.from_row, "to": args.to_row,
                                          "condition": args.condition, "task": args.task,
                                          "promised_at": stamp(), "delivered_at": None})
        return {"id": key, **data["handoffs"][key]}
    if args.action == "delivered":
        handoff = data["handoffs"][args.id]
        if handoff["delivered_at"] is None:
            handoff["delivered_at"] = stamp()
            handoff["delivery"] = args.delivery
        elif handoff["delivery"] != args.delivery:
            raise ValueError("handoff already delivered; inspect the state before changing it")
        return {"id": args.id, **handoff}
    raise ValueError(args.action)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", default=DEFAULT_STATE, help="shared hourly state JSON path")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("show", help="read all saved decisions, questions, handoffs, and requests")
    needs_owner = commands.add_parser(
        "requests-needing-owner",
        help="list open requests without a live owner; first record each open GitHub issue of the repositories "
             f"in {ISSUE_REPOS} beside the state file once, owned by the Orca worktree Orca links to it or to a PR "
             "that closes it")
    needs_owner.add_argument("--live-owner", action="append", default=[],
                             help="active scan row identity (PATH or ENVIRONMENT::PATH); repeat per owner")
    needs_owner.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                             help="Orca CLI executable (default: $ORCA_CLI_COMMAND or orca)")
    request_open = commands.add_parser("request-open", help="record a user's request and optional assigned lane")
    request_open.add_argument("--original-file", required=True)
    request_open.add_argument("--symptom-file", required=True)
    request_open.add_argument("--owner", help="assigned scan row identity (PATH or ENVIRONMENT::PATH)")
    request_open.add_argument("--opened-at", help="original ISO 8601 time when importing a request")
    request_assign = commands.add_parser("request-assign", help="replace the lane responsible for an open request")
    request_assign.add_argument("--id", required=True)
    request_assign.add_argument("--owner", required=True,
                                help="assigned scan row identity (PATH or ENVIRONMENT::PATH)")
    request_close = commands.add_parser("request-close", help="close after the lead verifies the symptom is gone")
    request_close.add_argument("--id", required=True)
    request_close.add_argument("--evidence-file", required=True)
    request_close.add_argument("--closed-at", help="original ISO 8601 time when importing a closure")
    left_now = commands.add_parser("left-now", help="check if a left row still has the saved final message")
    left_now.add_argument("--row", required=True)
    left_now.add_argument("--final-message-file", required=True)
    leave = commands.add_parser("leave", help="remember a row the user chose to leave")
    leave.add_argument("--row", required=True)
    leave.add_argument("--final-message-file", required=True)
    asked = commands.add_parser("asked", help="record the exact question before presenting it")
    asked.add_argument("--row", required=True)
    asked.add_argument("--decision", required=True, help="stable subject and action, e.g. merge:PR#10")
    asked.add_argument("--final-message-file", required=True)
    asked.add_argument("--question-file", required=True)
    asked.add_argument("--asked-at", help="original ISO 8601 time when importing a question")
    answered = commands.add_parser("answered", help="record the user's answer before acting on it")
    answered.add_argument("--id", required=True)
    answered.add_argument("--answer-file", required=True)
    answered.add_argument("--answered-at", help="original ISO 8601 time when importing an answer")
    promise = commands.add_parser("promise", help="remember when one lane's result should go to another")
    promise.add_argument("--from-row", required=True)
    promise.add_argument("--to-row", required=True)
    promise.add_argument("--condition", required=True)
    promise.add_argument("--task", required=True)
    delivered = commands.add_parser("delivered", help="mark a promised task sent to its lane")
    delivered.add_argument("--id", required=True)
    delivered.add_argument("--delivery", required=True, choices=("queued", "turn_started"))
    args = parser.parse_args(argv)
    path = Path(args.state_file).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        intake = issue_intake(path, args.orca) if args.action == "requests-needing-owner" else None
        with (path.parent / (path.name + ".lock")).open("a+") as lock:
            read_only = ("show", "left-now", "requests-needing-owner")
            writes = args.action not in read_only or intake is not None
            fcntl.flock(lock, fcntl.LOCK_EX if writes else fcntl.LOCK_SH)
            data = read(path)
            if intake is not None:
                import_issues(data, *intake)
            left_matches = False
            if args.action == "left-now":
                left_matches = data["left"].get(args.row, {}).get("final_message") == file_text(args.final_message_file)
                result = {"left": left_matches}
            elif args.action == "requests-needing-owner":
                live = {owner_name(value) for value in args.live_owner}
                result = [{"id": key, **request} for key, request in data["requests"].items()
                          if request["closed_at"] is None and request["owner"] not in live]
            else:
                result = data if args.action == "show" else change(data, args)
            if writes:
                save(path, data)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"hourly state: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.action == "left-now" and not left_matches else 0


if __name__ == "__main__":
    sys.exit(main())
