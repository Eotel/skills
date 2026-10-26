#!/usr/bin/env python3
"""Keep decisions shared by successive hourly triage sessions in one JSON file.

Use --state-file for an isolated copy. Mutations lock beside the file and replace
it atomically, so a waiting session and a new run cannot lose each other's edits.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

DEFAULT_STATE = "~/.cache/orca-session-triage/hourly-state.json"


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
    os.replace(temp, path)


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
    needs_owner = commands.add_parser("requests-needing-owner", help="list open requests without a live owner")
    needs_owner.add_argument("--live-owner", action="append", default=[],
                             help="active scan row identity (PATH or ENVIRONMENT::PATH); repeat per owner")
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
        with (path.parent / (path.name + ".lock")).open("a+") as lock:
            read_only = ("show", "left-now", "requests-needing-owner")
            fcntl.flock(lock, fcntl.LOCK_EX if args.action not in read_only else fcntl.LOCK_SH)
            data = read(path)
            if args.action == "left-now":
                same = data["left"].get(args.row, {}).get("final_message") == file_text(args.final_message_file)
                result = {"left": same}
            elif args.action == "requests-needing-owner":
                live = {owner_name(value) for value in args.live_owner}
                result = [{"id": key, **request} for key, request in data["requests"].items()
                          if request["closed_at"] is None and request["owner"] not in live]
            else:
                result = data if args.action == "show" else change(data, args)
            if args.action not in read_only:
                save(path, data)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"hourly state: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.action == "left-now" and not result["left"] else 0


if __name__ == "__main__":
    sys.exit(main())
