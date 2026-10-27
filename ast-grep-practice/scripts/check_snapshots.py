#!/usr/bin/env python3
"""Compare bundled snapshots with a fresh ast-grep generation; never rewrite them."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def files(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob("*.yml") if p.is_file()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("presets", nargs="+", type=Path,
                        help="Bundled preset directories containing rules/ and rule-tests/.")
    parser.add_argument("--ast-grep", default="ast-grep", help="Trusted CLI executable.")
    args = parser.parse_args()
    mismatch = False
    with tempfile.TemporaryDirectory(prefix="ast-grep-snapshots-") as directory:
        scratch = Path(directory)
        for index, supplied in enumerate(args.presets):
            preset = supplied.resolve()
            tests = preset / "rule-tests"
            if not (preset / "rules").is_dir() or not list(tests.glob("*-test.yml")):
                print(f"Invalid bundled preset: {preset}")
                return 2
            generated = scratch / str(index)
            isolated_tests = scratch / f"{index}-tests"
            isolated_tests.mkdir()
            for fixture in tests.glob("*-test.yml"):
                shutil.copyfile(fixture, isolated_tests / fixture.name)
            config = scratch / f"{index}.yml"
            # JSON is valid YAML. This config has no native-library declarations
            # and uses snapshotDir explicitly: the CLI flag loses to testConfigs.
            config.write_text(json.dumps({
                "ruleDirs": [str(preset / "rules")],
                "testConfigs": [{"testDir": str(isolated_tests), "snapshotDir": str(generated)}],
            }), encoding="utf-8")
            try:
                result = subprocess.run(
                    [args.ast_grep, "test", "--config", str(config), "-U"],
                    cwd=scratch, capture_output=True, text=True, check=False,
                )
            except OSError as error:
                print(f"Cannot run ast-grep: {error}")
                return 2
            if result.returncode:
                print(f"ast-grep failed ({result.returncode}): {result.stdout}{result.stderr}")
                return 2
            expected = files(generated)
            if not expected:
                print(f"No snapshots generated: {preset}")
                return 2
            committed = files(tests / "__snapshots__")
            changed = sorted(name for name in expected.keys() | committed.keys()
                             if expected.get(name) != committed.get(name))
            if changed:
                mismatch = True
                print(f"Snapshots differ: {preset}: {', '.join(changed)}")
            else:
                print(f"Snapshots current: {preset}: {len(expected)} files")
    return 1 if mismatch else 0


if __name__ == "__main__":
    raise SystemExit(main())
