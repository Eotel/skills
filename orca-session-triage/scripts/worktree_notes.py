"""Read keep notes from a session manager without coupling removal checks to it."""

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Protocol

KEEP_WORDS = ("消さない", "残す")


def keep_note(comment: str | None) -> str | None:
    return comment if comment and any(word in comment for word in KEEP_WORDS) else None


class NotesUnavailable(Exception):
    """The session manager's worktree note could not be read."""


class WorktreeNotes(Protocol):
    def comment(self, worktree: Path) -> str | None:
        """Return the note, None for an unknown worktree, or raise NotesUnavailable."""
        ...


@dataclass(frozen=True)
class OrcaNotes:
    timeout: float = 30

    def comment(self, worktree: Path) -> str | None:
        try:
            result = subprocess.run(
                ["orca", "worktree", "show", "--worktree", "path:" + str(worktree.resolve()), "--json"],
                capture_output=True, text=True, timeout=self.timeout,
            )
            payload = json.loads(result.stdout)
            error = payload.get("error")
            if payload.get("ok") is False and isinstance(error, dict) and error.get("code") == "selector_not_found":
                return None
            if result.returncode or payload.get("ok") is not True:
                raise NotesUnavailable(str(error or result.stderr or "orca exited %d" % result.returncode))
            return payload["result"]["worktree"].get("comment")
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError, AttributeError) as error:
            raise NotesUnavailable(str(error)) from error
