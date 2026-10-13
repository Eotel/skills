#!/usr/bin/env python3
"""Find Japanese word choices that need contextual review; never rewrite input."""

import argparse
import bisect
import json
from pathlib import Path
import re
import sys


RULES_PATH = Path(__file__).resolve().parents[1] / "references" / "word-choice.json"
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n)?$")


def load_rules():
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    rules = data["rules"]
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules は空でない配列にしてください")
    ids = set()
    compiled = []
    for rule in rules:
        for key in ("id", "term", "pattern", "reason"):
            if not isinstance(rule[key], str) or not rule[key]:
                raise ValueError(f"{key} は空でない文字列にしてください")
        for key in ("alternatives", "keep_when"):
            if not isinstance(rule[key], list) or not all(
                isinstance(item, str) and item for item in rule[key]
            ) or not rule[key]:
                raise ValueError(f"{key} は空でない文字列の配列にしてください")
        if rule["id"] in ids:
            raise ValueError(f"id が重複しています: {rule['id']}")
        ids.add(rule["id"])
        pattern = re.compile(rule["pattern"])
        if pattern.search("") is not None:
            raise ValueError(f"空文字列に一致するルールです: {rule['id']}")
        compiled.append((rule, pattern))
    return compiled


def mask_markdown_code(source):
    """Mask common fenced/inline code while preserving offsets and newlines."""
    chars = list(source)

    def mask(start, end):
        for index in range(start, end):
            if chars[index] not in "\r\n":
                chars[index] = " "

    fence = None
    offset = 0
    for line in source.splitlines(keepends=True):
        if fence is not None:
            mask(offset, offset + len(line))
            char, length = fence
            if re.fullmatch(
                r" {0,3}" + re.escape(char) + "{" + str(length) + r",}[ \t]*(?:\r?\n)?",
                line,
            ):
                fence = None
        else:
            opener = FENCE.fullmatch(line)
            if opener and (opener[1][0] != "`" or "`" not in opener[2]):
                fence = (opener[1][0], len(opener[1]))
                mask(offset, offset + len(line))
        offset += len(line)

    fenced = "".join(chars)
    position = 0
    while opener := re.search(r"`+", fenced[position:]):
        start = position + opener.start()
        end = position + opener.end()
        delimiter = opener.group()
        closer = re.search(r"(?<!`)" + re.escape(delimiter) + r"(?!`)", fenced[end:])
        if closer is None:
            position = end
        else:
            position = end + closer.end()
            mask(start, position)
    return "".join(chars)


def scan(source, path, rules, *, markdown=True):
    searchable = mask_markdown_code(source) if markdown else source
    line_starts = [0] + [match.end() for match in re.finditer("\n", source)]
    lines = source.split("\n")
    findings = []
    for rule, pattern in rules:
        for match in pattern.finditer(searchable):
            if match.start() == match.end():
                raise ValueError(f"空文字列に一致するルールです: {rule['id']}")
            line_index = bisect.bisect_right(line_starts, match.start()) - 1
            findings.append({
                "path": path,
                "line": line_index + 1,
                "column": match.start() - line_starts[line_index] + 1,
                "rule_id": rule["id"],
                "term": rule["term"],
                "match": source[match.start():match.end()],
                "context": lines[line_index].rstrip("\r"),
                "reason": rule["reason"],
                "alternatives": rule["alternatives"],
                "keep_when": rule["keep_when"],
            })
    return sorted(findings, key=lambda item: (item["line"], item["column"], item["rule_id"]))


def print_text(findings):
    if not findings:
        print("見直し候補はありません（登録済みの表現だけを確認）。")
        return
    for item in findings:
        print(f"{item['path']}:{item['line']}:{item['column']}: "
              f"見直し候補 [{item['rule_id']}] {item['match']}")
        print(f"  {item['context']}")
        print(f"  確認: {item['reason']}")
        print("  言い換えの例: " + " / ".join(item["alternatives"]))
        print("  残す場面: " + " / ".join(item["keep_when"]))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="人に向けた日本語から、文脈を見て見直す表現の候補を拾います。自動置換はしません。"
    )
    parser.add_argument("paths", nargs="*", metavar="FILE", help="UTF-8 の原稿。省略または - で標準入力")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="出力形式（既定: text）")
    parser.add_argument("--input-format", choices=("markdown", "text"), default="markdown",
                        help="入力形式（既定: markdown。通常のコード部分を除外）")
    parser.add_argument("--include-code", action="store_true", help="Markdown のコード部分も検査")
    parser.add_argument("--fail-on-findings", action="store_true", help="見直し候補がある場合に終了コード 1 を返す")
    args = parser.parse_args(argv)
    paths = args.paths or ["-"]
    if paths.count("-") > 1:
        parser.error("標準入力の指定は一度だけにしてください")
    try:
        rules = load_rules()
        findings = []
        for path in paths:
            source = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
            findings.extend(scan(
                source, "<stdin>" if path == "-" else path, rules,
                markdown=args.input_format == "markdown" and not args.include_code,
            ))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, re.error) as error:
        parser.exit(2, f"検査できません: {error}\n")
    if args.format == "json":
        print(json.dumps({"findings": findings}, ensure_ascii=False, indent=2))
    else:
        print_text(findings)
    return 1 if args.fail_on_findings and findings else 0


if __name__ == "__main__":
    sys.exit(main())
