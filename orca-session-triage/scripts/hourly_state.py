#!/usr/bin/env python3
"""Keep decisions shared by successive hourly triage sessions in one JSON file.

Use --state-file for an isolated copy. Mutations lock beside the file and replace
it atomically, so a waiting session and a new run cannot lose each other's edits.

`stale-asks --from SCAN_JSON` sets each waiting row of a saved scan against the lane's inbox
reports and the answers answer_evidence.py finds, and names the questions left in front of no one.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
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
import inbox_read
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


def worktree() -> Path:
    value = os.environ.get("ORCA_WORKTREE_ID") or os.environ.get("ORCA_WORKTREE_PATH")
    return Path(value.split("::", 1)[-1]).expanduser().absolute() if value else Path.cwd()


def appointed_lead(state: dict) -> Path | None:
    lead = state.get("lead") or {}
    return Path(lead["worktree"]) if lead.get("worktree") else None


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


def blocked_prs(rows: list[dict]) -> dict[str, dict]:
    wanted = [(scan_sessions.row_identity(row), scan_sessions.github_of(row),
               (row.get("pr") or row.get("linked_pr") or {}).get("number"))
              for row in rows if blocked_by_user(row)]
    repos = {repo: list(dict.fromkeys(number for _, slug, number in wanted if slug == repo and number))
             for _, repo, _ in wanted if repo}
    states = {repo: lead_decisions.github_pr_states(repo, numbers) for repo, numbers in repos.items() if numbers}
    return {identity: {"state": found.state, "updatedAt": found.updated_at}
            for identity, repo, number in wanted if (found := (states.get(repo) or {}).get(number))}


def stale_asks(rows: list[dict], entries: list[dict], open_ids: frozenset[str],
               answers: list[answer_evidence.Answer], now: datetime, *, state: dict,
               handed: list[lead_decisions.HandOff], prs: dict[str, dict],
               claimed: dict[str, inbox_read.Claim] | None = None) -> list[dict]:
    """Waiting rows, rows blocked on the user, and rows an unanswered handoff key names, longest waiting first.
    The lane's asks that count are
    those still open and those it sent in its last turn:
    - `unanswered`: one of them has no answer that proves it answered (answer_evidence.py), whether it is open
      or was marked answered; the oldest is `ask`;
    - `stopped_after_answer`: they are answered, one after the lane's last message, and the lane has not spoken
      since; `answer` is the newest such answer, `since` the first;
    - `unasked`: they were answered before the lane's last message, or it has none, so open-asks does not show
      its question; `choices_in_info` lists choices it sent as info since its last turn began.
    `candidates` holds the newest answers that name only the lane or its PR numbers, for the lead to check: the
    lead may ask the user a waiting row's question itself. An unanswered item's `ask.claimed` names the tab that
    claimed it with `inbox_read.py claim` and is asking the user now: `claimed` holds, by ask id, the claim on each
    open ask itself or on every one of its open questions (inbox_read.ask_claim).
    """
    found: list[tuple[datetime, dict[str, Any]]] = []
    entries = [item for item in entries if not inbox_read.is_handoff(item)]
    shared = answer_evidence.shared_questions(entries)
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
        lane = [(at, item) for item in entries if item.get("worktree") == name
                and item.get("repo") in (None, "", row.get("repo"))
                and (at := scan_sessions.parse_time(item.get("ts"))) is not None]
        keys = {item["id"]: [handoff.decision for handoff in handed if (key := answer_evidence.key_ask_id(handoff.decision))
                            and answer_evidence.names_ask_id(key, str(item["id"]))]
                for _, item in lane if item.get("kind") == "ask" and item.get("id")}
        if (((row.get("verdict") or {}).get("class") or row.get("class")) != "waiting"
                and not blocked_by_user(row) and not any(keys.values())):
            continue
        counted = [(at, item, answer_evidence.answers_to(item, answers, shared=shared)) for at, item in lane
                   if item.get("kind") == "ask" and item.get("id")
                   and (item["id"] in open_ids or keys.get(item["id"]) or began is not None and at >= began)]
        comment = row.get("comment") or ""
        question = row_question(row, final)
        view = {"row": identity, "name": scan_sessions.row_name(row),
                "question": excerpt(question), "merge_question": scan_sessions.asks_merge(question, comment),
                "review": scan_sessions.review_result(row, scan_sessions.github_of(row)),
                "ask": None, "answer": None, "candidates": [], "choices_in_info": []}
        if blocked_by_user(row):
            view["pr"] = prs.get(identity, {"state": (row.get("pr") or {}).get("state"), "updatedAt": None})
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
                    "decisions": keys.get(ask["id"], []),
                    "ask": {**report_view(ask), "marked": ask["id"] not in open_ids,
                            "claimed": (claim.view() if (claim := (claimed or {}).get(ask["id"])) else None)},
                    "candidates": [answer_view(answer) for answer in answer_evidence.related_to(ask, answers)[-3:]]}))
            continue
        later = [(answer, item) for _, item, proof in counted for answer in proof
                 if said_at is None or answer.at > said_at]
        if later:
            first = min(answer.at for answer, _ in later)
            answer, ask = max(later, key=lambda pair: pair[0].at)
            asked = "\n".join(answer_evidence.questions_of(ask)) or ask.get("text")
            found.append((first, {**view, "state": "stopped_after_answer", "since": first.isoformat(),
                                  "question": excerpt(asked) or view["question"],
                                  "merge_question": scan_sessions.asks_merge(asked or question, comment),
                                  "ask": {**report_view(ask), "marked": ask["id"] not in open_ids},
                                  "answer": answer_view(answer)}))
            continue
        since = said_at or scan_sessions.parse_time(row.get("scanned_at")) or now
        asked_directly = answer_evidence.related_to(
            {"worktree": name, "ts": final["at"], "text": f"{view['question']}\n{final['text']}"},
            answers) if said_at else []
        choices = [report_view(item) for at, item in lane if item.get("kind") == "info" and began and at >= began
                   and CHOICE.search(str(item.get("text") or ""))]
        found.append((since, {**view, "state": "unasked", "since": final["at"] or since.isoformat(),
                              "candidates": [answer_view(answer) for answer in asked_directly[-3:]],
                              "choices_in_info": choices}))
    found.sort(key=itemgetter(0))
    return [{**item, "age_minutes": int((now - at).total_seconds() // 60),
             **({"answer_age_minutes": int((now - scan_sessions.parse_time(item["answer"]["at"])).total_seconds() // 60)}
                if item["answer"] else {})} for at, item in found]


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


def closed_references(row: str, question: str) -> list[dict]:
    """The question's #numbers in its checkout's origin, only when every reference is CLOSED. A row on another
    Mac (ENVIRONMENT::path) gives none: the same path here may hold another repository."""
    numbers = list(dict.fromkeys(int(number) for number in re.findall(r"#(\d+)\b", question)))
    repo = origin_slug(row) if numbers and "::" not in row else None
    if not repo:
        return []
    owner, name = repo.split("/")
    fields = " ".join(f"item_{number}: issueOrPullRequest(number: {number}) {{ "
                      "... on Issue { state updatedAt } ... on PullRequest { state updatedAt } }"
                      for number in numbers)
    query = f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}"
    found = scan_sessions.graphql_repository(repo, query) or {}
    refs = [{"repo": repo, "number": number, **(found.get(f"item_{number}") or {})} for number in numbers]
    return refs if all(item.get("state") == "CLOSED" for item in refs) else []


def pending(data: dict, decisions: dict[str, lead_decisions.Decision],
            answers: list[answer_evidence.Answer], now: datetime) -> dict[str, list[dict]]:
    """Open items as lists, settled questions with evidence, and transcript answers still missing from state."""
    questions, settled = [], []
    for key, item in data["questions"].items():
        if item["answer"] is not None:
            continue
        view = {"id": key, **item}
        asked_at = scan_sessions.parse_time(item["asked_at"])
        covered = [decisions[name] for name in item.get("covers") or [] if name in decisions
                   and asked_at and scan_sessions.parse_time(decisions[name].decided_at)
                   and scan_sessions.parse_time(decisions[name].decided_at) > asked_at]
        if item.get("withdrawn_at"):
            settled.append({**view, "state": "withdrawn", "since": item["withdrawn_at"],
                            "reason": item["withdrawal_reason"]})
        elif covered and len(covered) == len(item["covers"]):
            settled.append({**view, "state": "settled", "since": max(
                covered, key=lambda answer: scan_sessions.parse_time(answer.decided_at)).decided_at,
                            "evidence": [{"decision": answer.decision, "decided_at": answer.decided_at,
                                          "answer": answer.answer} for answer in covered]})
        elif refs := closed_references(item["row"], item["question"]):
            settled.append({**view, "state": "settled", "since": max(ref.get("updatedAt") or "" for ref in refs),
                            "evidence": refs})
        else:
            unrecorded = [{"at": answer.at.isoformat(), "source": answer.source, "text": answer.text}
                          for answer in answers if not answer.free_text
                          and asked_at and answer.at >= asked_at and item["question"] in answer.answered]
            questions.append({**view, "state": "unanswered", "since": item["asked_at"],
                              "age_minutes": int((now - asked_at).total_seconds() // 60) if asked_at else None,
                              "unrecorded_answers": unrecorded})
    return {
        "questions": questions, "settled": settled,
        "handoffs": [{"id": key, **item} for key, item in data["handoffs"].items() if item["delivered_at"] is None],
        "requests": [{"id": key, **item} for key, item in data["requests"].items() if item["closed_at"] is None],
        "left": [{"row": row, **item} for row, item in data["left"].items()],
    }


def change(data, args):
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
        if key not in data["questions"] and (refs := closed_references(args.row, question)):
            return {"state": "settled", "recorded": False, "row": args.row, "question": question, "evidence": refs}
        data["questions"].setdefault(key, {"row": args.row, "decision": decision,
                                           "final_message": message,
                                           "question": question, "asked_at": recorded_at(args.asked_at),
                                           "owner_pane": os.environ.get("ORCA_PANE_KEY"),
                                           "asked_in": str(worktree()), "covers": list(dict.fromkeys(args.covers)),
                                           "answer": None})
        return {"id": key, **data["questions"][key]}
    if args.action == "answered":
        question = data["questions"][args.id]
        if question.get("withdrawn_at"):
            raise ValueError(f"question was withdrawn at {question['withdrawn_at']}: {question['withdrawal_reason']}")
        answer = file_text(args.answer_file)
        earlier = {key: question.get(key) for key in ("answer", "answered_at", "answered_in")}
        if args.replace_reason_file and question["answer"] is not None and question["answer"] != answer:
            # A checked reading replaces the answer; the one it replaces stays beside it with the reason.
            question.setdefault("replaced", []).append(
                {**earlier, "reason": file_text(args.replace_reason_file), "replaced_at": stamp()})
            question["answer"] = None
        if question["answer"] is not None and question["answer"] != answer:
            raise ValueError("answer already recorded: " + json.dumps(earlier, ensure_ascii=False))
        if question["answer"] is None:
            question.update(answer=answer, answered_at=recorded_at(args.answered_at),
                            answered_in=os.environ.get("ORCA_TERMINAL_HANDLE") or str(worktree()))
        return {"id": args.id, **question}
    if args.action == "withdraw":
        question = data["questions"][args.id]
        reason = file_text(args.reason_file)
        if question["answer"] is not None:
            raise ValueError("question already has an answer")
        if question.get("withdrawn_at") and question["withdrawal_reason"] != reason:
            raise ValueError("question already withdrawn; inspect its reason before changing it")
        if not question.get("withdrawn_at"):
            question.update(withdrawn_at=stamp(), withdrawal_reason=reason)
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


def summary(items: list[dict], now: datetime, kind: str = "") -> str:
    lines = []
    for item in items:
        row = item.get("row") or item.get("owner") or item.get("from") or ""
        since = item.get("since") or next((item[key] for key in
                    ("asked_at", "promised_at", "opened_at", "left_at") if item.get(key)), "")
        at = scan_sessions.parse_time(since)
        age = item.get("age_minutes", int((now - at).total_seconds() // 60) if at else None)
        name = item.get("name") or row.rsplit("::", 1)[-1].rstrip("/").rsplit("/", 1)[-1] or "-"
        state = item.get("state") or {"handoffs": "undelivered", "requests": "open", "left": "left"}.get(kind, kind)
        ask_id = (item.get("ask") or {}).get("id")
        question = item.get("question") or item.get("original") or item.get("task") or item.get("final_message")
        line = (f"{name} | {state} | since={since or '-'} | age_minutes={age} "
                f"| {f'ask={ask_id}' if ask_id else f'id={item.get('id') or '-'}'} | {excerpt(question)}")
        if item.get("evidence"):
            line += " | 済み: " + "; ".join(
                f"{proof['decision']}@{proof['decided_at']}" if "decision" in proof
                else f"{proof['repo']}#{proof['number']} {proof['state']}@{proof.get('updatedAt') or '-'}"
                for proof in item["evidence"])
        if item.get("pr"):
            line += f" | PR {item['pr'].get('state') or '-'}@{item['pr'].get('updatedAt') or '-'}"
        if claimed := (item.get("ask") or {}).get("claimed"):
            line += f" | 伺い中 {claimed['by']}（{claimed['at']}〜）。重ねて伺わない"
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
    commands.add_parser("show", help="read all saved decisions, questions, handoffs, and requests")
    pending_command = commands.add_parser(
        "pending", help="list open items with their ids, settled questions with evidence, and unrecorded answers")
    pending_command.add_argument("--summary", action="store_true", help="print one line per item with its age and question")
    answer_evidence.add_arguments(pending_command)
    get = commands.add_parser("get", help="read one saved question, handoff, request, or left row")
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
                           "ask without an answer that proves it, marked or not), or stopped_after_answer (answered "
                           "after its last message), with the lane's ask, the answer, candidates that name only the "
                           "lane or its PR numbers, and age_minutes")
    stale.add_argument("--from", dest="from_file", metavar="SCAN_JSON", required=True,
                       help="the rows scan_sessions.py printed as JSON")
    stale.add_argument("--verdicts", default=scan_sessions.VERDICTS_FILE,
                       help="the careful reads' verdict store whose verdicts the rows take (default: %(default)s)")
    stale.add_argument("--summary", action="store_true", help="print one line per row with its age, ask id, and question")
    stale.add_argument("--handed-off-file", type=Path, default=lead_decisions.HANDED_OFF_LOG,
                       help="the handed-off keys whose unanswered asks remain visible (default: %(default)s)")
    answer_evidence.add_arguments(stale)
    stale.epilog = "Without --asked-in, read the appointed lead's worktree from shared state."
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
    asked.add_argument("--covers", action="append", default=[], metavar="KEY",
                       help="a lead decision key this question covers; repeat per key")
    answered = commands.add_parser("answered", help="record the user's answer before acting on it")
    answered.add_argument("--id", required=True)
    answered.add_argument("--answer-file", required=True)
    answered.add_argument("--answered-at", help="original ISO 8601 time when importing an answer")
    answered.add_argument("--replace-reason-file",
                          help="replace a different recorded answer with this checked one, keeping the earlier "
                               "answer, its time and tab under `replaced` with this file's reason")
    withdraw = commands.add_parser("withdraw", help="close an unanswered question with a reason, without a user's answer")
    withdraw.add_argument("--id", required=True)
    withdraw.add_argument("--reason-file", required=True)
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
            decided = lead_decisions.latest(lead_decisions.records(args.decisions_file.expanduser()))
            handed = list({item.decision: item for item in lead_decisions.handed_off(args.handed_off_file.expanduser())
                           if item.decision not in decided}.values())
            result = stale_asks(rows, entries, open_ids, answer_evidence.from_arguments(args, answers_log),
                                datetime.now(timezone.utc), state=data, handed=handed, prs=blocked_prs(rows),
                                claimed={ask.report["id"]: claim for ask in asks
                                         if (claim := inbox_read.ask_claim(ask, claims))})
        except READ_ERRORS as error:
            print(f"hourly state: {error}", file=sys.stderr)
            return 1
        print(summary(result, datetime.now(timezone.utc)) if args.summary
              else json.dumps(result, ensure_ascii=False, indent=2))
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
                args.asked_in.extend(Path(item["asked_in"]) for item in data["questions"].values()
                                     if item.get("asked_in"))
                if lead := appointed_lead(data):
                    args.asked_in.append(lead)
                result = pending(data, lead_decisions.latest(lead_decisions.records(args.decisions_file.expanduser())),
                                 answer_evidence.from_arguments(args, inbox_read.answers_path()), datetime.now(timezone.utc))
            elif args.action == "get":
                result = next(({"id": args.id, **data[kind][args.id]} for kind in
                               ("questions", "handoffs", "requests", "left") if args.id in data[kind]), None)
                if result is None:
                    raise ValueError(f"unknown id: {args.id}")
            else:
                result = data if args.action == "show" else change(data, args)
            if writes:
                save(path, data)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"hourly state: {error}", file=sys.stderr)
        return 1
    if args.action == "pending" and args.summary:
        print("\n".join(line for kind, items in result.items()
                        if (line := summary(items, datetime.now(timezone.utc), kind))))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.action == "left-now" and not left_matches else 0


if __name__ == "__main__":
    sys.exit(main())
