"""Offline CLI checks for review candidates, source locations, and code exclusion."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "natural-japanese" / "scripts" / "check_japanese.py"


def run_check(text="", *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=text,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


class NaturalJapaneseScriptsTests(unittest.TestCase):
    def findings(self, text, *args):
        result = run_check(text, "--format", "json", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["findings"]

    def test_finds_all_six_candidates_in_prose(self):
        text = "正本を参照し、台帳に追記します。\nAPI の v2.1版です。\n静かに壊れた処理を閉包し、設定が効いています。\n"
        findings = self.findings(text)
        self.assertEqual([item["rule_id"] for item in findings], [
            "canonical-source", "ledger", "software-version",
            "silent-breakage", "closure", "effective",
        ])
        for item in findings:
            self.assertTrue(item["reason"])
            self.assertTrue(item["alternatives"])
            self.assertTrue(item["keep_when"])

    def test_reports_unicode_columns_and_full_line_context(self):
        text = "説明です。正本を参照。\n台帳を確認。\n"
        findings = self.findings(text)
        self.assertEqual([(item["line"], item["column"]) for item in findings], [(1, 6), (2, 1)])
        self.assertEqual(findings[0]["match"], "正本")
        self.assertEqual(findings[0]["context"], "説明です。正本を参照。")
        self.assertEqual(findings[0]["path"], "<stdin>")

    def test_preserves_positions_after_inline_code(self):
        findings = self.findings("`台帳` の正本。")
        self.assertEqual(len(findings), 1)
        self.assertEqual((findings[0]["rule_id"], findings[0]["column"]), ("canonical-source", 7))

    def test_skips_backtick_and_tilde_fences_and_inline_code(self):
        text = "```python\n'正本'\n```\n~~~text\n台帳\n~~~\n`閉包` と ``静かに壊れる ` 効く``\nこの版を確認。\n"
        findings = self.findings(text)
        self.assertEqual([item["rule_id"] for item in findings], ["software-version"])
        self.assertEqual(findings[0]["line"], 8)

    def test_shorter_fence_does_not_end_code_block(self):
        text = "````text\n```\n正本\n````\n台帳\n"
        findings = self.findings(text)
        self.assertEqual([item["rule_id"] for item in findings], ["ledger"])
        self.assertEqual(findings[0]["line"], 5)

    def test_unclosed_fence_masks_remaining_code(self):
        self.assertEqual(self.findings("```\n正本\n台帳\n"), [])

    def test_multiline_inline_code_and_unmatched_backticks(self):
        findings = self.findings("`台帳\n閉包`\n未対応の `正本。\n")
        self.assertEqual([item["rule_id"] for item in findings], ["canonical-source"])
        self.assertEqual(findings[0]["line"], 3)

    def test_can_scan_code_explicitly_or_as_plain_text(self):
        text = "```\n正本\n```\n`閉包`\n"
        for args in (("--include-code",), ("--input-format", "text")):
            with self.subTest(args=args):
                self.assertEqual([item["rule_id"] for item in self.findings(text, *args)],
                                 ["canonical-source", "closure"])

    def test_leaves_common_publication_and_type_words_unflagged(self):
        text = "出版、版画、日本語版、無料版、安定版。\n効率と有効期限を確認します。\n有効か、無効か、失効かを確認します。\nバージョン 2.1 に更新します。\n"
        self.assertEqual(self.findings(text), [])

    def test_version_candidates_remain_contextual(self):
        findings = self.findings("v1.2版、3版、この版、旧版、最新版。\n")
        self.assertEqual(len(findings), 5)
        self.assertTrue(all(item["rule_id"] == "software-version" for item in findings))
        self.assertTrue(any("書籍" in context for context in findings[0]["keep_when"]))

    def test_normal_uses_are_review_candidates_and_do_not_fail_by_default(self):
        text = "契約書の正本を保管。固定資産台帳を確認。数学の閉包。薬が効く。\n"
        findings = self.findings(text)
        self.assertEqual(len(findings), 4)
        self.assertIn("見直し候補", run_check(text).stdout)

    def test_inflected_forms_are_detected(self):
        findings = self.findings("静かに壊れます。静かに壊れた。\n設定が効いた。効かない。効きます。\n")
        self.assertEqual([item["rule_id"] for item in findings],
                         ["silent-breakage"] * 2 + ["effective"] * 3)

    def test_guard_noun_is_a_candidate_without_flagging_the_verb(self):
        findings = self.findings("止めが効いていない。止めを通り抜けた。止めを確実に効かせる。\n"
                                 "処理を止める。歯止めがかかった。\n")
        self.assertEqual([item["match"] for item in findings if item["rule_id"] == "guard-noun"],
                         ["止め", "止め", "止め"])

    def test_opt_in_exit_status_means_review_is_needed(self):
        result = run_check("正本です。", "--fail-on-findings", "--format", "json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(json.loads(result.stdout)["findings"]), 1)
        self.assertEqual(run_check("参照先です。", "--fail-on-findings").returncode, 0)

    def test_empty_input_has_no_findings(self):
        self.assertEqual(self.findings(""), [])
        self.assertIn("登録済み", run_check("").stdout)

    def test_multiple_files_and_stdin_do_not_modify_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.md"
            second = Path(directory) / "second.md"
            first.write_text("台帳です。\n", encoding="utf-8")
            second.write_text("正本です。\n", encoding="utf-8")
            before = [path.read_bytes() for path in (first, second)]
            findings = self.findings("閉包します。", str(first), str(second), "-")
            self.assertEqual([item["path"] for item in findings], [str(first), str(second), "<stdin>"])
            self.assertEqual([path.read_bytes() for path in (first, second)], before)

    def test_input_errors_return_two_without_partial_json_or_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.md"
            invalid = Path(directory) / "invalid.md"
            missing = Path(directory) / "missing.md"
            valid.write_text("正本です。", encoding="utf-8")
            invalid.write_bytes(b"\xff\xfe")
            for path in (missing, invalid, Path(directory)):
                with self.subTest(path=path):
                    result = run_check("", str(valid), str(path), "--format", "json")
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("検査できません", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_help_and_explicit_stdin(self):
        help_result = run_check("", "--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("自動置換はしません", help_result.stdout)
        self.assertEqual(len(self.findings("正本です。", "-")), 1)
        self.assertEqual(run_check("", "-", "-").returncode, 2)


if __name__ == "__main__":
    unittest.main()
