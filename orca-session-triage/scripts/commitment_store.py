"""Route mirrored triage records through the installed board component."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import closing, contextmanager
from functools import lru_cache
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
from types import ModuleType


MIRRORED = frozenset(("ask-answers.jsonl", "ask-claims.jsonl", "lead-decisions.jsonl",
                      "handed-off.jsonl", "hourly-state.json"))


CONTRACT = ("writing", "read_lines", "read_document", "CommitmentWriteError")


class CommitmentWriteError(OSError):
    pass


@lru_cache
def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("orca_board_commitments", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load commitments component: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _component(path: Path) -> ModuleType | None:
    if path.name not in MIRRORED:
        return None
    binary = Path(os.environ.get("ORCA_BRIEFS_BIN") or "~/.cache/orca-briefs/bin").expanduser()
    component = binary / "orca_board_commitments.py"
    if not component.is_file():
        return None
    module = _load(component)
    return module if all(hasattr(module, name) for name in CONTRACT) else None


@contextmanager
def writing(path: Path, *, lines: list[str] | None = None,
            document: str | None = None) -> Generator[bool, None, None]:
    component = _component(path)
    if component is None:
        yield True
        return
    try:
        with component.writing(path, lines=lines, document=document) as write_file:
            yield write_file
    except component.CommitmentWriteError as error:
        raise CommitmentWriteError(str(error)) from error


def read_lines(path: Path) -> list[str]:
    component = _component(path)
    if component is not None:
        return component.read_lines(path)
    try:
        with path.open(encoding="utf-8") as stream:
            return stream.readlines()
    except FileNotFoundError:
        return []


def read_document(path: Path) -> str | None:
    component = _component(path)
    if component is not None:
        return component.read_document(path)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def hold_rows(path: Path) -> list[dict]:
    if path.name == "holds.sqlite":
        binary = Path(os.environ.get("ORCA_BRIEFS_BIN") or "~/.cache/orca-briefs/bin").expanduser()
        component = binary / "orca_board_commitments.py"
        if component.is_file():
            module = _load(component)
            if hasattr(module, "hold_rows"):
                return module.hold_rows(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM holds")]
