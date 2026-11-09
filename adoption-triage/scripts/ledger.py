#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Append and read adoption triage events. Use --help for commands and paths."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import heapq
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import uuid
from typing import Literal, TypeGuard, TypedDict


class Event(TypedDict, total=False):
    id: str
    ts: str
    host: str
    type: str
    proposal_id: str
    after_id: str
    source: dict[str, str]
    fact: str
    proposal: str
    reason: str
    recommendation: str
    priority: str
    evidence: list[str]
    target: str | None
    merges: list[str]
    retires: list[str]
    confirms: list[str]
    decision: str
    actor: str
    result: str
    undo: str
    decision_id: str
    outcome: str
    closed: list[Event]
    confirmed: list[Event]


class Proposal(Event):
    status: str
    adopted_at: str | None
    rejected_at: str | None
    held_at: str | None
    revoked_at: str | None
    implemented_at: str | None
    reverted_at: str | None
    supporting_events: list[Event]
    decisions: list[Event]
    executions: list[Event]
    latest_event_id: str
    current_execution: Event | None
    merged_into: list[str]
    retired_by: list[str]
    confirmed_by: list[str]


def is_event(value: object) -> TypeGuard[Event]:
    """Validate JSONL fields once, before events enter ordering and projection."""
    if not isinstance(value, dict):
        return False
    required = {"id", "ts", "host", "type", "proposal_id"}
    strings = required | {"after_id", "fact", "proposal", "reason", "recommendation", "priority",
                          "decision", "actor", "result", "undo", "decision_id", "outcome"}
    if not required <= value.keys() or any(not isinstance(value[key], str) for key in strings & value.keys()):
        return False
    if any(not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key])
           for key in {"evidence", "merges", "retires", "confirms"} & value.keys()):
        return False
    source = value.get("source")
    if "source" in value and (not isinstance(source, dict) or set(source) != {"session", "worktree"}
                               or any(not isinstance(item, str) for item in source.values())):
        return False
    return value.get("target") is None or isinstance(value["target"], str)


DEFAULT_EVENTS = Path("~/.local/share/adoption-triage/events.jsonl").expanduser()
DEFAULT_IMPORTS = Path("~/.local/share/adoption-triage/imports").expanduser()
DEFAULT_HOSTS = Path("~/.config/adoption-triage/hosts.json").expanduser()
DECISIONS = {"adopt", "reject", "hold", "revoke"}
# handed_off: the worker left an adopted change to a lane, which records applied when it ships.
OUTCOMES = {"applied", "reverted", "failed", "handed_off"}
# The retrospective worker: its handed_off only says the proposal waits for a lane (references/ledger.md).
RETRO = "triage-retro"
# spawn-lane's handed_off result: 「担当の handle <handle>・指示書 <brief>」.
BRIEF = re.compile(r"指示書 (.+)$")
# merges: this proposal combines the listed ones; retires: it withdraws the listed stale ones.
# merges and retires close their targets when adopted; confirms records its adopted targets as applied.
RELATIONS: dict[Literal["merges", "retires", "confirms"],
                Literal["merged_into", "retired_by", "confirmed_by"]] = {
    "merges": "merged_into", "retires": "retired_by", "confirms": "confirmed_by"}
DECISION_TIMES: dict[str, Literal["adopted_at", "rejected_at", "held_at", "revoked_at"]] = {
    "adopt": "adopted_at", "reject": "rejected_at", "hold": "held_at", "revoke": "revoked_at"}


def required_text(value: object, name: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text (up to {limit} characters)")
    return value.strip()


def optional_text(value: object, name: str, limit: int = 4000) -> str | None:
    if value is None:
        return None
    return required_text(value, name, limit)


def timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("ts requires a timezone offset")
    return parsed


def read_rows(path: Path) -> list[Event]:
    if not path.exists():
        return []
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        raise ValueError(f"incomplete JSONL: {path}")
    rows = []
    for number, line in enumerate(data.splitlines(), 1):
        row = json.loads(line)
        if not is_event(row):
            raise ValueError(f"invalid event at {path}:{number}")
        timestamp(row["ts"])
        rows.append(row)
    return rows


def all_rows(events: Path, imports: Path) -> list[Event]:
    paths = [events]
    if imports.exists():
        paths.extend(sorted(imports.glob("*.jsonl")))
    seen = {}
    edges = set()
    for path in paths:
        previous_id = None
        for row in read_rows(path):
            old = seen.setdefault(row["id"], row)
            if old != row:
                raise ValueError(f"conflicting event id: {row['id']}")
            if previous_id is not None:
                edges.add((previous_id, row["id"]))
            previous_id = row["id"]
    for row in seen.values():
        if row.get("after_id"):
            edges.add((row["after_id"], row["id"]))
    followers = {event_id: [] for event_id in seen}
    incoming = {event_id: 0 for event_id in seen}
    for before, after in edges:
        if before not in seen:
            raise ValueError(f"missing predecessor event: {before}")
        if before != after:
            followers[before].append(after)
            incoming[after] += 1
    ready = [(timestamp(row["ts"]), row["id"]) for row in seen.values()
             if incoming[row["id"]] == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _, event_id = heapq.heappop(ready)
        ordered.append(seen[event_id])
        for following in followers[event_id]:
            incoming[following] -= 1
            if incoming[following] == 0:
                row = seen[following]
                heapq.heappush(ready, (timestamp(row["ts"]), following))
    if len(ordered) != len(seen):
        raise ValueError("event order contains a cycle")
    return ordered


def project(rows: list[Event]) -> list[Proposal]:
    proposals: dict[str, Proposal] = {}
    for row in rows:
        proposal_id = row["proposal_id"]
        if row["type"] == "proposal":
            if proposal_id in proposals:
                raise ValueError(f"duplicate proposal: {proposal_id}")
            proposals[proposal_id] = {**row, "status": "pending", "adopted_at": None,
                                      "rejected_at": None, "held_at": None, "revoked_at": None,
                                      "implemented_at": None, "reverted_at": None,
                                      "evidence": list(row.get("evidence", [])),
                                      "supporting_events": [], "decisions": [], "executions": [],
                                      "latest_event_id": row["id"], "current_execution": None,
                                      "merges": list(row.get("merges", [])),
                                      "retires": list(row.get("retires", [])),
                                      "confirms": list(row.get("confirms", [])),
                                      "merged_into": [], "retired_by": [], "confirmed_by": []}
    for item in list(proposals.values()):
        for relation, reverse in RELATIONS.items():
            for target in item[relation]:
                if target not in proposals:
                    raise ValueError(f"{relation} without proposal: {target}")
                proposals[target][reverse].append(item["proposal_id"])
    for row in rows:
        proposal_id = row["proposal_id"]
        if row["type"] == "proposal":
            continue
        if row["type"] == "evidence":
            if proposal_id not in proposals:
                raise ValueError(f"evidence without proposal: {proposal_id}")
            item = proposals[proposal_id]
            for value in row["evidence"]:
                if value not in item["evidence"]:
                    item["evidence"].append(value)
            item["supporting_events"].append(row)
            item["latest_event_id"] = row["id"]
        elif row["type"] == "decision":
            if proposal_id not in proposals:
                raise ValueError(f"decision without proposal: {proposal_id}")
            item = proposals[proposal_id]
            decision = row["decision"]
            if decision not in DECISIONS:
                raise ValueError(f"invalid decision: {decision}")
            if decision == "revoke" and item["status"] != "adopted":
                raise ValueError(f"only adopted proposals can be revoked: {proposal_id}")
            if item["status"] == "adopted" and decision in {"reject", "hold"}:
                raise ValueError(f"revoke adoption before {decision}: {proposal_id}")
            if decision == "adopt":
                # A proposal that only retires or confirms others has done its work in the adoption's own write.
                item["implemented_at"] = row["ts"] if nothing_to_apply(item) else None
                item["reverted_at"] = None
                item["revoked_at"] = None
            item["status"] = {"adopt": "adopted", "reject": "rejected", "hold": "held",
                              "revoke": "revoked"}[decision]
            item[DECISION_TIMES[decision]] = row["ts"]
            item["decisions"].append(row)
            item["latest_event_id"] = row["id"]
            item["current_execution"] = None
        elif row["type"] == "execution":
            if proposal_id not in proposals:
                raise ValueError(f"execution without proposal: {proposal_id}")
            item = proposals[proposal_id]
            outcome = row["outcome"]
            if outcome not in OUTCOMES:
                raise ValueError(f"invalid execution outcome: {outcome}")
            decision = next((decision for decision in item["decisions"]
                             if decision["id"] == row.get("decision_id")), None)
            if decision is None or decision["decision"] not in {"adopt", "revoke"}:
                raise ValueError(f"execution without an adoption or revocation: {proposal_id}")
            if outcome in {"applied", "handed_off"} and decision["decision"] != "adopt":
                raise ValueError(f"application without adoption: {proposal_id}")
            if outcome == "reverted" and decision["decision"] != "revoke":
                raise ValueError(f"reversal without revocation: {proposal_id}")
            item["executions"].append(row)
            item["latest_event_id"] = row["id"]
            if item["decisions"][-1]["id"] == decision["id"]:
                if outcome == "applied":
                    item["implemented_at"] = row["ts"]
                if outcome == "reverted":
                    item["reverted_at"] = row["ts"]
                item["current_execution"] = row
        else:
            raise ValueError(f"invalid event type: {row['type']}")
    return sorted(proposals.values(), key=lambda item: (
        item["status"] not in ("pending", "held"),
        {"high": 0, "normal": 1, "low": 2}[item["priority"]],
        -timestamp(item["ts"]).timestamp()))


def nothing_to_apply(item: Proposal) -> bool:
    """Whether adopting the proposal is its whole work: it retires or confirms others and merges none."""
    return bool(item["retires"] or item["confirms"]) and not item["merges"]


def active_owner(item: Proposal, known: dict[str, Proposal], besides: str | None = None) -> str | None:
    """The undecided or adopted proposal, other than `besides`, that already merges, retires, or confirms the item."""
    return next((owner for owner in (*item["merged_into"], *item["retired_by"], *item["confirmed_by"])
                 if owner != besides and known[owner]["status"] in {"pending", "held", "adopted"}), None)


def consolidation_blocker(item: Proposal, known: dict[str, Proposal]) -> str | None:
    """Why a proposal cannot be merged or retired now; None when it is a consolidation candidate."""
    if item["status"] not in {"pending", "held", "adopted"} or item["implemented_at"]:
        return "is not open"
    owner = active_owner(item, known)
    return f"is already in open proposal {owner}" if owner else None


def closing_decisions(owner: Proposal, known: dict[str, Proposal], actor: str) -> list[Event]:
    """Decisions that close the open targets of an adopted consolidation; re-adopting a target reopens it.

    A direct target adopted and not yet applied is revoked. The targets a closed consolidation hands on
    are closed only while undecided: one already adopted stays adopted for its lane to finish."""
    rows, seen = [], {owner["proposal_id"]}
    for relation, label in (("merges", "統合先"), ("retires", "取り下げの提案")):
        queue = [(target_id, True) for target_id in owner[relation]]
        while queue:
            target_id, direct = queue.pop(0)
            if target_id in seen:
                continue
            seen.add(target_id)
            target = known[target_id]
            if target["status"] in {"pending", "held"}:
                decision = "reject"
            elif direct and target["status"] == "adopted" and not target["implemented_at"]:
                decision = "revoke"
            else:
                continue
            # A consolidation closed here hands its own targets on, or they would return as candidates.
            queue.extend((handed, False) for handed in (*target["merges"], *target["retires"]))
            rows.append({**envelope("decision", target_id), "decision": decision, "actor": actor,
                         "reason": f"{label}「{owner['proposal']}」を採用",
                         "after_id": target["latest_event_id"]})
    return rows


def confirming_executions(owner: Proposal, known: dict[str, Proposal], actor: str) -> list[Event]:
    """Applied records, under each target's adoption, for the adopted and unapplied proposals the owner confirms."""
    return [{**envelope("execution", target["proposal_id"]), "decision_id": target["decisions"][-1]["id"],
             "outcome": "applied", "actor": actor, "result": f"実施の確認「{owner['proposal']}」を採用",
             "after_id": target["latest_event_id"]}
            for target in (known[target_id] for target_id in owner["confirms"])
            if target["status"] == "adopted" and not target["implemented_at"]]


def owner_view(item: Proposal) -> dict:
    """Who holds the proposal under its last adoption, the brief they were given, and whether the work is done.

    The holder is the last applied, or the last handed_off by a lane, under that adoption."""
    adoption = next((decision["id"] for decision in reversed(item["decisions"]) if decision["decision"] == "adopt"),
                    None)
    runs = [run for run in item["executions"] if adoption and run["decision_id"] == adoption]
    lanes = [run for run in runs if run["outcome"] == "handed_off" and run["actor"] != RETRO]
    holder = next((run for run in reversed(runs) if run["outcome"] == "applied" or run in lanes), None)
    brief = found.group(1) if lanes and (found := BRIEF.search(lanes[-1]["result"])) else None
    current = item["current_execution"] or {}
    state = (f"戻した {item['reverted_at'][:16]}" if item["reverted_at"]
             else f"済 {item['implemented_at'][:16]}" if item["implemented_at"]
             else f"失敗 {current['ts'][:16]}" if current.get("outcome") == "failed" else "未")
    return {"proposal_id": item["proposal_id"], "status": item["status"], "owner": holder and holder["actor"],
            "brief": brief, "state": state}


def named_proposals(items: list[Proposal], wanted: list[str]) -> list[Proposal]:
    """The proposal each ID names, in order: a full ID or a start of 8 or more characters naming one."""
    found, errors = [], []
    for value in wanted:
        hits = [item for item in items if len(value) >= 8 and item["proposal_id"].startswith(value.lower())]
        if len(hits) == 1:
            found.append(hits[0])
        else:
            errors.append(f"{value} names {len(hits)} proposals" if hits else f"no proposal has id {value}")
    if errors:
        raise ValueError("; ".join(errors))
    return found


def envelope(kind: str, proposal_id: str) -> Event:
    return {"id": str(uuid.uuid4()), "ts": dt.datetime.now().astimezone().isoformat(timespec="microseconds"),
            "host": socket.gethostname(), "type": kind, "proposal_id": proposal_id}


def validate_proposal(body: object) -> Event:
    if not isinstance(body, dict):
        raise ValueError("proposal must be a JSON object")
    allowed = {"proposal_id", "source", "fact", "proposal", "reason", "recommendation",
               "priority", "evidence", "target", *RELATIONS}
    if set(body) - allowed:
        raise ValueError("unknown proposal field")
    source = body.get("source")
    if not isinstance(source, dict) or set(source) != {"session", "worktree"}:
        raise ValueError("source requires session and worktree")
    source = {key: required_text(source[key], key, 240) for key in ("session", "worktree")}
    proposal_id = body.get("proposal_id", str(uuid.uuid4()))
    if not isinstance(proposal_id, str):
        raise ValueError("proposal_id must be a UUID string")
    uuid.UUID(proposal_id)
    for key in ("fact", "proposal", "reason"):
        required_text(body.get(key), key)
    if body.get("recommendation") not in {"adopt", "reject"}:
        raise ValueError("recommendation must be adopt or reject")
    if body.get("priority") not in {"high", "normal", "low"}:
        raise ValueError("priority must be high, normal, or low")
    evidence = body.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 100 or any(
            not isinstance(value, str) or len(value) > 1000 for value in evidence):
        raise ValueError("evidence must be a list of short strings")
    target = optional_text(body.get("target"), "target", 500)
    row: Event = {**body, **envelope("proposal", proposal_id), "source": source,
           "evidence": evidence, "target": target}
    for relation in RELATIONS:
        targets = body.get(relation)
        if targets is None:
            continue
        if not isinstance(targets, list) or not 1 <= len(targets) <= 100 or len(set(targets)) != len(targets):
            raise ValueError(f"{relation} must be a nonempty list of distinct proposal UUIDs")
        for value in targets:
            if not isinstance(value, str) or value == proposal_id:
                raise ValueError(f"{relation} must list other proposals")
            uuid.UUID(value)
    named = [value for relation in RELATIONS for value in body.get(relation, [])]
    if len(set(named)) != len(named):
        raise ValueError("a proposal names each other proposal in one relation only")
    return row


def validate_decision(body: object) -> Event:
    if not isinstance(body, dict) or set(body) - {"proposal_id", "decision", "actor", "reason", "result", "undo"}:
        raise ValueError("invalid decision object")
    proposal_id = required_text(body.get("proposal_id"), "proposal_id", 80)
    uuid.UUID(proposal_id)
    decision = body.get("decision")
    if decision not in DECISIONS:
        raise ValueError("decision must be adopt, reject, hold, or revoke")
    row: Event = {**envelope("decision", proposal_id), "decision": decision,
           "actor": required_text(body.get("actor"), "actor", 120)}
    for key in ("reason", "result", "undo"):
        value = optional_text(body.get(key), key)
        if value is not None:
            row[key] = value
    return row


def validate_execution(body: object) -> Event:
    if not isinstance(body, dict) or set(body) - {"proposal_id", "decision_id", "outcome", "actor", "result", "undo"}:
        raise ValueError("invalid execution object")
    proposal_id = required_text(body.get("proposal_id"), "proposal_id", 80)
    uuid.UUID(proposal_id)
    decision_id = required_text(body.get("decision_id"), "decision_id", 80)
    uuid.UUID(decision_id)
    outcome = body.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError("outcome must be applied, reverted, failed, or handed_off")
    row: Event = {**envelope("execution", proposal_id), "decision_id": decision_id, "outcome": outcome,
           "actor": required_text(body.get("actor"), "actor", 120),
           "result": required_text(body.get("result"), "result")}
    undo = optional_text(body.get("undo"), "undo")
    if undo is not None:
        row["undo"] = undo
    return row


def validate_evidence(body: object) -> Event:
    if not isinstance(body, dict) or set(body) != {"proposal_id", "source", "evidence"}:
        raise ValueError("evidence needs proposal_id, source, and evidence")
    proposal_id = required_text(body["proposal_id"], "proposal_id", 80)
    uuid.UUID(proposal_id)
    source = body["source"]
    if not isinstance(source, dict) or set(source) != {"session", "worktree"}:
        raise ValueError("source requires session and worktree")
    source = {key: required_text(source[key], key, 240) for key in ("session", "worktree")}
    evidence = body["evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 100 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 1000 for value in evidence):
        raise ValueError("evidence must be a nonempty list of short strings")
    return {**envelope("evidence", proposal_id), "source": source, "evidence": evidence}


def append(events: Path, imports: Path, row: Event) -> Event:
    events.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(events, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        items = project(all_rows(events, imports))
        known = {item["proposal_id"]: item for item in items}
        proposal_id = row["proposal_id"]
        if row["type"] == "proposal":
            if proposal_id in known:
                raise ValueError(f"proposal already exists: {proposal_id}")
            for relation in RELATIONS:
                for target_id in row.get(relation, []):
                    if target_id not in known:
                        raise ValueError(f"{relation} target not found: {target_id}")
                    blocker = consolidation_blocker(known[target_id], known)
                    if blocker:
                        raise ValueError(f"{relation} target {blocker}: {target_id}")
                    adopted = known[target_id]["status"] == "adopted"
                    if relation == "merges" and adopted:
                        raise ValueError(f"merges target is adopted: {target_id}; close it with retires, "
                                         "or record its work as done with confirms")
                    if relation == "confirms" and not adopted:
                        raise ValueError(f"confirms target is not adopted: {target_id}")
        elif row["type"] == "decision":
            if proposal_id not in known:
                raise ValueError(f"proposal not found: {proposal_id}")
            previous = known[proposal_id]["status"]
            decision = row["decision"]
            if decision == "revoke" and previous != "adopted":
                raise ValueError("only adopted proposals can be revoked")
            if previous == "adopted" and decision in {"adopt", "reject", "hold"}:
                raise ValueError("revoke adoption before another decision")
            if decision in {"adopt", "hold"}:
                # A rejected consolidation may come back only while no other one has taken its targets.
                for relation in RELATIONS:
                    for target_id in known[proposal_id][relation]:
                        owner = active_owner(known[target_id], known, besides=proposal_id)
                        if owner:
                            raise ValueError(f"{relation} target is already in open proposal {owner}: {target_id}")
        elif row["type"] == "execution":
            item = known.get(proposal_id)
            if item is None:
                raise ValueError(f"proposal not found: {proposal_id}")
            decision = next((decision for decision in item["decisions"]
                             if decision["id"] == row["decision_id"]), None)
            if decision is None or decision["decision"] not in {"adopt", "revoke"}:
                raise ValueError("execution needs an adoption or revocation")
            if row["outcome"] in {"applied", "handed_off"} and decision["decision"] != "adopt":
                raise ValueError("adoption was not decided")
            if row["outcome"] == "reverted" and decision["decision"] != "revoke":
                raise ValueError("reversal was not decided")
        else:
            if proposal_id not in known:
                raise ValueError(f"proposal not found: {proposal_id}")
        if row["type"] != "proposal":
            row["after_id"] = known[proposal_id]["latest_event_id"]
        adopted = row["type"] == "decision" and row["decision"] == "adopt"
        closed = closing_decisions(known[proposal_id], known, row["actor"]) if adopted else []
        confirmed = confirming_executions(known[proposal_id], known, row["actor"]) if adopted else []
        # One write keeps the adoption and the targets it closes or confirms together.
        data = b"".join((json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                        for event in (row, *closed, *confirmed))
        if os.write(fd, data) != len(data):
            raise OSError("partial event write")
        os.fsync(fd)
    finally:
        os.close(fd)
    return {**row, "closed": closed, "confirmed": confirmed} if adopted else row


def replace_import(imports: Path, name: str, temporary_path: Path, allow_reorder: bool = False) -> list[Event]:
    rows = read_rows(temporary_path)
    path = imports / f"{name}.jsonl"
    lock_fd = os.open(imports / f".{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        previous = read_rows(path)
        if allow_reorder:
            current = {row["id"]: row for row in rows}
            if len(current) != len(rows) or any(current.get(row["id"]) != row for row in previous):
                raise ValueError(f"remote ledger is not append-only: {name}")
        elif rows[:len(previous)] != previous:
            raise ValueError(f"remote ledger is not append-only: {name}")
        temporary_path.replace(path)
    finally:
        os.close(lock_fd)
    return rows


def receive(config: Path, imports: Path, name: str) -> int:
    settings = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or not isinstance(settings.get("hosts"), list):
        raise ValueError("hosts must be a list")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,39}", name):
        raise ValueError("invalid writer name")
    if settings.get("decision_writer") is True or settings.get("trusted_writer") != name:
        raise ValueError(f"decision writer is not configured: {name}")
    imports.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=imports, prefix=f".{name}-", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(sys.stdin.buffer.read())
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        return len(replace_import(imports, name, temporary_path, allow_reorder=True))
    finally:
        temporary_path.unlink(missing_ok=True)


def collect(config: Path, imports: Path, events: Path) -> int:
    settings = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError("host configuration must be an object")
    hosts = settings["hosts"]
    if not isinstance(hosts, list):
        raise ValueError("hosts must be a list")
    writer_name = settings.get("name") if settings.get("decision_writer") is True else None
    if settings.get("decision_writer") is True and writer_name is None:
        raise ValueError("local decision writer needs a stable name")
    if writer_name is not None and (not isinstance(writer_name, str) or
                                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,39}", writer_name)):
        raise ValueError("invalid local decision writer name")
    imports.mkdir(parents=True, exist_ok=True, mode=0o700)
    errors = []
    targets = []
    for host in hosts:
        if not isinstance(host, dict) or set(host) != {"name", "ssh_target", "remote_path"}:
            raise ValueError("each host needs name, ssh_target, remote_path")
        name, target, remote = (host[key] for key in ("name", "ssh_target", "remote_path"))
        if (not isinstance(name, str) or not isinstance(target, str) or not isinstance(remote, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,39}", name)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,119}", target)
                or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,300}", remote)):
            raise ValueError(f"invalid host configuration: {name}")
        targets.append((name, target))
        path = imports / f"{name}.jsonl"
        with tempfile.NamedTemporaryFile(dir=imports, prefix=f".{name}-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            result = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                     f"{target}:{remote}", str(temporary_path)], capture_output=True, timeout=20)
            if result.returncode:
                check_script = ("import sys\ntry:\n    open(sys.argv[1], 'rb').close()\n"
                                "except FileNotFoundError:\n    sys.exit(42)\n")
                check_command = f"python3 -c {shlex.quote(check_script)} {shlex.quote(remote)}"
                missing = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                          target, check_command], capture_output=True, timeout=10)
                if missing.returncode == 42 and (not path.exists() or not path.read_bytes()):
                    temporary_path.write_bytes(b"")
                else:
                    raise OSError(f"scp exit {result.returncode}: "
                                  f"{result.stderr.decode(errors='replace').strip()}; "
                                  f"remote check exit {missing.returncode}: "
                                  f"{missing.stderr.decode(errors='replace').strip()}")
            rows = read_rows(temporary_path)
            if any(row["type"] == "decision" for row in rows):
                raise ValueError(f"remote decision is not allowed: {name}")
            replace_import(imports, name, temporary_path)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{name}: {exc}")
        finally:
            temporary_path.unlink(missing_ok=True)
    if writer_name:
        try:
            data = b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                            for row in all_rows(events, imports))
        except (OSError, ValueError) as exc:
            errors.append(f"distribution: {exc}")
        else:
            for name, target in targets:
                try:
                    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target,
                                             f"~/.agents/skills/adoption-triage/scripts/ledger.py receive --name {writer_name}"],
                                            input=data, capture_output=True, timeout=20)
                    if result.returncode:
                        raise OSError(f"ssh exit {result.returncode}: "
                                      f"{result.stderr.decode(errors='replace').strip()}")
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(f"{name} distribution: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return len(hosts)


def collect_config(args: argparse.Namespace) -> Path:
    """The hosts collect may use: the default hosts.json lists the real Macs, so it pairs only with the real ledger."""
    if args.config is not None:
        return args.config
    if args.events.resolve() != DEFAULT_EVENTS.resolve() or args.imports.resolve() != DEFAULT_IMPORTS.resolve():
        raise ValueError("collect on a ledger copy needs its own --config; "
                         f"{DEFAULT_HOSTS} would send the copy to the real Macs")
    return DEFAULT_HOSTS


def require_decision_writer(config: Path) -> None:
    try:
        configured = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"decision writer is not configured: {config}") from exc
    if not isinstance(configured, dict) or configured.get("decision_writer") is not True:
        raise ValueError(f"decision writer is not enabled: {config}")
    if not isinstance(configured.get("name"), str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9.-]{0,39}", configured["name"]):
        raise ValueError(f"decision writer needs a stable name: {config}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--imports", type=Path, default=DEFAULT_IMPORTS)
    parser.add_argument("--decision-config", type=Path, default=DEFAULT_HOSTS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("propose", help="append one proposal from stdin JSON")
    commands.add_parser("decide", help="append one decision from stdin JSON")
    commands.add_parser("record", help="append one execution result from stdin JSON")
    commands.add_parser("support", help="append more source evidence to a proposal from stdin JSON")
    listing = commands.add_parser("list", help="read current proposals",
                                   description="Without --json, print one proposal per line: id, status, priority, proposal.")
    listing.add_argument("--json", action="store_true", help='print {"items": [...]} instead of one proposal per line')
    listing.add_argument("--candidates", action="store_true",
                         help="only pending, held, and unimplemented adopted proposals not already in an open consolidation")
    owning = commands.add_parser("owner", help="print, one line per proposal, its status, the lane holding it "
                                               "(担当), the brief it was given (指示書), and whether it is done (実施)")
    owning.add_argument("ids", nargs="+", help="proposal IDs, or their first 8 or more characters")
    owning.add_argument("--json", action="store_true",
                        help="print {items: [{proposal_id, status, owner, brief, state}]} instead")
    pulling = commands.add_parser("collect", help="copy remote Mac ledgers")
    pulling.add_argument("--config", type=Path,
                         help=f"hosts to read and send to (default {DEFAULT_HOSTS}, "
                              "only with the default --events and --imports)")
    incoming = commands.add_parser("receive", help="accept the configured decision writer's append-only ledger")
    incoming.add_argument("--name", required=True)
    incoming.add_argument("--config", type=Path, default=DEFAULT_HOSTS)
    args = parser.parse_args()
    try:
        if args.command == "propose":
            print(json.dumps(append(args.events, args.imports, validate_proposal(json.load(sys.stdin))), ensure_ascii=False))
        elif args.command == "decide":
            require_decision_writer(args.decision_config)
            print(json.dumps(append(args.events, args.imports, validate_decision(json.load(sys.stdin))), ensure_ascii=False))
        elif args.command == "record":
            print(json.dumps(append(args.events, args.imports, validate_execution(json.load(sys.stdin))), ensure_ascii=False))
        elif args.command == "support":
            print(json.dumps(append(args.events, args.imports, validate_evidence(json.load(sys.stdin))), ensure_ascii=False))
        elif args.command == "list":
            items = project(all_rows(args.events, args.imports))
            if args.candidates:
                known = {item["proposal_id"]: item for item in items}
                items = [item for item in items if consolidation_blocker(item, known) is None]
            if args.json:
                print(json.dumps({"items": items}, ensure_ascii=False))
            else:
                for item in items:
                    print(f"{item['proposal_id']}\t{item['status']}\t{item['priority']}\t{item['proposal']}")
        elif args.command == "owner":
            views = [owner_view(item) for item in named_proposals(project(all_rows(args.events, args.imports)),
                                                                   args.ids)]
            if args.json:
                print(json.dumps({"items": views}, ensure_ascii=False))
            else:
                for view in views:
                    print(f"{view['proposal_id']}\t{view['status']}\t担当 {view['owner'] or 'なし'}\t"
                          f"指示書 {view['brief'] or 'なし'}\t実施 {view['state']}")
        elif args.command == "collect":
            print(json.dumps({"collected": collect(collect_config(args), args.imports, args.events)}))
        else:
            print(json.dumps({"received": receive(args.config, args.imports, args.name)}))
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"adoption-triage: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
