"""Read the last agent hook entry Orca keeps for each pane.

Orca stores them in agent-hooks/last-status.db since stablyai/orca#39995 (2026-11-08): table
`rows`, one row per pane keyed `entry\\0<pane key>`, whose value is the entry as JSON (worktreeId,
paneKey, providerSession, payload, ...). It imports and deletes the older last-status.json.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any

DEFAULT_PATH = Path.home() / "Library/Application Support/orca/agent-hooks/last-status.db"
ENTRY = "entry\0"


class AgentStatusError(ValueError):
    pass


def pane_entries(path: Path = DEFAULT_PATH) -> dict[str, dict[str, Any]]:
    """Each pane's last hook entry by pane key; a missing or unreadable store raises AgentStatusError."""
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as db:
            rows = db.execute("SELECT key, value FROM rows").fetchall()
        # SQLite's string functions stop at the NUL inside each key, so the prefix is matched here.
        return {key.removeprefix(ENTRY): json.loads(value) for key, value in rows if key.startswith(ENTRY)}
    except (sqlite3.Error, ValueError) as exc:
        raise AgentStatusError(f"cannot read Orca's agent status {path}: {exc}") from exc
