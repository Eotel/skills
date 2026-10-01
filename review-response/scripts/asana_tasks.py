#!/usr/bin/env python3
"""List and update Asana review tasks; configuration comes from the caller."""

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


FIELDS = (
    "name,completed,assignee.gid,assignee.name,modified_at,created_at,permalink_url,"
    "custom_fields.gid,custom_fields.text_value,custom_fields.display_value"
)


def read_pat(env_file=None, environ=None):
    """Prefer ASANA_PAT in the environment; read only its assignment from a file."""
    environ = os.environ if environ is None else environ
    if environ.get("ASANA_PAT", "").strip():
        return environ["ASANA_PAT"].strip()
    if env_file:
        try:
            with Path(env_file).open(encoding="utf-8") as stream:
                for line in stream:
                    match = re.match(r"^\s*(?:export\s+)?ASANA_PAT\s*=\s*(.*?)\s*$", line)
                    if not match:
                        continue
                    value = match[1]
                    if value.startswith(("'", '"')):
                        end = value.find(value[0], 1)
                        tail = value[end + 1:].strip() if end != -1 else ""
                        if end == -1 or (tail and not tail.startswith("#")):
                            raise ValueError("Invalid ASANA_PAT assignment in --env-file")
                        value = value[1:end]
                    else:
                        value = re.sub(r"\s+#.*$", "", value).strip()
                    if value:
                        return value
        except (OSError, UnicodeError):
            raise ValueError("Cannot read --env-file") from None
    raise ValueError("Missing ASANA_PAT; set ASANA_PAT or pass --env-file")


def task_record(task, url_field):
    field = next(
        (field for field in task.get("custom_fields") or [] if field.get("gid") == url_field),
        {},
    )
    assignee = task.get("assignee") or {}
    return {
        "gid": task["gid"],
        "name": task["name"],
        "completed": task["completed"],
        "assignee": assignee.get("name"),
        "assignee_gid": assignee.get("gid"),
        "url": field.get("text_value") or field.get("display_value"),
        "modified_at": task.get("modified_at"),
        "created_at": task.get("created_at"),
        "permalink_url": task.get("permalink_url"),
    }


class Asana:
    def __init__(self, pat):
        self.pat = pat

    def call(self, method, path, data=None, params=None):
        url = "https://app.asana.com/api/1.0" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = json.dumps({"data": data}).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={
                "Authorization": "Bearer " + self.pat,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Asana request failed (HTTP {exc.code})") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise ValueError("Asana request failed; check connectivity and authentication") from None
        except ValueError:
            # HTTP header validation can include the token in its exception text.
            raise ValueError("Invalid Asana response or request") from None

    def project_tasks(self, project, incomplete=False):
        params = {"limit": 100, "opt_fields": FIELDS,
                  "completed_since": "now" if incomplete else "1970-01-01T00:00:00Z"}
        while True:
            page = self.call("GET", "/projects/" + urllib.parse.quote(project, safe="") + "/tasks", params=params)
            yield from page["data"]
            offset = (page.get("next_page") or {}).get("offset")
            if not offset:
                return
            params["offset"] = offset

    def show(self, gid):
        return self.call("GET", "/tasks/" + urllib.parse.quote(gid, safe=""), params={"opt_fields": FIELDS})["data"]


def add_options(parser):
    # SUPPRESS lets these options appear before or after the subcommand.
    parser.add_argument("--env-file", default=argparse.SUPPRESS, help="read only ASANA_PAT from this file (environment takes precedence)")
    parser.add_argument("--project", default=argparse.SUPPRESS, help="project ID (default: ASANA_PROJECT)")
    parser.add_argument("--url-field", default=argparse.SUPPRESS, help="URL custom field ID (default: ASANA_URL_FIELD)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog="All results are JSON lines. No task title suffix is added.")
    add_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    returned = commands.add_parser("returned", help="incomplete project tasks assigned to a user ID")
    returned.add_argument("--assignee", required=True, help="Asana user ID")
    find = commands.add_parser("find", help="find tasks by exact URL custom field, including completed tasks")
    find.add_argument("url")
    commands.add_parser("index", help="list open and completed project tasks with task links")
    create = commands.add_parser("create", help="create a task with the caller's full title")
    create.add_argument("url")
    create.add_argument("title")
    create.add_argument("--assignee", required=True, help="Asana user ID")
    for name in ("complete", "show"):
        commands.add_parser(name, help=f"{name} one or more tasks").add_argument("gids", nargs="+")
    for command in commands.choices.values():
        add_options(command)
    args = parser.parse_args(argv)
    try:
        pat = read_pat(getattr(args, "env_file", None))
        project = getattr(args, "project", os.environ.get("ASANA_PROJECT"))
        url_field = getattr(args, "url_field", os.environ.get("ASANA_URL_FIELD"))
        if not url_field:
            raise ValueError("Missing URL field; pass --url-field or set ASANA_URL_FIELD")
        if args.command in ("returned", "find", "index", "create") and not project:
            raise ValueError("Missing project; pass --project or set ASANA_PROJECT")
        client = Asana(pat)

        def emit(task):
            print(json.dumps(task_record(task, url_field), ensure_ascii=False))

        if args.command in ("returned", "find", "index"):
            for task in client.project_tasks(project, incomplete=args.command == "returned"):
                record = task_record(task, url_field)
                if args.command == "returned":
                    matches = not record["completed"] and record["assignee_gid"] == args.assignee
                elif args.command == "find":
                    matches = (record["url"] or "").rstrip("/") == args.url.rstrip("/")
                else:
                    matches = True
                if matches:
                    emit(task)
        elif args.command == "create":
            task = client.call("POST", "/tasks", {
                "name": args.title, "projects": [project], "assignee": args.assignee,
                "notes": "", "custom_fields": {url_field: args.url},
            })["data"]
            emit(client.show(task["gid"]))
        else:
            for gid in args.gids:
                if args.command == "complete":
                    client.call("PUT", "/tasks/" + urllib.parse.quote(gid, safe=""), {"completed": True})
                emit(client.show(gid))
    except (ValueError, KeyError) as exc:
        # Never echo HTTP response bodies, file contents, or request headers.
        message = str(exc) if not isinstance(exc, (KeyError, json.JSONDecodeError)) else "Invalid Asana response"
        print("asana_tasks: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
