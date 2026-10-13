"""Check snapshot coherence without requiring a real CLI in the Python test suite."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ast-grep-practice/scripts/check_snapshots.py"


class SnapshotChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.preset = self.root / "preset"
        (self.preset / "rules").mkdir(parents=True)
        (self.preset / "rule-tests/__snapshots__").mkdir(parents=True)
        (self.preset / "rule-tests/example-test.yml").write_text("id: example\n")
        self.baseline = self.preset / "rule-tests/__snapshots__/example.yml"
        self.baseline.write_text("current output\n")
        (self.preset / "expected.json").write_text(json.dumps({"example.yml": "current output\n"}))
        self.cli = self.root / "fake-ast-grep"
        self.cli.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
config = json.loads(Path(sys.argv[sys.argv.index('--config') + 1]).read_text())
entry = config['testConfigs'][0]
preset = Path(config['ruleDirs'][0]).parent
if (preset / 'fail').exists():
    print('fixture CLI failure', file=sys.stderr)
    sys.exit(7)
out = Path(entry['snapshotDir']); out.mkdir(parents=True)
for name, value in json.loads((preset / 'expected.json').read_text()).items():
    (out / name).write_text(value)
''')
        self.cli.chmod(0o755)

    def run_check(self):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.preset),
                               "--ast-grep", str(self.cli)],
                              capture_output=True, text=True, check=False)

    def test_current_snapshots_pass_without_rewriting_baseline(self):
        before = self.baseline.read_bytes()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_obsolete_records_fail_and_preserve_baseline(self):
        self.baseline.write_text("current output\nobsolete match\n")
        before = self.baseline.read_bytes()
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Snapshots differ", result.stdout)
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_missing_baseline_fails(self):
        self.baseline.unlink()
        self.assertEqual(self.run_check().returncode, 1)

    def test_cli_failure_cannot_pass(self):
        (self.preset / "fail").write_text("")
        result = self.run_check()
        self.assertEqual(result.returncode, 2)
        self.assertIn("fixture CLI failure", result.stdout)

    def test_empty_generation_cannot_pass(self):
        (self.preset / "expected.json").write_text("{}")
        self.assertEqual(self.run_check().returncode, 2)


if __name__ == "__main__":
    unittest.main()
