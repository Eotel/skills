#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


SLIDE = re.compile(r"ppt/slides/slide[0-9]+\.xml")
NOTES = re.compile(r"ppt/notesSlides/notesSlide[0-9]+\.xml")
NAMESPACES = (
    {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
     "p": "http://schemas.openxmlformats.org/presentationml/2006/main"},
    {"a": "http://purl.oclc.org/ooxml/drawingml/main",
     "p": "http://purl.oclc.org/ooxml/presentationml/main"},
)
NOTES_CHROME = {"dt", "hdr", "ftr", "sldNum", "sldImg"}


def name_pattern(path: Path) -> re.Pattern[str] | None:
    names = {value.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
             if not line.lstrip().startswith("#") for value in line.split("\t") if value.strip()}
    if not names:
        return None
    patterns = []
    for name in sorted(names, key=lambda value: (-len(value), value)):
        before = r"(?<![A-Za-z0-9_])" if name[0].isascii() and name[0].isalnum() else ""
        after = r"(?![A-Za-z0-9_])" if name[-1].isascii() and name[-1].isalnum() else ""
        patterns.append(before + re.escape(name) + after)
    return re.compile("|".join(patterns), re.IGNORECASE)


def paragraphs(root: ET.Element, ns: dict[str, str]) -> Iterator[str]:
    line_break = "{" + ns["a"] + "}br"
    for paragraph in root.findall(".//a:p", ns):
        text = "".join("\n" if node.tag == line_break else node.findtext("a:t", "", ns) for node in paragraph)
        yield from (line.strip() for line in text.splitlines() if line.strip())


def inspect_part(part: str, root: ET.Element, names: re.Pattern[str] | None) -> Iterator[str]:
    for ns in NAMESPACES:
        for extent in root.findall(".//a:ext", ns):
            negative = [f'{axis}="{value}"' for axis in ("cx", "cy")
                        if (value := extent.get(axis)) is not None and int(value) < 0]
            if negative:
                yield f"負の寸法 a:ext {' '.join(negative)}"
        if SLIDE.fullmatch(part):
            for line in paragraphs(root, ns):
                if line.startswith("※"):
                    yield f"※ で始まる注記 {line}"
                if names:
                    for name in names.finditer(line):
                        if not line[name.end():].lstrip().startswith("様"):
                            yield f"「様」が続かない名前 {name[0]}（{line}）"
        elif NOTES.fullmatch(part):
            for shape in root.findall(".//p:sp", ns):
                placeholder = shape.find("p:nvSpPr/p:nvPr/p:ph", ns)
                if placeholder is None or placeholder.get("type") not in NOTES_CHROME:
                    for line in paragraphs(shape, ns):
                        yield f"ノートに残ったテキスト {line}"


def lint(deck: Path, names: re.Pattern[str] | None) -> Iterator[str]:
    with ZipFile(deck) as archive:
        parts = sorted(part for part in archive.namelist() if part.startswith("ppt/") and part.endswith(".xml"))
        if not any(SLIDE.fullmatch(part) for part in parts):
            raise ValueError("スライドの XML がありません。")
        for part in parts:
            try:
                root = ET.fromstring(archive.read(part))
                for finding in inspect_part(part, root, names):
                    yield f"{part}: {finding}"
            except (ET.ParseError, ValueError) as error:
                raise ValueError(f"{part}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="PPTX の注記、ノート、負の寸法、名前の敬称を検査します。")
    parser.add_argument("deck", type=Path, metavar="PPTX")
    parser.add_argument("--names-file", required=True, type=Path, metavar="FILE",
                        help="UTF-8 の名前一覧。1行に1つの正式名称と、必要に応じてタブ区切りの別表記")
    args = parser.parse_args()
    try:
        findings = list(lint(args.deck, name_pattern(args.names_file)))
    except (OSError, ValueError, BadZipFile, RuntimeError) as error:
        print(f"deck_lint: {error}", file=sys.stderr)
        return 2
    if findings:
        print("\n".join(findings))
        return 1
    print("PPTX の検査4項目に指摘はありません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
