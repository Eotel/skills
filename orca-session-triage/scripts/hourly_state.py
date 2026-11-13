#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Keep decisions shared by successive hourly triage sessions in one JSON file.

Use --state-file for an isolated copy. Mutations lock beside the file and replace
it atomically, so a waiting session and a new run cannot lose each other's edits.

`stale-asks --from SCAN_JSON` sets each waiting row of a saved scan against the lane's inbox
reports and the answers answer_evidence.py finds, and names the questions left in front of no one,
with each wait on the user's own action (`user-wait`) read now the way it was recorded to be checked.

The triage workspace's Orca comment is written from the state: `asked` and `asked-many` (by default in the workspace
of the hourly triage automation) start it with 「要判断: 」 and the first line of each question asked there still
open, `answered` and `withdraw` take a settled one off, and `comment --result TEXT` adds the run's line,
「HH:MM の定時: TEXT」, after them.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
from functools import cached_property
import hashlib
import json
from operator import itemgetter
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Protocol
import uuid

import answer_evidence
import hourly_runs
import hourly_runtime
import inbox_read
import json_output
import lead_decisions
import scan_sessions

DEFAULT_STATE = "~/.cache/orca-session-triage/hourly-state.json"
# Beside the state file: {"repos": ["owner/name", ...]} whose open issues become requests.
ISSUE_REPOS = "issue-repos.json"
REPO_SLUG = re.compile(r"[\w.-]+/[\w.-]+")
ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/\d+")
ISSUE_LIMIT = 1000  # open issues gh lists per repository; a list this long may be cut
GH_TIMEOUT = 60
# A paired Mac that does not answer Orca within this is skipped, so a run that reads
# Orca itself still ends well inside the 300 seconds before the next hourly run.
REMOTE_ORCA_TIMEOUT = 15
READ_ERRORS = (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError)
# A lane's info that offers the user a choice; open-asks never lists one (2026-10-29: 14 hours unseen).
CHOICE = re.compile(r"【要(?:判断|承認|確認)】|どちら|どれに|選択肢|選んで|(?:ます|です|でしょう)か[。？?]")
EXCERPT = 160
# A question's 「関わる issue」 on GitHub: owner/name#12, or the issue's or PR's URL. A Backlog key is not read.
GITHUB_REF = re.compile(r"(?:https://github\.com/)?([\w.-]+/[\w.-]+)(?:#|/(?:issues|pull)/)(\d+)")
CLOSED_STATES = frozenset(("CLOSED", "MERGED"))
# A `user-wait` check: a check run on a PR (license/cla), or a secret each listed repository must hold.
PR_REF = re.compile(r"([\w.-]+/[\w.-]+)#(\d+)")
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# `ask:<lane>::<ask id>[:<n>[+<n>…]]`, the decision key a question taken from an inbox ask is recorded under.
ASK_DECISION = re.compile(r"ask:.*::([0-9a-fA-F]{8}[0-9a-fA-F-]*)(?::(\d+(?:\+\d+)*))?")
# A question an AskUserQuestion puts is recorded with `asked` just before it: records this long before the call count.
RECORDED_BEFORE = timedelta(minutes=30)
# The newest merged PRs that `asked` names per issue the question names.
MERGED_SHOWN = 5
JST = timezone(timedelta(hours=9))
# An answer text names its recorded time (JST HH:MM) with this; without it, each time of day the text gives
# (13:03, or 12:1x for 12:10-12:19) must be within ANSWER_CLOCK_SLACK minutes of that time.
ANSWERED_AT = "{answered_at}"
CLOCK = re.compile(r"(?<![\d:+])([01]?\d|2[0-3]):([0-5])([\dx])(?!\d)")
ANSWER_CLOCK_SLACK = 10
# A comment that starts with scan_sessions' decision marker makes the workspace a waiting row.
DECISION_MARKER = "要判断"
HEADLINE = 60  # characters of a question's first line in the comment


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
    """A worktree and the GitHub issues and PRs its host links to it."""
    identity: str
    repo: str  # owner/name, or the host's repository name when the owner is unknown
    issues: frozenset[int]
    prs: frozenset[int]

    def works_on(self, issue: Issue) -> bool:
        same_repo = self.repo == issue.repo or ("/" not in self.repo and self.repo == issue.repo.split("/")[1])
        return same_repo and (issue.number in self.issues or bool(self.prs & issue.closing_prs))


class WorktreeHost(Protocol):
    """Where the checkouts come from, and which of them a live lane works in.

    Orca is the host today; another one (t3code, say) answers the same two questions.
    """

    def checkouts(self) -> list[Checkout]: ...

    def live_owners(self) -> frozenset[str]: ...


@dataclass(frozen=True)
class OrcaHost:
    """Orca's worktrees, read now. Telling a live lane from a finished one takes the scan, so it names none."""
    orca: str

    def checkouts(self) -> list[Checkout]:
        return [checkout_of(scan_keys(worktree)) for worktree in orca_worktrees(self.orca)]

    def live_owners(self) -> frozenset[str]:
        return frozenset()


@dataclass(frozen=True)
class SavedScan:
    """The rows scan_sessions.py printed, read without asking Orca again."""
    rows: list[dict]

    def checkouts(self) -> list[Checkout]:
        return [checkout_of(row) for row in self.rows]

    def live_owners(self) -> frozenset[str]:
        """Rows with an agent tab whose class (a careful read's verdict over the scan's) is not finished."""
        return frozenset(scan_sessions.row_identity(row) for row in self.rows
                         if any(term.get("agent") for term in row.get("terminals") or [])
                         and ((row.get("verdict") or {}).get("class") or row.get("class")) != "finished")


@dataclass(frozen=True)
class Intake:
    """What one run read: the open issues, the repositories gh listed in full, and the host's checkouts."""
    issues: tuple[Issue, ...]
    complete: frozenset[str]  # lowercased owner/name of each repository whose whole open list gh returned
    checkouts: tuple[Checkout, ...]

    @cached_property
    def open_urls(self) -> frozenset[str]:
        return frozenset(issue.url for issue in self.issues)

    def no_longer_open(self, request: dict) -> str | None:
        """owner/name when the request came from an issue that its repository's full open list, read now, lacks."""
        source = request.get("source")
        url = source.get("url") if isinstance(source, dict) and source.get("kind") == "github_issue" else None
        found = ISSUE_URL.fullmatch(url) if isinstance(url, str) else None
        if found is None or found.group(1).lower() not in self.complete or url in self.open_urls:
            return None
        return found.group(1)


def issue_repos(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, list) or not all(isinstance(repo, str) and REPO_SLUG.fullmatch(repo) for repo in repos):
        raise ValueError(f'{path} must hold {{"repos": ["owner/name", ...]}}')
    return repos


def gh_open_issues(repo: str) -> list[dict]:
    out = subprocess.run(["gh", "issue", "list", "-R", repo, "--state", "open", "--limit", str(ISSUE_LIMIT),
                          "--json", "number,title,url,assignees,closedByPullRequestsReferences"],
                         capture_output=True, text=True, check=True, timeout=GH_TIMEOUT).stdout
    return json.loads(out)


def listed_issues(repo: str) -> list[Issue]:
    return [Issue(repo=repo, number=item["number"], title=item["title"], url=item["url"],
                  closing_prs=frozenset(pr["number"] for pr in item.get("closedByPullRequestsReferences") or []),
                  assignees=tuple(person["login"] for person in item.get("assignees") or []))
            for item in gh_open_issues(repo)]


def open_issues(repo: str) -> list[Issue] | None:
    """The open issues gh lists for repo, asking once more after a failed read; None when both fail."""
    try:
        return listed_issues(repo)
    except READ_ERRORS as error:
        print(f"hourly state: reading open issues of {repo} again after: {error}", file=sys.stderr)
    try:
        return listed_issues(repo)
    except READ_ERRORS as error:
        print(f"hourly state: open issues of {repo} not imported: {error}", file=sys.stderr)
        return None


def orca_worktrees(orca: str) -> list[dict]:
    return scan_sessions.collect_worktrees_and_terminals(orca, remote_timeout=REMOTE_ORCA_TIMEOUT)[0]


def origin_slug(path: str) -> str | None:
    try:
        origin = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return scan_sessions.github_slug(origin.stdout.strip()) if origin.returncode == 0 else None


def scan_keys(worktree: dict) -> dict:
    """An Orca worktree under the key names a scan row gives the same facts."""
    return {"path": worktree["path"], "environment": worktree.get("_environment"), "repo": worktree.get("repo"),
            "linked_pr": worktree.get("linkedPR"), "linked_issue": worktree.get("linkedIssue"),
            "linked_items": worktree.get("linkedItems") or []}


def checkout_of(row: dict) -> Checkout:
    """A scan row's checkout, with the issues and PRs Orca links to it."""
    linked = row.get("linked_pr") or {}
    found = scan_sessions.PR_URL.search(linked.get("url") or "")
    slug = found.group(1) if found else None if row.get("environment") else origin_slug(row["path"])
    items = [item for item in row.get("linked_items") or [] if isinstance(item, dict)]
    return Checkout(
        identity=scan_sessions.row_identity(row),
        repo=slug or row.get("repo") or "",
        issues=frozenset([*([row["linked_issue"]] if row.get("linked_issue") else []),
                          *(item.get("number") for item in items if item.get("type") == "issue")]),
        prs=frozenset([*([linked["number"]] if linked.get("number") else []),
                       *(item.get("number") for item in items if item.get("type") == "pr")]))


def issue_intake(state: Path, host: WorktreeHost) -> Intake | None:
    """Open issues of the listed repositories and the host's checkouts, or None when none are listed.

    A repository gh cannot read twice is left out with a warning; without the checkouts nothing is imported.
    """
    repos = issue_repos(state.parent / ISSUE_REPOS)
    if not repos:
        return None
    try:
        checkouts = tuple(host.checkouts())
    except READ_ERRORS as error:
        print(f"hourly state: open issues not imported, worktrees unreadable: {error}", file=sys.stderr)
        return None
    read = {repo: open_issues(repo) for repo in repos}
    listed = {repo: issues for repo, issues in read.items() if issues is not None}
    return Intake(issues=tuple(issue for issues in listed.values() for issue in issues),
                  complete=frozenset(repo.lower() for repo, issues in listed.items() if len(issues) < ISSUE_LIMIT),
                  checkouts=checkouts)


def import_issues(data: dict, intake: Intake) -> None:
    """Record each open issue as a request once; an ownerless one gets the checkout that works on it."""
    recorded = {request["source"].get("url"): request for request in data["requests"].values()
                if isinstance(request.get("source"), dict)}
    for issue in intake.issues:
        owner = next((checkout.identity for checkout in intake.checkouts if checkout.works_on(issue)), None)
        request = recorded.get(issue.url)
        if request is None:
            data["requests"][uuid.uuid4().hex[:16]] = {
                "original": f"{issue.title}\n{issue.url}", "symptom": f"{issue.repo}#{issue.number} is open",
                "owner": owner, "opened_at": stamp(), "closed_at": None, "closure_evidence": None,
                "source": {"kind": "github_issue", "url": issue.url, "assignees": list(issue.assignees)}}
        elif request["closed_at"] is None and request["owner"] is None and owner:
            request["owner"] = owner


def owner_report(data: dict, live: frozenset[str], intake: Intake | None) -> dict[str, list[dict]]:
    """Open requests without a live owner; apart from them, those whose GitHub issue is no longer open."""
    open_requests = [(key, request, intake.no_longer_open(request) if intake else None)
                     for key, request in data["requests"].items() if request["closed_at"] is None]
    return {
        "needs_owner": [{"id": key, **request} for key, request, gone in open_requests
                        if gone is None and request["owner"] not in live],
        "closed_issues": [{"id": key, **request,
                           "decided_by": f"gh issue list -R {gone} --state open in this run does not list it"}
                          for key, request, gone in open_requests if gone is not None],
    }


def excerpt(text: object) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:EXCERPT] + ("…" if len(compact) > EXCERPT else "")


def report_view(item: dict) -> dict:
    return {"id": item.get("id") or "", "ts": item.get("ts") or "", "text": excerpt(item.get("text"))}


def answer_view(answer: answer_evidence.Answer) -> dict:
    return {"at": answer.at.isoformat(), "source": answer.source, "text": excerpt(answer.text)}


def gh_json(args: list[str]) -> tuple[Any, str | None]:
    """What gh printed as JSON, else None and why, in gh's own words: on 2026-11-10 `gh secret list` on a guessed
    repository name failed with 2>/dev/null and was read as 0 secrets."""
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)
    try:
        return json.loads(out.stdout), None
    except ValueError:
        return None, " ".join((out.stderr or out.stdout or f"gh exited {out.returncode}").split())[:200]


def pr_check(ref: str, name: str) -> dict:
    found = PR_REF.fullmatch(ref.strip())
    if found is None or not name.strip():
        raise ValueError(f"--pr-check takes OWNER/NAME#N and a check name, not: {ref} {name}")
    return {"kind": "pr_check", "repo": found[1], "number": int(found[2]), "name": name.strip()}


def secret_check(values: list[str]) -> dict:
    name, *repos = values
    if not SECRET_NAME.fullmatch(name) or not repos or not all(REPO_SLUG.fullmatch(repo) for repo in repos):
        raise ValueError("--secret takes a secret name and the OWNER/NAME of each repository that must hold it, not: "
                         + " ".join(values))
    return {"kind": "secret", "name": name, "repos": list(dict.fromkeys(repos))}


def check_now(check: dict) -> dict:
    """A user-wait check read now. A PR check gets `state`, gh's bucket of its runs of that name (fail, cancel, pending,
    pass, skipping, the first of these any run has), or `missing` when the PR has none; a secret gets, per
    repository, whether it holds the secret. `unread` says why gh could not read one, instead of a state."""
    if check["kind"] == "command":
        try:
            result = subprocess.run(["/bin/sh", "-c", check["command"]], cwd=check["cwd"],
                                    capture_output=True, text=True, timeout=GH_TIMEOUT)
            return {**check, "exit_code": result.returncode, "unread": None}
        except (OSError, subprocess.SubprocessError) as error:
            return {**check, "exit_code": None, "unread": str(error)}
    if check["kind"] == "pr_check":
        runs, why = gh_json(["pr", "checks", str(check["number"]), "-R", check["repo"], "--json", "name,bucket"])
        if why is None and not isinstance(runs, list):
            why = "gh pr checks printed no list"
        buckets = {run.get("bucket") for run in runs or [] if isinstance(run, dict) and run.get("name") == check["name"]}
        state = None if why else next((bucket for bucket in ("fail", "cancel", "pending", "pass", "skipping")
                                       if bucket in buckets), "missing")
        return {**check, "state": state, "unread": why}
    holders = []
    for repo in check["repos"]:
        listed, why = gh_json(["secret", "list", "-R", repo, "--json", "name"])
        if why is None and not isinstance(listed, list):
            why = "gh secret list printed no list"
        holders.append({"repo": repo, "unread": why, "present": None if why else any(
            isinstance(item, dict) and item.get("name") == check["name"] for item in listed)})
    return {**check, "repos": holders}


def check_done(check: dict) -> bool:
    if check["kind"] == "command":
        return check["exit_code"] == 0
    if check["kind"] == "pr_check":
        return check["state"] == "pass"
    return all(holder["present"] for holder in check["repos"])


def user_waits_now(data: dict, now: datetime) -> list[dict]:
    """Each open user-wait, its checks read now, as a stale-asks item; `checks_done` when every check holds."""
    items = []
    for key, wait in data["user_waits"].items():
        if wait["closed_at"] is not None:
            continue
        checks = [check_now(check) for check in wait["checks"]]
        if all(map(check_done, checks)):
            continue
        since = scan_sessions.parse_time(wait["started_at"]) or now
        items.append({"row": wait["row"], "name": Path(wait["row"].split("::", 1)[-1]).name if wait["row"] else "-",
                      "state": "user_wait", "id": key, "since": wait["started_at"], "question": excerpt(wait["text"]),
                      "checks": checks, "checks_done": all(map(check_done, checks)), "ask": None, "answer": None,
                      "age_minutes": int((now - since).total_seconds() // 60)})
    return items


def check_line(check: dict) -> str:
    if check["kind"] == "command":
        return f"{check['command']}: " + (f"読めない（{check['unread']}）" if check["unread"]
                                         else f"exit {check['exit_code']}")
    if check["kind"] == "pr_check":
        return (f"{check['repo']}#{check['number']} {check['name']} "
                + (f"読めない（{check['unread']}）" if check["unread"] else check["state"]))
    return f"{check['name']}: " + ", ".join(
        f"{holder['repo']} " + (f"読めない（{holder['unread']}）" if holder["unread"]
                                else "有り" if holder["present"] else "無し") for holder in check["repos"])


def inbox_entries() -> list[dict]:
    """The triage inbox's entries; none while it does not exist yet."""
    inbox = inbox_read.paths()[0]
    return inbox_read.inbox_entries(inbox) if inbox.exists() else []


def question_ask(item: dict, entries: list[dict]) -> tuple[str, frozenset[int]] | None:
    """The full id of the inbox ask a recorded question came from, and the numbers of that ask's questions it puts
    (none when it puts the whole ask or they are unknown): the ask an `asked-many` "ask" line recorded, else the one
    its `ask:<lane>::<id>[:<n>[+<n>…]]` decision names."""
    found = ASK_DECISION.fullmatch(str(item.get("decision") or ""))
    wanted = item.get("ask_id") or (found[1] if found else None)
    ids = [str(entry["id"]) for entry in entries
           if wanted and entry.get("kind") == "ask" and entry.get("id") and inbox_read.names(str(entry["id"]), wanted)]
    if len(ids) != 1:
        return None
    return ids[0], frozenset(int(number) for number in found[2].split("+")) if found and found[2] else frozenset()


def withdrawn_by(ask: tuple[str, frozenset[int]] | None, entries: list[dict]) -> list[dict]:
    """The later asks of the lane that withdrew the ask, or one of its questions the record puts (any of them when it
    names none), with `triage-notify --withdraws`: the lane asked again, and the question shown may be the old one."""
    if ask is None:
        return []
    ask_id, numbers = ask
    return [reissue.view() for reissue in inbox_read.reissues(ask_id, entries)
            if (any(reissue.covers(number) for number in numbers) if numbers else reissue.withdrawn)]


def worktree() -> Path:
    value = os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")
    return Path(value.split("::", 1)[-1]).expanduser().absolute() if value else Path.cwd()


def appointed_lead(state: dict) -> Path | None:
    lead = state.get("lead") or {}
    return Path(lead["worktree"]) if lead.get("worktree") else None


def hourly_workspace(orca: str, workspace: str) -> bool:
    """Whether the hourly triage automation runs in the workspace. Outside an Orca tab it is not, and Orca is not
    asked."""
    if not os.environ.get("ORCA_PANE_KEY"):
        return False
    here = os.path.realpath(workspace)
    return any(hourly_runtime.workspace_path(automation.get("workspaceId")) == here
               for automation in hourly_runtime.hourly_automations(orca))


def headline(question: str) -> str:
    first = next((line.strip() for line in question.splitlines() if line.strip()), "")
    return first[:HEADLINE] + ("…" if len(first) > HEADLINE else "")


def workspace_comment(data: dict, workspace: str) -> str:
    """「要判断: 」 and the headline of each question asked in the workspace still open, in the order recorded, then
    the line of its last run; either is left out when there is none, so with neither the comment is empty."""
    asked = [headline(item["question"]) for item in data["questions"].values()
             if item.get("asked_in") == workspace and item["answer"] is None and not item.get("withdrawn_at")]
    lines = [f"{DECISION_MARKER}: " + "／".join(asked)] if asked else []
    run = data["comments"].get(workspace, {}).get("run")
    return "\n".join([*lines, *([run] if run else [])])


def write_comment(orca: str, workspace: str, text: str) -> str | None:
    """Set the workspace's Orca comment (an empty text clears it); None once Orca took it, else why it did not."""
    try:
        hourly_runtime.orca_result(orca, "worktree", "set", "--worktree", f"path:{workspace}", "--comment", text)
    except READ_ERRORS as error:
        said = " ".join(str(getattr(error, "stderr", "") or "").split())
        return f"作業場 {workspace} の Orca のコメントを書けなかった: {error}{f'（{said[:200]}）' if said else ''}"
    return None


def session_record() -> Path:
    """This Claude Code session's record, which names the session and when it started."""
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
    return config / "sessions" / f"{os.environ.get('CLAUDE_PID', '')}.json"


def run_line(result: str) -> tuple[str, str | None]:
    """「HH:MM の定時: RESULT」 at the local time this session started, and, when its record cannot be read, why the
    time is now's instead."""
    text = " ".join(result.split())
    if not text:
        raise ValueError("--result is empty")
    record = session_record()
    try:
        started, why = datetime.fromtimestamp(json.loads(record.read_text(encoding="utf-8"))["startedAt"] / 1000), None
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        started, why = datetime.now(), f"回の始まりを {record} から読めず、今の時刻で書いた: {error!r}"
    return f"{started:%H:%M} の定時: {text}", why


def row_question(row: dict, final: dict) -> str:
    # The question --row shows, then what a scan saved before marker_question existed leaves.
    if question := scan_sessions.row_question(row).strip():
        return question
    question = str((row.get("verdict") or {}).get("question") or "").strip()
    if question:
        return str(question)
    comment = re.search(r"要判断[:：]\s*(.+)", row.get("comment") or "", re.DOTALL)
    if comment:
        return comment.group(1).strip()
    return final["text"] or next((message for card in reversed(row.get("done_cards") or [])
                                  if (message := scan_sessions.screen_message(
                                      {"tail": card.get("screen_tail") or []}))), "")


def blocked_by_user(row: dict) -> bool:
    verdict = row.get("verdict") or {}
    return verdict.get("class") == "blocked" and bool(re.search(
        r"三浦|miura", str(verdict.get("waits_on") or row.get("waits_on") or ""), re.IGNORECASE))


def row_prs(rows: list[dict]) -> dict[str, dict]:
    wanted = [(scan_sessions.row_identity(row), scan_sessions.github_of(row),
               (row.get("pr") or row.get("linked_pr") or {}).get("number"))
              for row in rows]
    repos = {repo: list(dict.fromkeys(number for _, slug, number in wanted if slug == repo and number))
             for _, repo, _ in wanted if repo}
    states = {repo: lead_decisions.github_pr_states(repo, numbers) for repo, numbers in repos.items() if numbers}
    return {identity: {"state": found.state, "head": found.head, "updatedAt": found.updated_at}
            for identity, repo, number in wanted if (found := (states.get(repo) or {}).get(number))}


def related_issues(ask: dict) -> list[str]:
    """The 「関わる issue」 the ask's structured questions name, once each."""
    return list(dict.fromkeys(issue for question in inbox_read.questions_of(ask) if isinstance(question, dict)
                              if (issue := str(question.get("issue") or "").strip())))


def with_issue_states(items: list[dict]) -> None:
    """Replace each item's `related_issues` with their states, read in one GraphQL query per repository, and set
    `related_issues_closed` when every one is closed or merged. A reference that is not on GitHub has no state."""
    refs = {ref: GITHUB_REF.fullmatch(ref) for item in items for ref in item.get("related_issues") or []}
    numbers: dict[str, set[int]] = {}
    for found in filter(None, refs.values()):
        numbers.setdefault(found.group(1), set()).add(int(found.group(2)))
    states = {repo: lead_decisions.github_pr_states(repo, sorted(wanted), include_issues=True) or {}
              for repo, wanted in numbers.items()}
    for item in items:
        shown = []
        for ref in item.get("related_issues") or []:
            found = (states[match.group(1)].get(int(match.group(2))) if (match := refs[ref]) else None)
            shown.append({"ref": ref, "state": found.state if found else None,
                          "updatedAt": found.updated_at if found else None})
        item["related_issues"] = shown
        item["related_issues_closed"] = bool(shown) and all(ref["state"] in CLOSED_STATES for ref in shown)


def covering_answers(question: dict, decisions: dict[str, lead_decisions.Decision]) -> list[lead_decisions.Decision]:
    asked_at = scan_sessions.parse_time(question.get("asked_at"))
    return [decisions[key] for key in question.get("covers") or [] if key in decisions
            and asked_at and (at := scan_sessions.parse_time(decisions[key].decided_at)) is not None and at > asked_at]


def stale_asks(rows: list[dict], entries: list[dict], open_ids: frozenset[str],
               answers: list[answer_evidence.Answer], now: datetime, *, state: dict,
               handed: list[lead_decisions.HandOff],
               claimed: dict[str, inbox_read.Claim] | None = None,
               decided: dict[str, list[lead_decisions.Decision]] | None = None,
               rejected: list[dict] | None = None) -> list[dict]:
    """Waiting rows, rows blocked on the user, and rows an unanswered handoff key names, longest waiting first.
    The lane's asks that count are
    those still open and those it sent in its last turn:
    - `unanswered`: one of them has no answer that proves it answered (answer_evidence.py), whether it is open
      or was marked answered; the oldest is `ask`;
    - `stopped_after_answer`: they are answered, one after the lane's last message, and the lane has not spoken
      since; `answer` is the newest such answer, `since` the first;
    - `settled`: none of the above, and a lead decision key naming the row (`ask:<name>::…`) has a record after
      the lane's last message; `evidence` holds each such key's newest record that decides something, past a
      later one that only says it was passed on (lead_decisions.substance);
    - `unasked`: they were answered before the lane's last message, or it has none, so open-asks does not show
      its question; `choices_in_info` lists choices it sent as info since its last turn began;
    - `rejected`: an AskUserQuestion of `rejected` (answer_evidence.rejected_questions), closed without an
      answer, with its `questions`, `since` (asked) and `closed_at`; `name` is the worktree that asked it.
    `candidates` holds the newest answers that name only the lane or its PR numbers, for the lead to check: the
    lead may ask the user a waiting row's question itself. `reissued` lists the later asks of the lane that withdrew
    the item's ask, or the recorded question's (withdrawn_by): the question to put or relay may be the old one. An unanswered item's `ask.claimed` names the tab that
    claimed it with `inbox_read.py claim` and is asking the user now: `claimed` holds, by ask id, the claim on each
    open ask itself or on every one of its open questions (inbox_read.ask_claim).
    """
    found: list[tuple[datetime, dict[str, Any]]] = []
    entries = [item for item in entries if not inbox_read.is_handoff(item)]
    shared = answer_evidence.shared_questions(entries)
    latest_decisions = {key: items[-1] for key, items in (decided or {}).items() if items}
    for row in rows:
        identity = scan_sessions.row_identity(row)
        own_handle = os.environ.get("ORCA_TERMINAL_HANDLE")
        if not row.get("environment") and (Path(row["path"]).resolve() == worktree().resolve()
                or own_handle and any(term.get("handle") == own_handle for term in row.get("terminals") or [])):
            continue
        final = scan_sessions.final_message(row)
        if state.get("left", {}).get(identity, {}).get("final_message") == final["text"].strip():
            continue
        said_at = scan_sessions.parse_time(final["at"])
        began = (scan_sessions.parse_time((row.get("claude") or {}).get("last_user_at"))
                 if final["source"] == "claude" else None) or said_at
        name = Path(row["path"]).name
        for job in row.get("lost_schedules") or []:
            since = said_at or scan_sessions.parse_time(row.get("scanned_at")) or now
            found.append((since, {"row": identity, "name": scan_sessions.row_name(row),
                                   "state": "lost_schedule", "since": since.isoformat(),
                                   "question": f"予約が消えた: {job['cron']} {job['prompt'][:100]}",
                                   "schedule": job, "ask": None, "answer": None}))
        lane = [(at, item) for item in entries if item.get("worktree") == name
                and item.get("repo") in (None, "", row.get("repo"))
                and (at := scan_sessions.parse_time(item.get("ts"))) is not None]
        keys = {item["id"]: [handoff.decision for handoff in handed if (key := answer_evidence.key_ask_id(handoff.decision))
                            and answer_evidence.names_ask_id(key, str(item["id"]))]
                for _, item in lane if item.get("kind") == "ask" and item.get("id")}
        shared_pending = [dict(id=key, **item) for key, item in state.get("questions", {}).items()
                          if item.get("row") == identity and item.get("answer") is None and not item.get("withdrawn_at")
                          and not (item.get("covers") and
                                   len(covering_answers(item, latest_decisions)) == len(item["covers"]))]
        in_progress = (row.get("verdict") or {}).get("class") == "in_progress"
        progress_question = in_progress and bool(row.get("prompt_waiting") or
                                                  (row.get("claude") or {}).get("pending_questions"))
        counted = [(at, item, answer_evidence.answers_to(item, answers, shared=shared)) for at, item in lane
                   if item.get("kind") == "ask" and item.get("id")
                   and (item["id"] in open_ids or keys.get(item["id"]) or began is not None and at >= began)]
        if (((row.get("verdict") or {}).get("class") or row.get("class")) != "waiting"
                and not blocked_by_user(row) and not any(keys.values()) and not shared_pending
                and not (in_progress and (progress_question or counted))):
            continue
        comment = row.get("comment") or ""
        question = row_question(row, final)
        view = {"row": identity, "name": scan_sessions.row_name(row),
                "question": excerpt(question), "merge_question": scan_sessions.asks_merge(question, comment),
                "review": scan_sessions.review_result(row, scan_sessions.github_of(row)),
                "ask": None, "answer": None, "candidates": [], "choices_in_info": []}
        if blocked_by_user(row):
            proven = [answer for _, _, proof in counted for answer in proof]
            if proven:
                view["answer"] = answer_view(max(proven, key=lambda answer: answer.at))
        unanswered = [(at, item) for at, item, proof in counted if not proof or keys.get(item["id"])]
        if unanswered:
            oldest = min(unanswered, key=itemgetter(0))
            for asked_at, ask in unanswered:
                if (asked_at, ask) != oldest and not keys.get(ask["id"]):
                    continue
                asked = "\n".join(answer_evidence.questions_of(ask)) or ask.get("text")
                found.append((asked_at, {
                    **view, "state": "unanswered", "since": ask.get("ts"),
                    "question": excerpt(asked) or view["question"],
                    "merge_question": scan_sessions.asks_merge(asked or question, comment),
                    "decisions": keys.get(ask["id"], []), "related_issues": related_issues(ask),
                    "reissued": withdrawn_by((ask["id"], frozenset()), entries),
                    "ask": {**report_view(ask), "marked": ask["id"] not in open_ids,
                            "claimed": (claim.view() if (claim := (claimed or {}).get(ask["id"])) else None)},
                    "candidates": [answer_view(answer) for answer in answer_evidence.related_to(ask, answers)[-3:]]}))
            continue
        if shared_pending or progress_question:
            item = min(shared_pending, key=lambda item: item.get("asked_at") or "") if shared_pending else {}
            since_text = item.get("asked_at") or (row.get("prompt_waiting") or {}).get("asked_at") or final["at"]
            since = scan_sessions.parse_time(since_text) or now
            found.append((since, {**view, "state": "unanswered", "since": since_text or since.isoformat(),
                                   "id": item.get("id"), "question": excerpt(item.get("question")) or view["question"],
                                   "reissued": withdrawn_by(question_ask(item, entries), entries) if item else []}))
            continue
        later = [(answer, item) for _, item, proof in counted for answer in proof
                 if said_at is None or answer.at > said_at]
        if later:
            first = min(answer.at for answer, _ in later)
            answer, ask = max(later, key=lambda pair: pair[0].at)
            if any(at > answer.at and item.get("kind") in ("done", "info") for at, item in lane):
                continue
            asked = "\n".join(answer_evidence.questions_of(ask)) or ask.get("text")
            found.append((first, {**view, "state": "stopped_after_answer", "since": first.isoformat(),
                                  "question": excerpt(asked) or view["question"],
                                  "merge_question": scan_sessions.asks_merge(asked or question, comment),
                                  "related_issues": related_issues(ask),
                                  "reissued": withdrawn_by((ask["id"], frozenset()), entries),
                                  "ask": {**report_view(ask), "marked": ask["id"] not in open_ids},
                                  "answer": answer_view(answer)}))
            continue
        # A question on the lane's screen or pending in its transcript asks now, after every record: montly-report
        # asked a new one on 2026-11-06.
        keyed = [lead_decisions.substance(items) for key, items in (decided or {}).items()
                 if not scan_sessions.prompt_question(row.get("prompt_waiting") or {})
                 and not (row.get("claude") or {}).get("pending_questions")
                 and key.startswith(f"ask:{view['name']}::") and any(
                     said_at is None or (at := scan_sessions.parse_time(item.decided_at)) is not None and at > said_at
                     for item in items)]
        if keyed:
            newest = max(keyed, key=lambda item: scan_sessions.parse_time(item.decided_at) or now)
            found.append((scan_sessions.parse_time(newest.decided_at) or now, {
                **view, "state": "settled", "since": newest.decided_at,
                "evidence": [{"decision": item.decision, "decided_at": item.decided_at, "answer": item.answer}
                             for item in keyed]}))
            continue
        prompt_at = (row.get("prompt_waiting") or {}).get("asked_at")
        since = scan_sessions.parse_time(prompt_at) or said_at or scan_sessions.parse_time(row.get("scanned_at")) or now
        asked_directly = answer_evidence.related_to(
            {"worktree": name, "ts": final["at"], "text": f"{view['question']}\n{final['text']}"},
            answers) if said_at else []
        choices = [report_view(item) for at, item in lane if item.get("kind") == "info" and began and at >= began
                   and CHOICE.search(str(item.get("text") or ""))]
        found.append((since, {**view, "state": "unasked", "since": prompt_at or final["at"] or since.isoformat(),
                              "candidates": [answer_view(answer) for answer in asked_directly[-3:]],
                              "choices_in_info": choices}))
    for item in rejected or []:
        asked = "\n".join(str(question.get("question") or "") for question in item["questions"]
                          if isinstance(question, dict))
        found.append((scan_sessions.parse_time(item["asked_at"]) or now, {
            "row": None, "name": item["worktree"], "state": "rejected", "id": item["id"], "since": item["asked_at"],
            "closed_at": item["closed_at"], "question": excerpt(asked), "questions": item["questions"],
            "ask": None, "answer": None}))
    found.sort(key=itemgetter(0))
    return [{**item, "age_minutes": int((now - at).total_seconds() // 60),
             **({"answer_age_minutes": int((now - (scan_sessions.parse_time(item["answer"]["at"]) or now)).total_seconds() // 60)}
                if item["answer"] else {})} for at, item in found]


def read(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "left": {}, "questions": {}, "handoffs": {}, "requests": {}, "user_waits": {},
                "comments": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or any(
            not isinstance(data.get(key), dict) for key in ("left", "questions", "handoffs")) or any(
                not isinstance(data.get(key, {}), dict) for key in ("requests", "user_waits", "comments")):
        raise ValueError(f"invalid hourly state: {path}")
    data.setdefault("requests", {})
    data.setdefault("user_waits", {})
    # By workspace whose Orca comment this script writes: {"run": the line of its last run, or None}.
    data.setdefault("comments", {})
    return data


def save(path: Path, data: dict) -> None:
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


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recorded_at(value: str | None) -> str:
    if value is None:
        return stamp()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs a timezone offset")
    return parsed.isoformat()


def identifier(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:16]


def file_text(path: str | Path) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"empty text file: {path}")
    return value


def owner_name(value: str) -> str:
    owner = value.strip()
    environment, separator, path = owner.partition("::")
    if not owner or not Path(path if separator else owner).is_absolute() or (separator and not environment):
        raise ValueError("owner must be a scan row identity: PATH or ENVIRONMENT::PATH")
    return owner


def repository_names(owner: str) -> frozenset[str]:
    """The names of the repositories gh lists for an organization or user; none, named on stderr, when gh cannot."""
    listed, why = gh_json(["repo", "list", owner, "--limit", str(ISSUE_LIMIT), "--json", "name"])
    if why is None and isinstance(listed, list):
        return frozenset(str(item["name"]) for item in listed if isinstance(item, dict) and item.get("name"))
    print(f"hourly state: the repositories of {owner} were not listed, so a question's name#N stays unread: "
          f"{why or 'gh repo list printed no list'}", file=sys.stderr)
    return frozenset()


# A word of letters written before ` #N` (or ` PR #N`): a repository, or prose such as "merge #10".
SPACED_WORD = re.compile(r"(?<![\w.-])([A-Za-z]+)(?: PR)? #\d")


def prose_words(text: str, names: frozenset[str]) -> frozenset[str]:
    """The words written before ` #N` that no repository of `names` (the organization's, as gh listed them) has as
    a hyphen-separated part of its name: prose, so the `#N` is bare. With no names listed, none: `cvm #12` is read
    as no repository, never as the row's own."""
    return frozenset(word for word in map(str.lower, SPACED_WORD.findall(text)) if names and not any(
        f"-{word}-" in f"-{name.lower()}-" for name in names))


def question_references(questions: dict[str, dict]) -> dict[str, list[dict]]:
    """Each question's issues and PRs, read once per repository, by the rule scan_sessions.waited_items reads a
    verdict's waits_on with: `owner/name#N` and GitHub URLs as written, `name#N` as the repository of the row's
    organization so named (or the only one whose name ends in `-name`), and a bare `#N` in the repository named before
    it, else the row's checkout's origin; so does a `#N` after an ordinary word (prose_words). A repository it
    cannot tell leaves `repo` None and no state: a name no repository of the organization has (infra-aws#526 was
    read as the row's own #526 until 2026-11-11), or a bare `#N` of a row on another Mac, whose path here may hold
    another repository. The organization's names are listed once,
    only for a question naming a repository other than the row's own."""
    wanted: dict[str, list[tuple[str | None, int]]] = {}
    origins: dict[str, str | None] = {}
    listed: dict[str, frozenset[str]] = {}
    for key, item in questions.items():
        text, row = item["question"], item["row"]
        if not scan_sessions.WAITED_ITEM.search(text):
            continue
        if row not in origins:
            origins[row] = origin_slug(row) if "::" not in row else None
        owner, _, own = (origins[row] or "").partition("/")
        named = {found["repo"] for found in scan_sessions.WAITED_ITEM.finditer(text) if found["repo"]
                 and "/" not in found["repo"] and found["repo"].lower() not in scan_sessions.NOT_REPOS}
        if owner and not named <= {own} and owner not in listed:
            listed[owner] = repository_names(owner)
        names = {own, *listed.get(owner, ())} if owner else set()
        prose = prose_words(text, listed.get(owner, frozenset()))
        refs = [(repo or None, number)
                for repo, number in scan_sessions.waited_items(text, origins[row] or "", names, prose)]
        if refs:
            wanted[key] = refs
    repos: dict[str, set[int]] = {}
    for refs in wanted.values():
        for repo, number in refs:
            if repo:
                repos.setdefault(repo, set()).add(number)
    states = {repo: lead_decisions.github_pr_states(repo, sorted(numbers), include_issues=True)
              for repo, numbers in repos.items()}
    return {key: [{"repo": repo, "number": number, "type": found.type if found else None,
                   "state": found.state if found else None, "head": found.head if found else None,
                   "updatedAt": found.updated_at if found else None}
                  for repo, number in refs for found in [(states.get(repo) or {}).get(number) if repo else None]]
            for key, refs in wanted.items()}


LINKED_PRS = ("closedByPullRequestsReferences(first: {n}, includeClosedPrs: true) {{ nodes {{ ...Linked }} }} "
              "timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 50) {{ nodes {{ "
              "... on CrossReferencedEvent {{ source {{ ... on PullRequest {{ ...Linked }} }} }} }} }}")
LINKED = "fragment Linked on PullRequest { number title url mergedAt repository { nameWithOwner } }"


def merged_prs(repo: str, numbers: list[int]) -> dict[int, list[dict]]:
    """By issue, the merged PRs that close it or name it (Refs, Closes, a mention in any repository), newest merge
    first, read in one GraphQL query per scan_sessions.GRAPHQL_CHUNK issues; an issue gh cannot read has none."""
    owner, _, name = repo.partition("/")
    found: dict[int, dict[str, dict]] = {}
    for start in range(0, len(numbers), scan_sessions.GRAPHQL_CHUNK):
        chunk = numbers[start:start + scan_sessions.GRAPHQL_CHUNK]
        fields = " ".join(f"issue_{number}: issue(number: {number}) {{ "
                          + LINKED_PRS.format(n=scan_sessions.GRAPHQL_CHUNK) + " }" for number in chunk)
        repository = scan_sessions.graphql_repository(
            repo, f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }} {LINKED}")
        for number in chunk:
            issue = (repository or {}).get(f"issue_{number}") or {}
            nodes = [*((issue.get("closedByPullRequestsReferences") or {}).get("nodes") or []),
                     *((event or {}).get("source") for event in (issue.get("timelineItems") or {}).get("nodes") or [])]
            for pr in nodes:
                if isinstance(pr, dict) and pr.get("mergedAt") and isinstance(pr.get("number"), int):
                    ref = f"{(pr.get('repository') or {}).get('nameWithOwner') or repo}#{pr['number']}"
                    found.setdefault(number, {})[ref] = {"pr": ref, "title": excerpt(pr.get("title")),
                                                         "url": pr.get("url"), "merged_at": pr["mergedAt"]}
    return {number: sorted(prs.values(), key=lambda pr: pr["merged_at"], reverse=True)
            for number, prs in found.items()}


@dataclass(frozen=True)
class Asking:
    """A question about to be put to the user, and the decision it settles."""
    row: str
    decision: str
    final_message: str
    question: str
    asked_at: str
    covers: tuple[str, ...] = ()
    ask: str | None = None  # the full id of the inbox ask it was taken from
    based_at: str | None = None  # when what the question rests on was said: that ask's time
    session: str | None = None  # the session that put it; None for this one (CLAUDE_CODE_SESSION_ID)

    @property
    def key(self) -> str:
        return identifier(self.row, self.decision)


@dataclass(frozen=True)
class Answering:
    """The user's answer to a recorded question, named by its id or by the decision it was asked under."""
    answer: str
    id: str | None = None
    decision: str | None = None
    answered_at: str | None = None
    replace_reason: str | None = None


def current_question(data: dict, asking: Asking) -> str | None:
    return next((key for key, item in reversed(list(data["questions"].items()))
                 if item["row"] == asking.row and item["decision"] == asking.decision
                 and not item.get("withdrawn_at")), None)


def new_references(data: dict, askings: list[Asking]) -> dict[str, list[dict]]:
    """By key, the issues and PRs each question not yet recorded names (question_references)."""
    return question_references({asking.key: {"row": asking.row, "question": asking.question}
                                for asking in askings if current_question(data, asking) is None})


def record_question(data: dict, asking: Asking, references: dict[str, list[dict]]) -> dict:
    """Record the question, or, when every issue and PR it names is CLOSED, record nothing and give that evidence.
    A question another session already put (`asking.session`) is recorded all the same: it only accounts for that
    call. A recorded question names its session (CLAUDE_CODE_SESSION_ID by default), its transcript's name."""
    refs = references.get(asking.key) or []
    existing = current_question(data, asking)
    if (existing is None and asking.session is None and refs
            and all(ref.get("state") == "CLOSED" for ref in refs)):
        return {"state": "settled", "recorded": False, "row": asking.row, "question": asking.question,
                "evidence": refs}
    new = existing is None
    key = existing or (asking.key if asking.key not in data["questions"] else uuid.uuid4().hex[:16])
    data["questions"].setdefault(key, {"row": asking.row, "decision": asking.decision,
                                              "final_message": asking.final_message,
                                              "question": asking.question, "asked_at": asking.asked_at,
                                              "owner_pane": os.environ.get("ORCA_PANE_KEY"),
                                              "session": asking.session or os.environ.get("CLAUDE_CODE_SESSION_ID"),
                                              "ask_id": asking.ask,
                                              "asked_in": str(worktree()), "covers": list(dict.fromkeys(asking.covers)),
                                              "answer": None})
    return {"id": key, **data["questions"][key],
            **({"merged_after_basis": merged} if new and (merged := merged_after(asking, refs)) else {})}


def merged_after(asking: Asking, refs: list[dict]) -> list[dict]:
    """For each issue the question names, its newest merged PRs (merged_prs) merged after what the question rests on
    (the ask's time), or after nothing when that time is not known: on 2026-11-11 a question rested on an issue
    comment that a PR merged the same day had made untrue."""
    issues = [ref for ref in refs if ref.get("type") == "issue" and ref.get("repo")]
    by_repo = {repo: merged_prs(repo, sorted({ref["number"] for ref in issues if ref["repo"] == repo}))
               for repo in dict.fromkeys(ref["repo"] for ref in issues)}
    basis = scan_sessions.parse_time(asking.based_at)
    found = []
    for ref in issues:
        prs = [pr for pr in by_repo[ref["repo"]].get(ref["number"], [])
               if basis is None or (at := scan_sessions.parse_time(pr["merged_at"])) is not None and at > basis]
        if prs:
            found.append({"issue": f"{ref['repo']}#{ref['number']}", "since": asking.based_at,
                          "prs": prs[:MERGED_SHOWN]})
    return found


def asked_under(data: dict, decision: str) -> str:
    """The id of the one open question asked under `decision`, else of the only question ever asked under it."""
    named = {key: item for key, item in data["questions"].items() if item["decision"] == decision}
    waiting = [key for key, item in named.items() if item["answer"] is None and not item.get("withdrawn_at")]
    found = waiting or list(named)
    if not found:
        raise ValueError(f"no question was asked under decision {decision}")
    if len(found) > 1:
        raise ValueError(f"decision {decision} names {len(found)} {'open ' if waiting else ''}questions; "
                         "give the id of one: "
                         + ", ".join(f"{key} ({named[key]['row']})" for key in found))
    return found[0]


def stamped_answer(answer: str, answered_at: str) -> str:
    """The answer with {answered_at} as its recorded time in JST. A text without it may not give a time of day
    ANSWER_CLOCK_SLACK minutes or more from that time: on 2026-11-10 an answer of 13:03 was recorded as 「12:1x」.
    Beside {answered_at}, other times (the hourly tab's, a time the user names) are left as written."""
    at = datetime.fromisoformat(answered_at).astimezone(JST)
    if ANSWERED_AT in answer:
        return answer.replace(ANSWERED_AT, f"{at:%H:%M}")
    minute = at.hour * 60 + at.minute
    off = []
    for found in CLOCK.finditer(answer):
        start = int(found[1]) * 60 + int(found[2]) * 10
        minutes = range(start, start + 10) if found[3] == "x" else (start + int(found[3]),)
        gap = min(min(abs(given - minute), 1440 - abs(given - minute)) for given in minutes)
        if gap >= ANSWER_CLOCK_SLACK:
            off.append(f"{found[0]} is {gap} minutes from answered_at {at:%H:%M} JST")
    if off:
        raise ValueError("; ".join(off) + f"; write {ANSWERED_AT} where the answer's time goes")
    return answer


def record_answer(data: dict, answering: Answering) -> dict:
    key = answering.id if answering.id is not None else asked_under(data, str(answering.decision))
    if key not in data["questions"]:
        raise ValueError(f"unknown question id: {key}")
    question = data["questions"][key]
    if question.get("withdrawn_at"):
        return {"id": key, **question, "state": "withdrawn", "recorded": False}
    # A recorded answer keeps its time, so recording the same text again changes nothing.
    when = (question["answered_at"] if question["answer"] is not None and not answering.replace_reason
            else recorded_at(answering.answered_at))
    answer = stamped_answer(answering.answer, when)
    earlier = {name: question.get(name) for name in ("answer", "answered_at", "answered_in")}
    if answering.replace_reason and question["answer"] is not None and question["answer"] != answer:
        # A checked reading replaces the answer; the one it replaces stays beside it with the reason.
        question.setdefault("replaced", []).append(
            {**earlier, "reason": answering.replace_reason, "replaced_at": stamp()})
        question["answer"] = None
    if question["answer"] is not None and question["answer"] != answer:
        raise ValueError("answer already recorded: " + json.dumps(earlier, ensure_ascii=False))
    if question["answer"] is None:
        question.update(answer=answer, answered_at=when,
                        answered_in=os.environ.get("ORCA_TERMINAL_HANDLE") or str(worktree()))
    return {"id": key, **question}


ASKED_KEYS = frozenset(("ask", "row", "final_message", "decision", "question", "asked_at", "covers", "session"))
ANSWERED_KEYS = frozenset(("id", "decision", "answer", "answered_at", "replace_reason"))


def line_text(item: dict, key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be non-empty text")
    return value.strip()


def line_object(raw: str, keys: frozenset[str], either: tuple[str, str]) -> dict:
    """A JSON Lines line as an object with only `keys` and exactly one of `either`."""
    try:
        item = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"not JSON: {error}") from error
    if not isinstance(item, dict):
        raise ValueError("not a JSON object")
    if unknown := sorted(set(item) - keys):
        raise ValueError("unknown keys: " + ", ".join(unknown))
    if (either[0] in item) == (either[1] in item):
        raise ValueError(f"give either {either[0]} or {either[1]}")
    return item


class Inbox:
    """The inbox asks and the saved scan's rows, read once, that an `asked-many` line names by ask id."""

    def __init__(self, scan: str | None) -> None:
        self.scan = scan

    @cached_property
    def asks(self) -> list[dict]:
        return [item for item in inbox_read.inbox_entries(inbox_read.paths()[0])
                if item.get("kind") == "ask" and item.get("id")]

    @cached_property
    def rows(self) -> list[dict]:
        if self.scan is None:
            raise ValueError("--from SCAN_JSON is needed to find the row of an ask")
        return scan_sessions.saved_rows(self.scan)

    def lane(self, ask_id: str) -> tuple[str, str, dict]:
        """The scan row of the worktree that sent the ask, found by its tab (which tells two Macs' same path apart),
        else by its repository, the ask's text (the lane's final message), and the ask."""
        found = [item for item in self.asks if inbox_read.names(str(item["id"]), ask_id)]
        if len(found) != 1:
            raise ValueError(f"ask {ask_id} names {len(found)} asks in the inbox")
        ask = found[0]
        named = [row for row in self.rows if Path(row["path"]).name == ask.get("worktree")]
        lanes = [row for row in named if ask.get("handle") and any(
            term.get("handle") == ask["handle"] for term in row.get("terminals") or [])] or [
            row for row in named if (ask.get("repo") or "") in ("", row.get("repo") or "")]
        if len(lanes) != 1:
            raise ValueError(f"ask {ask_id} from {ask.get('worktree')} matches {len(lanes)} scan rows; give row: "
                             + (", ".join(scan_sessions.row_identity(row) for row in lanes) or "(none)"))
        return scan_sessions.row_identity(lanes[0]), line_text(ask, "text"), ask


def asking_line(raw: str, inbox: Inbox) -> Asking:
    item = line_object(raw, ASKED_KEYS, ("ask", "row"))
    if "ask" in item and "final_message" in item:
        raise ValueError("an ask gives the final message; leave final_message out")
    row, message, ask = (inbox.lane(line_text(item, "ask")) if "ask" in item
                         else (line_text(item, "row"), line_text(item, "final_message"), {}))
    covers = item.get("covers", [])
    if not isinstance(covers, list) or not all(isinstance(key, str) and key.strip() for key in covers):
        raise ValueError("covers must be a list of lead decision keys")
    when = item.get("asked_at")
    if when is not None and not isinstance(when, str):
        raise ValueError("asked_at must be an ISO 8601 time with its timezone offset")
    return Asking(row=row, decision=line_text(item, "decision"), final_message=message,
                  question=line_text(item, "question"), asked_at=recorded_at(when),
                  covers=tuple(key.strip() for key in covers), ask=ask.get("id"), based_at=ask.get("ts"),
                  session=line_text(item, "session") if "session" in item else None)


def answering_line(raw: str) -> Answering:
    item = line_object(raw, ANSWERED_KEYS, ("id", "decision"))
    when = item.get("answered_at")
    if when is not None and not isinstance(when, str):
        raise ValueError("answered_at must be an ISO 8601 time with its timezone offset")
    if when is not None:
        recorded_at(when)
    key = "id" if "id" in item else "decision"
    return Answering(answer=line_text(item, "answer"), answered_at=when,
                     replace_reason=line_text(item, "replace_reason") if "replace_reason" in item else None,
                     **{key: line_text(item, key)})


def checked_lines[T](text: str, parse: Callable[[str], T]) -> list[tuple[int, T | str]]:
    """Each non-blank line, numbered from 1, as what `parse` makes of it or the reason it cannot."""
    checked: list[tuple[int, T | str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            checked.append((number, parse(raw)))
        except (OSError, ValueError, KeyError, TypeError) as error:
            checked.append((number, str(error)))
    return checked


def record_lines[T](lines: list[tuple[int, T | str]], record: Callable[[T], dict]) -> list[dict]:
    """Record every line, or raise naming each line that is wrong so the caller writes nothing."""
    results, wrong = [], []
    for number, line in lines:
        if isinstance(line, str):
            wrong.append((number, line))
            continue
        try:
            results.append(record(line))
        except ValueError as error:
            wrong.append((number, str(error)))
    if wrong or not results:
        raise ValueError("nothing was recorded" + ("".join(f"\nline {number}: {reason}" for number, reason in wrong)
                                                   or ": no lines were given"))
    return results


def unpaired(questions: list[hourly_runs.Question],
             records: list[dict]) -> tuple[list[hourly_runs.Question], set[int]]:
    """The questions left once each record is paired, most shared words first, with a question not yet paired, and
    the indexes of the records paired."""
    words = [set(answer_evidence.QUESTION_WORD.findall(question.question)) for question in questions]
    pairs = sorted(((len(words[asked] & set(answer_evidence.QUESTION_WORD.findall(record["question"]))), asked, kept)
                    for asked in range(len(questions)) for kept, record in enumerate(records)), reverse=True)
    paired: set[int] = set()
    used: set[int] = set()
    for _, asked, kept in pairs:
        if asked not in paired and kept not in used:
            paired.add(asked)
            used.add(kept)
    return [question for index, question in enumerate(questions) if index not in paired], used


def paired_calls(records: list[dict], path: Path) -> Iterator[
        tuple[list[hourly_runs.Question], list[dict], list[hourly_runs.Question]]]:
    """Each AskUserQuestion call of a transcript, in order, with the records paired with it and its questions no
    record accounts for. A call pairs (unpaired) with the records asked in the RECORDED_BEFORE up to it, only those of
    its session when a record names one, that no earlier call, answered or not, was paired with."""
    calls: dict[str, list[hourly_runs.Question]] = {}
    for question in hourly_runs.conversation(path).questions:
        calls.setdefault(question.tool_use_id, []).append(question)
    unused = [item for item in records if item.get("session") in (None, path.stem)]
    for asked in calls.values():
        at = scan_sessions.parse_time(asked[0].asked_at)
        kept = [item for item in unused if at is not None
                and (when := scan_sessions.parse_time(item["asked_at"])) is not None
                and at - RECORDED_BEFORE <= when <= at]
        left, used = unpaired(asked, kept)
        taken = [kept[index] for index in sorted(used)]
        unused = [item for item in unused if not any(item is record for record in taken)]
        yield asked, taken, left


def call_records(records: list[dict], transcript: Path, tool_use_id: str) -> list[dict]:
    """The records paired with the AskUserQuestion call `tool_use_id` of the transcript (paired_calls)."""
    return next((taken for asked, taken, _ in paired_calls(records, transcript)
                 if asked[0].tool_use_id == tool_use_id), [])


def unrecorded_questions(data: dict, projects: Path, workspaces: list[Path]) -> list[dict]:
    """The questions of each AskUserQuestion still waiting for its answer (no tool_result), read as
    `hourly_runs.py --questions` reads them, that no record accounts for (paired_calls), in the transcripts of
    `workspaces` (the current one and each --asked-in) and of the workspaces questions were recorded in (each record's
    `asked_in`: the triage workspace). A transcript pairs with the records of its workspace and those naming its
    session, wherever they were recorded (a run records the lead's question it read with --asked-in). A record
    rarely repeats AskUserQuestion's wording, so their count decides and the words only pick which is left: on
    2026-11-10 a run put three questions and recorded two, and no tool showed the third."""
    records = [item for item in data["questions"].values() if item.get("asked_in")]
    found = []
    for workspace in dict.fromkeys([*map(str, workspaces), *(item["asked_in"] for item in records)]):
        for path in answer_evidence.transcripts_of(projects, [Path(workspace)]):
            here = [item for item in records if item["asked_in"] == workspace or item.get("session") == path.stem]
            for asked, _, left in paired_calls(here, path):
                if not asked[0].result_received:
                    found.extend({"name": Path(workspace).name, "workspace": workspace, "session": path.stem,
                                  "tool_use_id": question.tool_use_id, "state": "unrecorded",
                                  "since": question.asked_at, "question": question.question}
                                 for question in left)
    return found


def pending(data: dict, decisions: dict[str, lead_decisions.Decision],
            answers: list[answer_evidence.Answer], now: datetime, entries: list[dict] | None = None,
            unrecorded: list[dict] | None = None) -> dict[str, list[dict]]:
    """Open items as lists, settled questions with evidence, transcript answers still missing from state, and the
    asked questions never recorded (`unrecorded`, from unrecorded_questions). An unanswered question carries
    `reissued` (withdrawn_by) from the inbox `entries`."""
    questions, settled = [], []
    references = question_references({key: item for key, item in data["questions"].items()
                                      if item["answer"] is None and not item.get("withdrawn_at")})
    for key, item in data["questions"].items():
        if item["answer"] is not None:
            continue
        view = {"id": key, **item}
        asked_at = scan_sessions.parse_time(item["asked_at"])
        covered = covering_answers(item, decisions)
        if item.get("withdrawn_at"):
            settled.append({**view, "state": "withdrawn", "since": item["withdrawn_at"],
                            "reason": item["withdrawal_reason"]})
        elif covered and len(covered) == len(item["covers"]):
            settled.append({**view, "state": "settled", "since": max(
                covered, key=lambda answer: scan_sessions.parse_time(answer.decided_at) or now).decided_at,
                            "evidence": [{"decision": answer.decision, "decided_at": answer.decided_at,
                                          "answer": answer.answer} for answer in covered]})
        elif (refs := references.get(key, [])) and all(ref.get("state") == "CLOSED" for ref in refs):
            settled.append({**view, "state": "settled", "since": max(ref.get("updatedAt") or "" for ref in refs),
                            "evidence": refs})
        else:
            unrecorded_answers = [{"at": answer.at.isoformat(), "source": answer.source, "text": answer.text}
                                  for answer in answers if not answer.free_text
                                  and asked_at and answer.at >= asked_at and item["question"] in answer.answered]
            questions.append({**view, "state": "unanswered", "since": item["asked_at"],
                              "prs": references.get(key, []),
                              "age_minutes": int((now - asked_at).total_seconds() // 60) if asked_at else None,
                              "unrecorded_answers": unrecorded_answers,
                              "reissued": withdrawn_by(question_ask(item, entries or []), entries or [])})
    return {
        "questions": questions, "settled": settled,
        "handoffs": [{"id": key, **item} for key, item in data["handoffs"].items() if item["delivered_at"] is None],
        "requests": [{"id": key, **item} for key, item in data["requests"].items() if item["closed_at"] is None],
        "user_waits": [{"id": key, **item} for key, item in data["user_waits"].items() if item["closed_at"] is None],
        "left": [{"row": row, **item} for row, item in data["left"].items()],
        "unrecorded_questions": unrecorded or [],
    }


def change(data: dict, args: argparse.Namespace) -> dict:
    if args.action == "appoint-lead":
        at = datetime.fromisoformat(recorded_at(args.appointed_at)).astimezone(timezone.utc).isoformat()
        data["lead"] = {"worktree": str(args.worktree.expanduser().resolve()), "appointed_at": at}
        return data["lead"]
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
    if args.action == "user-wait":
        if any(not command.strip() for command in args.check_command):
            raise ValueError("--check-command must not be empty")
        checks = [*(pr_check(ref, name) for ref, name in args.pr_check), *map(secret_check, args.secret),
                  *({"kind": "command", "command": command, "cwd": str(worktree())}
                    for command in args.check_command)]
        if not checks:
            raise ValueError("give --pr-check, --secret or --check-command: a user wait needs a check")
        text, row = file_text(args.text_file), owner_name(args.row) if args.row is not None else None
        key = identifier("user-wait", text, row, checks)
        data["user_waits"].setdefault(key, {"text": text, "row": row, "started_at": recorded_at(args.started_at),
                                            "checks": checks, "closed_at": None, "closure_evidence": None})
        return {"id": key, **data["user_waits"][key]}
    if args.action == "user-wait-close":
        wait = data["user_waits"][args.id]
        evidence = file_text(args.evidence_file)
        if wait["closed_at"] is not None and wait["closure_evidence"] != evidence:
            raise ValueError("user wait already closed; inspect the state before changing it")
        if wait["closed_at"] is None:
            wait.update(closed_at=recorded_at(args.closed_at), closure_evidence=evidence)
        return {"id": args.id, **wait}
    if args.action == "leave":
        data["left"][args.row] ={"final_message": file_text(args.final_message_file), "left_at": stamp()}
        return data["left"][args.row]
    if args.action == "asked":
        decision = args.decision.strip()
        if not decision:
            raise ValueError("decision is empty")
        asking = Asking(row=args.row, decision=decision, final_message=file_text(args.final_message_file),
                        question=file_text(args.question_file), asked_at=recorded_at(args.asked_at),
                        covers=tuple(args.covers), session=args.session)
        return record_question(data, asking, new_references(data, [asking]))
    if args.action == "answered":
        return record_answer(data, Answering(
            answer=file_text(args.answer_file), id=args.id, answered_at=args.answered_at,
            decision=args.decision.strip() if args.decision is not None else None,
            replace_reason=file_text(args.replace_reason_file) if args.replace_reason_file else None))
    if args.action == "withdraw":
        question = data["questions"][args.id]
        reason = file_text(args.reason_file)
        if args.replaced_by is not None:
            replacement = data["questions"].get(args.replaced_by)
            if replacement is None or args.replaced_by == args.id or replacement.get("withdrawn_at"):
                raise ValueError("--replaced-by must name another existing question that is not withdrawn")
            if question.get("replaced_by") not in (None, args.replaced_by):
                raise ValueError("question already has a different replacement")
        if question["answer"] is not None:
            raise ValueError("question already has an answer")
        if question.get("withdrawn_at") and question["withdrawal_reason"] != reason:
            raise ValueError("question already withdrawn; inspect its reason before changing it")
        if not question.get("withdrawn_at"):
            question.update(withdrawn_at=stamp(), withdrawal_reason=reason)
        if args.replaced_by is not None:
            question["replaced_by"] = args.replaced_by
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


def reissue_notices(items: list[dict]) -> list[str]:
    """Set `reissued_by` on each answered question whose ask later asks of its lane withdrew or name
    (inbox_read.reissues), and name each: on 2026-11-11 an answer to an ask withdrawn and asked again with other
    choices was recorded and relayed as it stood. A failed read of the inbox is named; the answers stay recorded."""
    try:
        entries = inbox_entries()
    except OSError as error:
        return [f"受信箱を読めず、答えた問いの ask が出し直されたかを確かめていない: {error}"]
    notices = []
    for item in items:
        if (ask := question_ask(item, entries)) is None or not (found := inbox_read.reissues(ask[0], entries)):
            continue
        item["reissued_by"] = [reissue.view() for reissue in found]
        notices.extend(f"注意: 問い {item['id']} の ask {ask[0][:8]} を、後から同じ担当の ask {reissue.id[:8]}"
                       f"（{reissue.ts}）が" + ("取り下げて出し直しています" if reissue.withdrawn else "名指ししています")
                       + "。答えが今の問いに合うかを確かめてから担当へ送ってください" for reissue in found)
    return notices


def summary(items: list[dict], now: datetime, kind: str = "") -> str:
    lines = []
    for item in items:
        row = item.get("row") or item.get("owner") or item.get("from") or ""
        since = item.get("since") or next((item[key] for key in
                    ("asked_at", "promised_at", "opened_at", "started_at", "left_at") if item.get(key)), "")
        at = scan_sessions.parse_time(since)
        age = item.get("age_minutes", int((now - at).total_seconds() // 60) if at else None)
        name = item.get("name") or row.rsplit("::", 1)[-1].rstrip("/").rsplit("/", 1)[-1] or "-"
        state = item.get("state") or {"handoffs": "undelivered", "requests": "open", "user_waits": "user_wait",
                                      "left": "left"}.get(kind, kind)
        ask_id = (item.get("ask") or {}).get("id")
        question = (item.get("question") or item.get("original") or item.get("task") or item.get("text")
                    or item.get("final_message"))
        line = (f"{name} | {state} | since={since or '-'} | age_minutes={age} "
                f"| {f'ask={ask_id}' if ask_id else f'id={item.get('id') or '-'}'} | {excerpt(question)}")
        if item.get("evidence"):
            line += " | 済み: " + "; ".join(
                f"{proof['decision']}@{proof['decided_at']}" if "decision" in proof
                else f"{proof['repo']}#{proof['number']} {proof['state']}@{proof.get('updatedAt') or '-'}"
                for proof in item["evidence"])
        if item.get("pr"):
            line += (f" | PR {item['pr'].get('state') or '-'} head={item['pr'].get('head') or '-'}"
                     f"@{item['pr'].get('updatedAt') or '-'}")
        for pr in item.get("prs") or []:
            line += (f" | {pr['repo'] or '（リポジトリ不明）'}#{pr['number']} {pr['state'] or '-'} "
                     f"head={pr['head'] or '-'}@{pr['updatedAt'] or '-'}")
        if "checks_done" in item:
            line += " | 確かめ: " + "; ".join(map(check_line, item["checks"]))
            if item["checks_done"]:
                line += " | 確かめ方の条件がそろった（閉じる前に確かめる）"
        elif item.get("checks"):
            line += " | 確かめ方: " + "; ".join(
                f"{check['repo']}#{check['number']} {check['name']}" if check["kind"] == "pr_check"
                else f"{check['name']}: {', '.join(check['repos'])}" for check in item["checks"])
        if item.get("reissued"):
            line += " | 出し直しあり（" + ", ".join(reissue["id"] for reissue in item["reissued"]) + "）"
        if claimed := (item.get("ask") or {}).get("claimed"):
            if claimed.get("inactive_minutes") is not None:
                line += f" | 伺っていない（claim のみ、{claimed['inactive_minutes']} 分）{claimed['by']}"
            else:
                line += f" | 伺い中 {claimed['by']}（{claimed['at']}〜）。重ねて伺わない"
        if item.get("related_issues_closed"):
            line += " | 関わる issue が閉じた（" + ", ".join(
                f"{ref['ref']} {ref['state']}" for ref in item["related_issues"]) + "）"
        if item.get("merge_question"):
            line += " | merge の問い"
        if review := item.get("review"):
            line += (f" | review {review['head']}{'（今の head ではない）' if review['head_matches'] is False else ''}: "
                     f"{review['verdict'][:60]}")
        if item.get("answer_age_minutes") is not None:
            line += f" | answer_age_minutes={item['answer_age_minutes']}"
        if item.get("withdrawal_reason"):
            line += " | " + excerpt(item["withdrawal_reason"])
        if item.get("unrecorded_answers"):
            line += " | 未記録の答え: " + "; ".join(
                f"{answer['at']} {excerpt(answer['text'])}" for answer in item["unrecorded_answers"])
        lines.append(line)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", default=DEFAULT_STATE, help="shared hourly state JSON path")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("show", help="read all saved decisions, questions, handoffs, requests, and user waits")
    pending_command = commands.add_parser(
        "pending", help="list open items with their ids (an unanswered question with the prs its #numbers name and the "
                        "later asks that withdrew its ask as reissued), settled questions with evidence, unrecorded "
                        "answers, and as unrecorded_questions each question an AskUserQuestion of the triage "
                        "workspace still waits on that no record accounts for")
    pending_command.add_argument("--summary", action="store_true", help="print one line per item with its age and question")
    answer_evidence.add_arguments(pending_command)
    get = commands.add_parser("get", help="read one saved question, handoff, request, user wait, or left row")
    get.add_argument("--id", required=True)
    appoint = commands.add_parser("appoint-lead", help="record the appointed lead's worktree, replacing an earlier one")
    appoint.add_argument("--worktree", type=Path, required=True, metavar="PATH")
    appoint.add_argument("--appointed-at", metavar="ISO", help="original ISO 8601 appointment time (saved in UTC)")
    needs_owner = commands.add_parser(
        "requests-needing-owner",
        help="print {needs_owner, closed_issues}: open requests without a live owner, and apart from them the "
             "requests whose GitHub issue gh no longer lists as open; first record each open GitHub issue of the "
             f"repositories in {ISSUE_REPOS} beside the state file once, owned by the worktree Orca links to it "
             "or to a PR that closes it")
    needs_owner.add_argument("--live-owner", action="append", default=[],
                             help="active scan row identity (PATH or ENVIRONMENT::PATH); repeat per owner")
    needs_owner.add_argument("--from", dest="from_file", metavar="SCAN_JSON",
                             help="read the rows scan_sessions.py printed as JSON instead of Orca: each row with an "
                                  "agent tab whose class (the verdict's, else the scan's) is not finished is a live "
                                  "owner, added to --live-owner")
    needs_owner.add_argument("--verdicts", default=scan_sessions.VERDICTS_FILE,
                             help="with --from, the careful reads' verdict store whose verdicts the rows take, "
                                  "including those classed after the scan was saved (default: %(default)s)")
    needs_owner.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                             help="Orca CLI executable, read without --from (default: $ORCA_CLI_COMMAND or orca); "
                                  f"a paired Mac that does not answer within {REMOTE_ORCA_TIMEOUT} s is skipped")
    stale = commands.add_parser(
        "stale-asks", help="print, longest waiting first, waiting rows, rows blocked on Miura, and rows an unanswered "
                           "handed-off ask key names; a question is unasked "
                           "(not in open-asks; choices it sent as info are listed), unanswered (an open or last-turn "
                           "ask without an answer that proves it, marked or not), stopped_after_answer (answered "
                           "after its last message), or settled (a lead decision key ask:<name>::… has a record "
                           "after its last message, given as evidence), with the lane's ask, the answer, candidates "
                           "that name only the lane or its PR numbers, age_minutes, and the ask's 関わる issue "
                           "(related_issues with their states; related_issues_closed when every one is closed or "
                           "merged, 関わる issue が閉じた in --summary) and the later asks that withdrew its ask "
                           "(reissued, 出し直しあり in --summary); each AskUserQuestion of the --asked-in transcripts "
                           "closed unanswered since the newest saved handoff, as rejected; and each open user-wait, "
                           "its checks read now with gh, as user_wait")
    stale.add_argument("--from", dest="from_file", metavar="SCAN_JSON", required=True,
                       help="the rows scan_sessions.py printed as JSON")
    stale.add_argument("--verdicts", default=scan_sessions.VERDICTS_FILE,
                       help="the careful reads' verdict store whose verdicts the rows take (default: %(default)s)")
    stale_output = stale.add_mutually_exclusive_group()
    stale_output.add_argument("--summary", action="store_true",
                               help="print one line per row with its age, ask id, and question")
    stale_output.add_argument("--out", type=Path, metavar="FILE",
                               help="atomically write JSON to FILE instead of stdout")
    stale.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                       help="Orca CLI used to check the current state of claimed questions")
    stale.add_argument("--handed-off-file", type=Path, default=lead_decisions.HANDED_OFF_LOG,
                       help="the handed-off keys whose unanswered asks remain visible (default: %(default)s)")
    # Imported here: handoff imports this module.
    import handoff
    stale.add_argument("--handoffs-dir", type=Path, default=handoff.HANDOFFS,
                       help="saved handoffs: questions closed unanswered since the newest are listed as rejected "
                            "(default: %(default)s)")
    answer_evidence.add_arguments(stale)
    stale.epilog = "Without --asked-in, read the appointed lead's worktree from shared state."
    user_wait = commands.add_parser(
        "user-wait", help="record a wait on the user's own action (a CLA to sign, a secret to register) with how to "
                          "check it; stale-asks reads each check now, and recording the same wait again changes nothing")
    user_wait.add_argument("--text-file", required=True, help="what the user is to do, in a UTF-8 file")
    user_wait.add_argument("--row", help="the scan row identity (PATH or ENVIRONMENT::PATH) of the lane it holds up")
    user_wait.add_argument("--started-at", help="original ISO 8601 time the wait began (default: now)")
    user_wait.add_argument("--pr-check", nargs=2, action="append", default=[], metavar=("OWNER/NAME#N", "CHECK"),
                           help="a check run on that PR that passes once the user acts, e.g. license/cla; repeat")
    user_wait.add_argument("--secret", nargs="+", action="append", default=[], metavar="NAME_THEN_REPO",
                           help="a secret name, then the OWNER/NAME of each repository that must hold it; repeat")
    user_wait.add_argument("--check-command", action="append", default=[], metavar="COMMAND",
                           help="trusted read-only /bin/sh command; run on every stale-asks in this workspace, "
                                f"exit 0 means done, timeout {GH_TIMEOUT}s; repeat, all checks must pass")
    user_wait_close = commands.add_parser("user-wait-close", help="close a user wait once its result is seen")
    user_wait_close.add_argument("--id", required=True)
    user_wait_close.add_argument("--evidence-file", required=True)
    user_wait_close.add_argument("--closed-at", help="original ISO 8601 time when importing a closure")
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
    asked = commands.add_parser(
        "asked", help="record the exact question before presenting it; for each issue it names, print the PRs that "
                      "close or name it merged after what the question rests on (merged_after_basis)")
    asked.add_argument("--row", required=True)
    asked.add_argument("--decision", required=True, help="stable subject and action, e.g. merge:PR#10")
    asked.add_argument("--final-message-file", required=True)
    asked.add_argument("--question-file", required=True)
    asked.add_argument("--asked-at", help="original ISO 8601 time when importing a question")
    asked.add_argument("--session", help="the session that put it, when importing a question another session put "
                                         "(an unrecorded question's session in pending); default: this one")
    asked.add_argument("--covers", action="append", default=[], metavar="KEY",
                       help="a lead decision key this question covers; repeat per key")
    asked_many = commands.add_parser(
        "asked-many", help='record several questions from JSON Lines of {"decision", "question", "asked_at"?, '
                           '"covers"?: [KEY, ...], "session"?} with either "ask": an inbox ask id (or its first 8 or more '
                           'characters), whose lane and text become the row and final message, or "row" and '
                           '"final_message"; every line is checked first, and a wrong one writes nothing. A line '
                           'whose #numbers are all CLOSED is not recorded: each is named after the output '
                           '(記録しなかった: 行 N), and the exit code is 2')
    asked_many.add_argument("--file", type=Path, help="UTF-8 JSON Lines file (default: standard input)")
    asked_many.add_argument("--from", dest="from_file", metavar="SCAN_JSON",
                            help="the rows scan_sessions.py printed as JSON, where an ask's worktree (and its tab, "
                                 "when the same path is on two Macs) finds its row; needed for an \"ask\" line")
    answered = commands.add_parser(
        "answered", help="record the user's answer before acting on it; name the later asks of the lane that withdrew "
                         "or name the question's ask (reissued_by), to check the answer still fits")
    which = answered.add_mutually_exclusive_group(required=True)
    which.add_argument("--id")
    which.add_argument("--decision", metavar="KEY",
                       help="the asked --decision of the one open question asked under it (else of the only "
                            "question asked under it); several open ones are refused with their ids")
    answered.add_argument("--answer-file", required=True,
                          help=f"the exact answer; {ANSWERED_AT} in it becomes the recorded time (JST HH:MM), and "
                               f"without it a time of day {ANSWER_CLOCK_SLACK}+ minutes off that time is refused")
    answered.add_argument("--answered-at", help="original ISO 8601 time when importing an answer")
    answered.add_argument("--replace-reason-file",
                          help="replace a different recorded answer with this checked one, keeping the earlier "
                               "answer, its time and tab under `replaced` with this file's reason")
    answered_many = commands.add_parser(
        "answered-many", help='record several answers from JSON Lines of {"answer", "answered_at"?, '
                              '"replace_reason"?} with either "id" or "decision" (as answered --decision), the answer '
                              'checked as answered --answer-file is; every line is checked first, and a wrong one '
                              'writes nothing')
    answered_many.add_argument("--file", type=Path, help="UTF-8 JSON Lines file (default: standard input)")
    withdraw = commands.add_parser("withdraw", help="close an unanswered question with a reason, without a user's answer")
    withdraw.add_argument("--id", required=True)
    withdraw.add_argument("--reason-file", required=True)
    withdraw.add_argument("--replaced-by", metavar="ID", help="the existing question that replaces this one")
    promise = commands.add_parser("promise", help="remember when one lane's result should go to another")
    promise.add_argument("--from-row", required=True)
    promise.add_argument("--to-row", required=True)
    promise.add_argument("--condition", required=True)
    promise.add_argument("--task", required=True)
    delivered = commands.add_parser("delivered", help="mark a promised task sent to its lane")
    delivered.add_argument("--id", required=True)
    delivered.add_argument("--delivery", required=True, choices=("queued", "turn_started"))
    comment = commands.add_parser(
        "comment", help="write this workspace's Orca comment from the state: 「要判断: 」 and the first line of each "
                        "question asked here still open, then the run's line; exit 1 when Orca does not take it")
    comment.add_argument("--result", help="what this run did: the run's line becomes 「HH:MM の定時: RESULT」, HH:MM "
                                          "the local time this Claude Code session started; without it the saved "
                                          "line stays")
    for command in (asked, asked_many):
        command.add_argument("--comment", action=argparse.BooleanOptionalAction,
                             help="write the asking workspace's Orca comment as `comment` does (default: in the "
                                  "workspace of the hourly triage automation, read from an Orca tab)")
    for command in (asked, asked_many, answered, answered_many, withdraw, comment):
        command.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                             help="Orca CLI that writes the comment (default: $ORCA_CLI_COMMAND or orca); answered "
                                  "and withdraw rewrite the comment of the workspace the question was asked in, "
                                  "once this script has written it")
    args = parser.parse_args(argv)
    path = Path(args.state_file).expanduser()
    if args.action == "stale-asks":
        try:
            data = read(path)
            if not args.asked_in and (lead := appointed_lead(data)):
                args.asked_in = [lead]
            rows = scan_sessions.with_verdicts(scan_sessions.saved_rows(args.from_file),
                                               scan_sessions.absorb_verdicts(args.verdicts, write=False))
            entries = inbox_read.inbox_entries(inbox_read.paths()[0])
            answers_log = inbox_read.answers_path()
            asks = inbox_read.open_asks(entries, inbox_read.answered_ids(answers_log))
            open_ids = frozenset(ask.report["id"] for ask in asks)
            claims = inbox_read.read_claims(inbox_read.claims_path())
            open_keys = {key for ask in asks for key in (
                ask.report["id"], *(f"{ask.report['id']}:{number}" for number in ask.open_questions))}
            claims = {key: claim for key, claim in claims.items() if key in open_keys}
            claims = inbox_read.checked_claims(claims, datetime.now(timezone.utc), args.orca, args.claude_projects)
            decided = lead_decisions.history(lead_decisions.records(args.decisions_file.expanduser()))
            handed = list({item.decision: item for item in lead_decisions.handed_off(args.handed_off_file.expanduser())
                           if item.decision not in decided}.values())
            since = handoff.last_sent_at(args.handoffs_dir.expanduser())
            rejected = [{**item, "worktree": worktree.name} for worktree in answer_evidence.asking_worktrees(args)
                        for item in answer_evidence.rejected_questions(answer_evidence.transcripts_of(
                            args.claude_projects.expanduser(), [worktree]), since)]
            result = stale_asks(rows, entries, open_ids, answer_evidence.from_arguments(args, answers_log),
                                datetime.now(timezone.utc), state=data, handed=handed,
                                claimed={ask.report["id"]: claim for ask in asks
                                         if (claim := inbox_read.ask_claim(ask, claims))},
                                decided=decided, rejected=rejected)
            result = sorted([*result, *user_waits_now(data, datetime.now(timezone.utc))],
                            key=lambda item: -item["age_minutes"])
            identities = {item["row"] for item in result if item.get("row")}
            prs = row_prs([row for row in rows if scan_sessions.row_identity(row) in identities])
            for item in result:
                item["pr"] = prs.get(item.get("row"), {"state": None, "head": None, "updatedAt": None})
            with_issue_states(result)
        except READ_ERRORS as error:
            print(f"hourly state: {error}", file=sys.stderr)
            return 1
        try:
            if args.summary:
                print(summary(result, datetime.now(timezone.utc)))
            else:
                json_output.emit(result, args.out)
        except OSError as error:
            print(f"hourly state: {error}", file=sys.stderr)
            return 1
        return 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        live: frozenset[str] = frozenset()
        intake: Intake | None = None
        if args.action == "requests-needing-owner":
            # A saved scan predates the verdicts orca-row-classify wrote after it; take them as scan --from does.
            host: WorktreeHost = (SavedScan(scan_sessions.with_verdicts(
                scan_sessions.saved_rows(args.from_file), scan_sessions.absorb_verdicts(args.verdicts, write=False)))
                if args.from_file else OrcaHost(args.orca))
            live = frozenset(owner_name(value) for value in args.live_owner) | host.live_owners()
            intake = issue_intake(path, host)
        given = ((args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read())
                 if args.action in ("asked-many", "answered-many") else "")
        notices: list[str] = []  # printed on stderr after the output
        skipped = False
        here = str(worktree())
        comment_here: bool = args.action == "comment"
        if args.action in ("asked", "asked-many"):
            comment_here = args.comment
            if comment_here is None:
                try:
                    comment_here = hourly_workspace(args.orca, here)
                except READ_ERRORS as error:
                    comment_here = False
                    notices.append(f"定時の作業場かを Orca から読めず、Orca のコメントを書いていない: {error}")
        with (path.parent / (path.name + ".lock")).open("a+") as lock:
            read_only = ("show", "get", "pending", "left-now", "requests-needing-owner")
            writes = args.action not in read_only or intake is not None
            fcntl.flock(lock, fcntl.LOCK_EX if writes else fcntl.LOCK_SH)
            data = read(path)
            if intake is not None:
                import_issues(data, intake)
            left_matches = False
            if args.action == "left-now":
                left_matches = data["left"].get(args.row, {}).get("final_message") == file_text(args.final_message_file)
                result = {"left": left_matches}
            elif args.action == "requests-needing-owner":
                result = owner_report(data, live, intake)
            elif args.action == "pending":
                workspaces = [*answer_evidence.asking_worktrees(args), worktree()]
                args.asked_in.extend(Path(item["asked_in"]) for item in data["questions"].values()
                                     if item.get("asked_in"))
                if lead := appointed_lead(data):
                    args.asked_in.append(lead)
                result = pending(data, lead_decisions.latest(lead_decisions.records(args.decisions_file.expanduser())),
                                 answer_evidence.from_arguments(args, inbox_read.answers_path()), datetime.now(timezone.utc),
                                 inbox_entries(), unrecorded_questions(data, args.claude_projects.expanduser(), workspaces))
            elif args.action == "get":
                result = next(({"id": args.id, **data[kind][args.id]} for kind in
                               ("questions", "handoffs", "requests", "user_waits", "left") if args.id in data[kind]),
                              None)
                if result is None:
                    raise ValueError(f"unknown id: {args.id}")
            elif args.action == "asked-many":
                inbox = Inbox(args.from_file)
                askings = checked_lines(given, lambda raw: asking_line(raw, inbox))
                references = new_references(data, [line for _, line in askings if isinstance(line, Asking)])
                result = record_lines(askings, lambda asking: record_question(data, asking, references))
                # record_lines returned, so every line was read: the results follow the lines.
                unrecorded = [f"記録しなかった: 行 {number}（問いの #番号がすべて閉じている: " + "、".join(
                    f"{ref['repo']}#{ref['number']} {ref['state']}" for ref in item["evidence"]) + "）"
                    for (number, _), item in zip(askings, result) if item.get("recorded") is False]
                notices.extend(unrecorded)
                skipped = bool(unrecorded)
            elif args.action == "answered-many":
                result = record_lines(checked_lines(given, answering_line),
                                      lambda answering: record_answer(data, answering))
            elif args.action == "comment":
                entry = data["comments"].setdefault(here, {"run": None})
                if args.result is not None:
                    entry["run"], why = run_line(args.result)
                    notices.extend([why] if why else [])
                view: dict[str, Any] = {"workspace": here, "comment": workspace_comment(data, here)}
                result = view
            else:
                result = data if args.action == "show" else change(data, args)
            items = result if isinstance(result, list) else [result]
            # A question answered or withdrawn from another tab (the lead's) rewrites the comment of the workspace it
            # was asked in, once this script writes that comment.
            commented = ([item["asked_in"] for item in items if item.get("asked_in") in data["comments"]]
                         if args.action in ("answered", "answered-many", "withdraw") else [here] if comment_here else [])
            for workspace in commented:
                data["comments"].setdefault(workspace, {"run": None})
            if writes:
                save(path, data)
            for workspace in dict.fromkeys(commented):
                if why := write_comment(args.orca, workspace, workspace_comment(data, workspace)):
                    if args.action == "comment":
                        raise ValueError(why)
                    notices.append(why + "。記録は済んでいる。その作業場で hourly_state.py comment を実行して書き直す")
        if args.action in ("asked", "asked-many"):
            notices[:0] = [f"注意: {found['issue']} を閉じる・名指しする PR が、問いの根拠（{found['since'] or '時刻不明'}）"
                           "の後に merge されています: " + "、".join(f"{pr['pr']} {pr['merged_at']}" for pr in found["prs"])
                           + "。問いがまだ成り立つかを確かめてから伺ってください"
                           for item in items for found in item.get("merged_after_basis") or []]
        if args.action in ("answered", "answered-many"):
            notices.extend(reissue_notices(items))
        if args.action in ("asked", "asked-many", "answered", "answered-many"):
            for item in items:
                if item.get("withdrawn_at"):
                    skipped = True
                    notices.append(f"記録しなかった: 既存 {item['id']}（取り下げ済み）"
                                   + (f"。置き換え先: {item['replaced_by']}" if item.get("replaced_by") else ""))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"hourly state: {error}", file=sys.stderr)
        return 1
    if args.action == "pending" and args.summary and isinstance(result, dict):
        print("\n".join(line for kind, items in result.items()
                        if (line := summary(items, datetime.now(timezone.utc), kind))))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    for notice in notices:
        print(f"hourly state: {notice}", file=sys.stderr)
    return 1 if args.action == "left-now" and not left_matches else 2 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
