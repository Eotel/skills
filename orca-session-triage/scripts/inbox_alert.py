#!/usr/bin/env python3
"""Show a macOS notification for each new ask in the triage inbox.

Pressing the notification brings the sender's agent tab forward when
terminal-notifier is installed and the agent host can still find that tab;
otherwise the notification only shows the ask. This reader has its own byte
cursor. The launchd service initializes that cursor at the current end, so
enabling alerts never replays old questions.
"""

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Protocol

from inbox_read import complete_end


APPLE_SCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)

Item = Mapping[str, object]


@dataclass(frozen=True)
class Command:
    """Run this argv when the notification is pressed."""

    argv: tuple[str, ...]


@dataclass(frozen=True)
class Link:
    """Open this URL when the notification is pressed."""

    url: str


ClickAction = Command | Link


class SenderTabs(Protocol):
    """The agent host that knows where the lane behind an ask is running."""

    def action(self, item: Item) -> ClickAction | None: ...


class Notifier(Protocol):
    def show(self, title: str, body: str, action: ClickAction | None) -> None: ...


def same_machine(left: str, right: str) -> bool:
    return left.removesuffix(".local").casefold() == right.removesuffix(".local").casefold()


@dataclass(frozen=True)
class OrcaTabs:
    """Find the sender's tab with the Orca CLI; another Mac is a paired Orca environment."""

    orca: str
    local_host: str
    # Paired server per host. A server sometimes stalls a call; asking once keeps the next asks prompt.
    paired: dict[str, str] = field(default_factory=dict)

    def result(self, *args: str) -> dict[str, Any]:
        # Calls answer within a second; a stalled server would delay every later notification.
        out = subprocess.run([self.orca, *args, "--json"], capture_output=True, text=True,
                             check=True, timeout=5).stdout
        data = json.loads(out)
        return data.get("result", data)

    def environment(self, host: str) -> str | None:
        """Name of the paired Orca server on host; None for this Mac."""
        if not host or same_machine(host, self.local_host):
            return None
        machine = host.removesuffix(".local").casefold()
        if machine not in self.paired:
            names = (environment["name"] for environment in self.result("environment", "list")["environments"])
            paired = next((name for name in names if same_machine(
                self.result("host", "name", "--environment", name)["machineName"], host)), None)
            if paired is None:
                raise LookupError(f"no paired Orca server runs on {host}")
            self.paired[machine] = paired
        return self.paired[machine]

    def action(self, item: Item) -> ClickAction | None:
        errors = (OSError, ValueError, KeyError, LookupError, subprocess.SubprocessError)
        handle = item.get("handle")
        try:
            environment = self.environment(str(item.get("host") or ""))
        except errors as exc:
            print(f"inbox-alert: cannot find the sender's Orca: {exc}", file=sys.stderr)
            return None
        target = ("--environment", environment) if environment else ()
        try:
            terminals = [terminal for terminal in self.result("terminal", "list", *target)["terminals"]
                         if terminal.get("connected")]
        except errors as exc:
            # The reported tab is usually still open, so pressing it can switch there.
            print(f"inbox-alert: cannot check the sender's tab: {exc}", file=sys.stderr)
            return Command((self.orca, "terminal", "switch", "--terminal", str(handle), *target)) if handle else None
        if not any(terminal.get("handle") == handle for terminal in terminals):
            # The sender's tab closed; its lane may have started a new agent in the same worktree.
            lane = [terminal["handle"] for terminal in terminals if terminal.get("agentIdentity")
                    and item.get("worktree") == Path(str(terminal.get("worktreePath") or "/")).name]
            if len(lane) != 1:
                return None
            handle = lane[0]
        # When Orca ships an orca:// link to a tab, return a Link built here instead.
        return Command((self.orca, "terminal", "switch", "--terminal", str(handle), *target))


@dataclass(frozen=True)
class AppleScript:
    """Plain notification; AppleScript cannot attach a click action."""

    def show(self, title: str, body: str, action: ClickAction | None) -> None:
        command = ["/usr/bin/osascript"]
        for line in APPLE_SCRIPT:
            command.extend(("-e", line))
        subprocess.run([*command, title, body], check=True, timeout=15,
                       capture_output=True, text=True)


@dataclass(frozen=True)
class TerminalNotifier:
    path: str

    def show(self, title: str, body: str, action: ClickAction | None) -> None:
        match action:
            case Command(argv):
                click: tuple[str, ...] = ("-execute", shlex.join(argv))
            case Link(url):
                click = ("-open", url)
            case None:
                click = ()
        subprocess.run([self.path, "-title", title, "-message", body, *click], check=True,
                       timeout=15, capture_output=True, text=True)


def notify(item: Item, tabs: SenderTabs, notifier: Notifier) -> None:
    lane = str(item.get("worktree") or item.get("handle") or "担当未記載").splitlines()[0]
    first_line = str(item.get("text") or "確認依頼が届きました").splitlines()[0]
    title, body = f"担当からの確認依頼: {lane[:80]}", first_line[:180]
    try:
        notifier.show(title, body, tabs.action(item))
    except subprocess.CalledProcessError as exc:
        # terminal-notifier exits non-zero until notifications are allowed for it.
        print(f"inbox-alert: {exc}: {exc.stderr or ''}".rstrip(), file=sys.stderr)
        AppleScript().show(title, body, None)
    print(f"ask alert: {lane}", flush=True)


def process(inbox: Path, cursor: Path, send: Callable[[dict[str, object]], None]) -> int:
    start = int(cursor.read_text().strip())
    with inbox.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if start > size:
            start = 0
        stream.seek(start)
        data = stream.read()
    end = complete_end(data)
    position = start
    for raw in data[:end].splitlines(keepends=True):
        try:
            item = json.loads(raw)
        except ValueError:
            item = None
        if isinstance(item, dict) and item.get("kind") == "ask":
            send(item)
        position += len(raw)
        temporary = cursor.with_name(cursor.name + ".tmp")
        temporary.write_text(str(position))
        temporary.replace(cursor)
    return position


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=Path.home() / ".cache/orca-briefs/triage-inbox.jsonl")
    parser.add_argument("--cursor", type=Path, default=Path.home() / ".cache/orca-session-triage/inbox-alert.offset")
    parser.add_argument("--once", action="store_true", help="read available records once, then exit")
    args = parser.parse_args(argv)
    tabs = OrcaTabs(shutil.which("orca") or "orca", socket.gethostname())
    path = shutil.which("terminal-notifier")
    notifier: Notifier = TerminalNotifier(path) if path else AppleScript()
    try:
        while True:
            process(args.inbox, args.cursor, partial(notify, tabs=tabs, notifier=notifier))
            if args.once:
                return 0
            time.sleep(2)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"inbox-alert: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
