#!/usr/bin/env python3
"""Append and read adoption triage events. Use --help for commands and paths."""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import uuid


DEFAULT_EVENTS = Path("~/.local/share/adoption-triage/events.jsonl").expanduser()
DEFAULT_IMPORTS = Path("~/.local/share/adoption-triage/imports").expanduser()
DEFAULT_HOSTS = Path("~/.config/adoption-triage/hosts.json").expanduser()
DECISIONS = {"adopt", "reject", "hold", "revoke"}


def required_text(value, name, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text (up to {limit} characters)")
    return value.strip()


def optional_text(value, name, limit=4000):
    if value is None:
        return None
    return required_text(value, name, limit)


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("ts requires a timezone offset")
    return parsed


def read_rows(path):
    if not path.exists():
        return []
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        raise ValueError(f"incomplete JSONL: {path}")
    rows = []
    for number, line in enumerate(data.splitlines(), 1):
        row = json.loads(line)
        if not isinstance(row, dict) or not all(key in row for key in ("id", "ts", "host", "type", "proposal_id")):
            raise ValueError(f"invalid event at {path}:{number}")
        timestamp(row["ts"])
        rows.append(row)
    return rows


def all_rows(events, imports):
    paths = [events]
    if imports.exists():
        paths.extend(sorted(imports.glob("*.jsonl")))
    seen = {}
    for path in paths:
        for row in read_rows(path):
            old = seen.setdefault(row["id"], row)
            if old != row:
                raise ValueError(f"conflicting event id: {row['id']}")
    return sorted(seen.values(), key=lambda row: (timestamp(row["ts"]), row["id"]))


def project(rows):
    proposals = {}
    for row in rows:
        proposal_id = row["proposal_id"]
        if row["type"] == "proposal":
            if proposal_id in proposals:
                raise ValueError(f"duplicate proposal: {proposal_id}")
            proposals[proposal_id] = {**row, "status": "pending", "adopted_at": None,
                                      "rejected_at": None, "held_at": None, "revoked_at": None,
                                      "implemented_at": None, "reverted_at": None,
                                      "evidence": list(row.get("evidence", [])),
                                      "supporting_events": [], "decisions": [], "executions": []}
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
                item["implemented_at"] = None
                item["reverted_at"] = None
                item["revoked_at"] = None
            item["status"] = {"adopt": "adopted", "reject": "rejected", "hold": "held",
                              "revoke": "revoked"}[decision]
            item[{"adopt": "adopted_at", "reject": "rejected_at", "hold": "held_at",
                  "revoke": "revoked_at"}[decision]] = row["ts"]
            item["decisions"].append(row)
        elif row["type"] == "execution":
            if proposal_id not in proposals:
                raise ValueError(f"execution without proposal: {proposal_id}")
            item = proposals[proposal_id]
            outcome = row["outcome"]
            if outcome == "applied" and item["status"] != "adopted":
                raise ValueError(f"adoption was not decided: {proposal_id}")
            if outcome == "reverted" and item["status"] != "revoked":
                raise ValueError(f"reversal was not decided: {proposal_id}")
            if outcome not in {"applied", "reverted", "failed"}:
                raise ValueError(f"invalid execution outcome: {outcome}")
            if outcome == "applied":
                item["implemented_at"] = row["ts"]
            if outcome == "reverted":
                item["reverted_at"] = row["ts"]
            item["executions"].append(row)
        else:
            raise ValueError(f"invalid event type: {row['type']}")
    return sorted(proposals.values(), key=lambda item: (
        item["status"] not in ("pending", "held"),
        {"high": 0, "normal": 1, "low": 2}[item["priority"]],
        -timestamp(item["ts"]).timestamp()))


def envelope(kind, proposal_id):
    return {"id": str(uuid.uuid4()), "ts": dt.datetime.now().astimezone().isoformat(timespec="microseconds"),
            "host": socket.gethostname(), "type": kind, "proposal_id": proposal_id}


def validate_proposal(body):
    if not isinstance(body, dict):
        raise ValueError("proposal must be a JSON object")
    allowed = {"proposal_id", "source", "fact", "proposal", "reason", "recommendation",
               "priority", "evidence", "target"}
    if set(body) - allowed:
        raise ValueError("unknown proposal field")
    source = body.get("source")
    if not isinstance(source, dict) or set(source) != {"session", "worktree"}:
        raise ValueError("source requires session and worktree")
    source = {key: required_text(source[key], key, 240) for key in ("session", "worktree")}
    proposal_id = body.get("proposal_id") or str(uuid.uuid4())
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
    row = {**envelope("proposal", proposal_id), **body, "source": source,
           "evidence": evidence, "target": target}
    return row


def validate_decision(body):
    if not isinstance(body, dict) or set(body) - {"proposal_id", "decision", "actor", "reason", "result", "undo"}:
        raise ValueError("invalid decision object")
    proposal_id = required_text(body.get("proposal_id"), "proposal_id", 80)
    uuid.UUID(proposal_id)
    decision = body.get("decision")
    if decision not in DECISIONS:
        raise ValueError("decision must be adopt, reject, hold, or revoke")
    row = {**envelope("decision", proposal_id), "decision": decision,
           "actor": required_text(body.get("actor"), "actor", 120)}
    for key in ("reason", "result", "undo"):
        value = optional_text(body.get(key), key)
        if value is not None:
            row[key] = value
    return row


def validate_execution(body):
    if not isinstance(body, dict) or set(body) - {"proposal_id", "outcome", "actor", "result", "undo"}:
        raise ValueError("invalid execution object")
    proposal_id = required_text(body.get("proposal_id"), "proposal_id", 80)
    uuid.UUID(proposal_id)
    outcome = body.get("outcome")
    if outcome not in {"applied", "reverted", "failed"}:
        raise ValueError("outcome must be applied, reverted, or failed")
    row = {**envelope("execution", proposal_id), "outcome": outcome,
           "actor": required_text(body.get("actor"), "actor", 120),
           "result": required_text(body.get("result"), "result")}
    undo = optional_text(body.get("undo"), "undo")
    if undo is not None:
        row["undo"] = undo
    return row


def validate_evidence(body):
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


def append(events, imports, row):
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
        elif row["type"] == "decision":
            if proposal_id not in known:
                raise ValueError(f"proposal not found: {proposal_id}")
            previous = known[proposal_id]["status"]
            decision = row["decision"]
            if decision == "revoke" and previous != "adopted":
                raise ValueError("only adopted proposals can be revoked")
            if previous == "adopted" and decision in {"adopt", "reject", "hold"}:
                raise ValueError("revoke adoption before another decision")
        elif row["type"] == "execution":
            item = known.get(proposal_id)
            if item is None:
                raise ValueError(f"proposal not found: {proposal_id}")
            if row["outcome"] == "applied" and item["status"] != "adopted":
                raise ValueError("adoption was not decided")
            if row["outcome"] == "reverted" and item["status"] != "revoked":
                raise ValueError("reversal was not decided")
        else:
            if proposal_id not in known:
                raise ValueError(f"proposal not found: {proposal_id}")
        data = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if os.write(fd, data) != len(data):
            raise OSError("partial event write")
        os.fsync(fd)
    finally:
        os.close(fd)
    return row


def collect(config, imports):
    hosts = json.loads(config.read_text(encoding="utf-8"))["hosts"]
    if not isinstance(hosts, list):
        raise ValueError("hosts must be a list")
    imports.mkdir(parents=True, exist_ok=True, mode=0o700)
    errors = []
    for host in hosts:
        if not isinstance(host, dict) or set(host) != {"name", "ssh_target", "remote_path"}:
            raise ValueError("each host needs name, ssh_target, remote_path")
        name, target, remote = (host[key] for key in ("name", "ssh_target", "remote_path"))
        if (not isinstance(name, str) or not isinstance(target, str) or not isinstance(remote, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,39}", name)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,119}", target)
                or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,300}", remote)):
            raise ValueError(f"invalid host configuration: {name}")
        path = imports / f"{name}.jsonl"
        with tempfile.NamedTemporaryFile(dir=imports, prefix=f".{name}-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            result = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                     f"{target}:{remote}", str(temporary_path)], capture_output=True, timeout=20)
            if result.returncode:
                missing = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                          target, "test", "-e", remote], capture_output=True, timeout=10)
                if missing.returncode == 1 and (not path.exists() or not path.read_bytes()):
                    temporary_path.write_bytes(b"")
                else:
                    raise OSError(result.stderr.decode(errors="replace").strip())
            rows = read_rows(temporary_path)
            if any(row["host"] != name for row in rows):
                raise ValueError(f"host mismatch: {name}")
            previous = {row["id"]: row for row in read_rows(path)}
            current = {row["id"]: row for row in rows}
            if any(current.get(key) != value for key, value in previous.items()):
                raise ValueError(f"remote ledger is not append-only: {name}")
            os.replace(temporary_path, path)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{name}: {exc}")
        finally:
            temporary_path.unlink(missing_ok=True)
    if errors:
        raise RuntimeError("; ".join(errors))
    return len(hosts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--imports", type=Path, default=DEFAULT_IMPORTS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("propose", help="append one proposal from stdin JSON")
    commands.add_parser("decide", help="append one decision from stdin JSON")
    commands.add_parser("record", help="append one execution result from stdin JSON")
    commands.add_parser("support", help="append more source evidence to a proposal from stdin JSON")
    listing = commands.add_parser("list", help="read current proposals")
    listing.add_argument("--json", action="store_true")
    pulling = commands.add_parser("collect", help="copy remote Mac ledgers")
    pulling.add_argument("--config", type=Path, default=DEFAULT_HOSTS)
    args = parser.parse_args()
    try:
        if args.command == "propose":
            print(json.dumps(append(args.events, args.imports, validate_proposal(json.load(sys.stdin))), ensure_ascii=False))
        elif args.command == "decide":
            print(json.dumps(append(args.events, args.imports, validate_decision(json.load(sys.stdin))), ensure_ascii=False))
        elif args.command == "record":
            print(json.dumps(append(args.events, args.imports, validate_execution(json.load(sys.stdin))), ensure_ascii=False))
        elif args.command == "support":
            print(json.dumps(append(args.events, args.imports, validate_evidence(json.load(sys.stdin))), ensure_ascii=False))
        elif args.command == "list":
            items = project(all_rows(args.events, args.imports))
            if args.json:
                print(json.dumps({"items": items}, ensure_ascii=False))
            else:
                for item in items:
                    print(f"{item['proposal_id']}\t{item['status']}\t{item['priority']}\t{item['proposal']}")
        else:
            print(json.dumps({"collected": collect(args.config, args.imports)}))
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"adoption-triage: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
