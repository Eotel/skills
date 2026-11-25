#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
from __future__ import annotations

import argparse
from datetime import date
import json
import re
import subprocess
import sys
from collections.abc import Iterator


HEADING = re.compile(r"^ {0,3}(#{1,2})[ \t]+(.*?)[ \t#]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
START = re.compile(r"^( {0,3}[-*+][ \t]+(?:着手|Start)[ \t]*[:：])[ \t]*(.*)$", re.I)
EFFORT = re.compile(r"^( {0,3}[-*+][ \t]+(?:予定工数|Estimated effort)[ \t]*[:：])[ \t]*(.*)$", re.I)
DATES = re.compile(r"(?:From\b[ \t]*(.*?)[ \t]*/[ \t]*)?To\b[ \t]*([^（(]*?)([ \t]*[（(].*)?", re.I)
DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")


def markdown_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    fence = ""
    for index, line in enumerate(lines):
        text = line.rstrip("\r\n")
        match = FENCE.fullmatch(text)
        if fence:
            if match and match[1].startswith(fence) and not match[2].strip():
                fence = ""
        elif match:
            fence = match[1]
        else:
            yield index, text
    if fence:
        raise ValueError("本文に閉じていないコードフェンスがあります。")


def date_fields(start: str, done: str) -> str:
    start_field = f"From {start}".rstrip()
    done_field = f"To {done}".rstrip()
    return f"{start_field} / {done_field}"


def schedule_body(body: str, start: str | None, done: str | None, effort: str | None) -> str:
    lines = body.splitlines(keepends=True)
    visible = list(markdown_lines(lines))
    headings = [(index, match) for index, line in visible if (match := HEADING.fullmatch(line))]
    schedules = [(index, match[2].casefold()) for index, match in headings
                 if match[1] == "##" and match[2].casefold() in {"予定", "schedule"}]
    if len(schedules) > 1:
        raise ValueError("本文に予定セクションが複数あります。")
    if not schedules:
        newline = "\r\n" if "\r\n" in body else "\n"
        separator = "" if not body else newline if body.endswith(newline) else newline * 2
        if body.endswith(newline * 2):
            separator = ""
        effort_value = f" {effort} 人日" if effort is not None else ""
        schedule = ["## 予定", f"- 着手: {date_fields(start or '', done or '')}",
                    f"- 予定工数:{effort_value}", ""]
        return body + separator + newline.join(schedule)

    begin, title = schedules[0]
    end = next((index for index, _ in headings if index > begin), len(lines))
    section = [(index, line) for index, line in visible if begin < index < end]
    start_rows = [(index, match) for index, line in section if (match := START.fullmatch(line))]
    effort_rows = [(index, match) for index, line in section if (match := EFFORT.fullmatch(line))]
    if len(start_rows) != 1 or len(effort_rows) != 1:
        raise ValueError("予定セクションには着手行と予定工数行を1行ずつ指定してください。")

    if start is not None or done is not None:
        index, row = start_rows[0]
        dates = DATES.fullmatch(row[2])
        if dates is None:
            raise ValueError("着手行の日付は From … / To … または To … の形式で指定してください。")
        from_value = start if start is not None else (dates[1] or "").strip()
        to_value = done if done is not None else dates[2].strip()
        text = f"{row[1]} {date_fields(from_value, to_value)}{dates[3] or ''}"
        lines[index] = text + lines[index][len(lines[index].rstrip("\r\n")):]
    if effort is not None:
        index, row = effort_rows[0]
        note = re.search(r"[（(].*", row[2])
        unit = "人日" if title == "予定" else "person-days"
        suffix = (" " if note[0].startswith("(") else "") + note[0] if note else ""
        lines[index] = f"{row[1]} {effort} {unit}{suffix}" + lines[index][len(lines[index].rstrip("\r\n")):]
    return "".join(lines)


def iso_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("日付を YYYY-MM-DD で指定してください。")


def person_days(value: str) -> str:
    if not DECIMAL.fullmatch(value):
        raise argparse.ArgumentTypeError("予定工数を0以上の数値で指定してください。")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Issue の予定セクションを更新します。")
    parser.add_argument("repository", metavar="OWNER/REPO")
    parser.add_argument("number", type=int)
    dates = parser.add_mutually_exclusive_group()
    dates.add_argument("--start", nargs="?", const=date.today().isoformat(), type=iso_date,
                       help="From を更新し、gh の認証ユーザーを担当に追加します。日付の省略時は今日")
    dates.add_argument("--done", nargs="?", const=date.today().isoformat(), type=iso_date,
                       help="To を更新します。日付の省略時は今日")
    parser.add_argument("--effort", type=person_days, help="本人の予定工数（人日）")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*/[a-zA-Z0-9_.-]+", args.repository) or args.number < 1:
        parser.error("OWNER/REPO と正の Issue 番号を指定してください。")
    if all(value is None for value in (args.start, args.done, args.effort)):
        parser.error("--start、--done、--effort のいずれかを指定してください。")

    target = [str(args.number), "-R", args.repository]
    view = ["gh", "issue", "view", *target, "--json", "body"]
    try:
        original = json.loads(subprocess.check_output(view, encoding="utf-8"))["body"]
        changed = schedule_body(original, args.start, args.done, args.effort)
        edit = ["gh", "issue", "edit", *target]
        if changed != original:
            if json.loads(subprocess.check_output(view, encoding="utf-8"))["body"] != original:
                raise ValueError("処理中に Issue 本文が更新されました。再実行してください。")
            edit.extend(["--body-file", "-"])
        if args.start is not None:
            edit.extend(["--add-assignee", "@me"])
        if changed != original or args.start is not None:
            subprocess.run(edit, input=changed, encoding="utf-8", check=True)
        else:
            print(f"{args.repository}#{args.number} の予定は指定値と一致しています。")
    except (OSError, KeyError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"issue-schedule: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
