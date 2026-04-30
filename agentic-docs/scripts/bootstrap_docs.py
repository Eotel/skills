#!/usr/bin/env python3
"""Copy the agentic-docs template scaffold into a target repository."""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        default=Path.cwd(),
        help="Repository root to receive the docs scaffold. Defaults to cwd.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without writing files.",
    )
    parser.add_argument(
        "--date",
        default=dt.date.today().isoformat(),
        help="Date used to replace YYYY-MM-DD placeholders.",
    )
    return parser.parse_args()


def iter_template_files(template_root: Path) -> list[Path]:
    return sorted(path for path in template_root.rglob("*") if path.is_file())


def render(content: str, review_date: str) -> str:
    return content.replace("YYYY-MM-DD", review_date)


def main() -> int:
    args = parse_args()
    skill_root = Path(__file__).resolve().parents[1]
    template_root = skill_root / "assets" / "templates"
    target_root = args.target.resolve()

    if not template_root.exists():
        raise SystemExit(f"Template root not found: {template_root}")
    if not target_root.exists():
        raise SystemExit(f"Target root not found: {target_root}")

    for source in iter_template_files(template_root):
        relative = source.relative_to(template_root)
        destination = target_root / relative
        exists = destination.exists()
        if exists and not args.force:
            print(f"skip existing {relative}")
            continue

        action = "overwrite" if exists else "create"
        print(f"{action} {relative}")
        if args.dry_run:
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        text = source.read_text(encoding="utf-8")
        destination.write_text(render(text, args.date), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
