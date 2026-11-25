from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

SCRIPT = Path(__file__).resolve().parents[1] / "natural-japanese/scripts/deck_lint.py"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def xml_part(root: str, shapes: str) -> str:
    return f'<p:{root} xmlns:a="{A}" xmlns:p="{P}"><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:{root}>'


def text_shape(*runs: str, placeholder: str = "") -> str:
    properties = f'<p:nvSpPr><p:nvPr><p:ph type="{placeholder}"/></p:nvPr></p:nvSpPr>' if placeholder else ""
    text = "".join(f"<a:r><a:t>{run}</a:t></a:r>" for run in runs)
    return f"<p:sp>{properties}<p:txBody><a:p>{text}</a:p></p:txBody></p:sp>"


class DeckLintTests(unittest.TestCase):
    def lint(self, slides: str, notes: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = root / "names.txt"
            names.write_text("架空テスト工業株式会社\t架空テスト工業\tFictionalTest Industries\n", encoding="utf-8")
            deck = root / "deck.pptx"
            with ZipFile(deck, "w") as archive:
                archive.writestr("ppt/slides/slide1.xml", xml_part("sld", slides))
                archive.writestr("ppt/notesSlides/notesSlide1.xml", xml_part("notes", notes))
            return subprocess.run([sys.executable, str(SCRIPT), "--names-file", str(names), str(deck)],
                                  text=True, capture_output=True, check=False)

    def test_reports_four_checks_with_a_part_location_and_exit_one(self) -> None:
        result = self.lint(
            text_shape("　※", "注記") + text_shape("FictionalTest ", "Industries への提案")
            + '<p:cxnSp><p:spPr><a:xfrm><a:ext cx="1" cy="-502920"/></a:xfrm></p:spPr></p:cxnSp>',
            text_shape("提出前のメモ", placeholder="body"),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        for item in ("※", "ノート", 'cy="-502920"', "様", "ppt/slides/slide1.xml", "ppt/notesSlides/notesSlide1.xml"):
            self.assertIn(item, result.stdout)

    def test_allows_qualified_names_nonnegative_sizes_and_notes_slide_numbers(self) -> None:
        result = self.lint(
            text_shape("架空テスト工業株式会社", " 様") + text_shape("参照先は FictionalTest Industries様です。")
            + '<p:cxnSp><p:spPr><a:xfrm><a:ext cx="0" cy="2"/></a:xfrm></p:spPr></p:cxnSp>',
            text_shape("1", placeholder="sldNum") + text_shape("　", placeholder="body"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
