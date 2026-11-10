#!/usr/bin/env -S uv run --no-project --no-config --python 3.12
"""Drive a macOS desktop app built around a web view (Tauri, wry, WKWebView) behind the user's windows.

The app runs with two helper libraries loaded into it: one keeps it from coming to the front, the
other takes commands (keys, clicks, page script, pictures) from this script. No screen-recording,
accessibility, or input-posting rights are needed.

  launch  start an app binary with the helpers and wait until its page has loaded; prints JSON
          (compiles the helpers from scripts/desktop/ into --tools first when they are missing or old)
  send    give an app this script launched one command and print its result
  stop    end an app this script launched
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SOURCES = Path(__file__).resolve().parent / "desktop"
DEFAULT_TOOLS = Path.home() / ".cache" / "real-browser-verify" / "desktop"
# The helpers load before the app's own code runs, so a process without them by then never gets them.
LOAD_GRACE = 5
HELPERS = {
    "noactivate.dylib": ("noactivate.m", ("-dynamiclib", "-framework", "AppKit")),
    "drive.dylib": ("drive.m", ("-dynamiclib", "-framework", "AppKit", "-framework", "WebKit")),
    "send": ("send.m", ("-framework", "Foundation")),
}
MODIFIERS = ("cmd", "shift", "option", "control")
# macOS virtual key codes (ANSI layout). A letter or digit key types itself; the others type SPECIAL.
KEY_CODES = {
    **dict(zip("asdfhgzxcv", range(10))),
    **dict(zip("bqweryt", range(11, 18))),
    **dict(zip("1234659780", (18, 19, 20, 21, 22, 23, 25, 26, 28, 29))),
    **dict(zip("oui", (31, 32, 34))),
    **dict(zip("pljkn", (35, 37, 38, 40, 45))),
    "m": 46, "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
}
SPECIAL = {
    "return": "\r", "tab": "\t", "space": " ", "delete": "\x7f", "escape": "\x1b",
    "left": "\uf702", "right": "\uf703", "down": "\uf701", "up": "\uf700",
}
# Separates the fields of a command; tabs and newlines stay usable in page script and text.
SEPARATOR = "\x1f"


class DriveError(Exception):
    """A failure to report on stderr with exit code 1."""


@dataclass(frozen=True)
class Run:
    """An app this script launched, as recorded under <tools>/runs/<pid>.json."""

    pid: int
    binary: str
    drive_log: Path
    app_log: Path

    @staticmethod
    def record(tools: Path, pid: int) -> Path:
        return tools / "runs" / f"{pid}.json"

    @classmethod
    def load(cls, tools: Path, pid: int) -> Run:
        path = cls.record(tools, pid)
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            raise DriveError(f"no app with pid {pid} was launched by this script (no {path})") from None
        return cls(pid, data["binary"], Path(data["drive_log"]), Path(data["app_log"]))

    def save(self, tools: Path) -> None:
        path = self.record(tools, self.pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_json()))

    def as_json(self) -> dict[str, object]:
        return {"pid": self.pid, "binary": self.binary, "drive_log": str(self.drive_log), "app_log": str(self.app_log)}


def ensure_built(tools: Path) -> None:
    """Compile each helper that is missing or older than its source (as after an update of this skill)."""
    tools.mkdir(parents=True, exist_ok=True)
    for output, (source, flags) in HELPERS.items():
        target, origin = tools / output, SOURCES / source
        if target.exists() and target.stat().st_mtime >= origin.stat().st_mtime:
            continue
        command = ["clang", "-fobjc-arc", *flags, str(origin), "-o", str(target)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DriveError(f"could not build {output}:\n{result.stderr.strip()}")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def launch(tools: Path, binary: Path, args: list[str], env: list[tuple[str, str]], timeout: float) -> Run:
    if binary.is_dir():
        raise DriveError(f"{binary} is a folder; pass the executable inside it, such as <app>.app/Contents/MacOS/<name>")
    ensure_built(tools)
    logs = tools / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = uuid.uuid4().hex[:8]
    drive_log, app_log = logs / f"{binary.name}-{stamp}-drive.log", logs / f"{binary.name}-{stamp}-app.log"
    environment = {
        **os.environ,
        **dict(env),
        "DRIVE_LOG": str(drive_log),
        "DYLD_INSERT_LIBRARIES": f"{tools / 'noactivate.dylib'}:{tools / 'drive.dylib'}",
    }
    # Started straight from here: a SIP-protected wrapper (nohup, /bin/sh) would drop DYLD_*.
    with app_log.open("w") as out:
        process = subprocess.Popen(
            [str(binary), *args], env=environment, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    run = Run(process.pid, str(binary), drive_log, app_log)
    started = time.monotonic()
    while time.monotonic() < started + timeout:
        lines = drive_log.read_text().splitlines() if drive_log.exists() else []
        if f"ready {process.pid}" in lines:
            run.save(tools)
            return run
        if process.poll() is not None:
            raise DriveError(f"{binary} exited with {process.returncode}; its output:\n{tail(app_log)}")
        if f"loaded {process.pid}" not in lines and time.monotonic() > started + LOAD_GRACE:
            process.terminate()
            raise DriveError(
                f"{binary} did not load the helpers and was stopped. A binary signed with the hardened runtime (a "
                f"release build) ignores DYLD_INSERT_LIBRARIES; use a debug build. Its output:\n{tail(app_log)}"
            )
        time.sleep(0.05)
    process.terminate()
    raise DriveError(
        f"{binary} loaded the helpers, but its page did not finish loading within {timeout:g}s; it was stopped. "
        f"Its output:\n{tail(app_log)}"
    )


def tail(path: Path, lines: int = 20) -> str:
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:]) if path.exists() else "(none)"


def key_command(spec: str) -> list[str]:
    *mods, name = spec.lower().split("+")
    if name not in KEY_CODES or any(m not in MODIFIERS for m in mods):
        raise DriveError(
            f"unknown key {spec!r}: use [cmd+][shift+][option+][control+]<key>, key one of {', '.join(KEY_CODES)}"
        )
    return ["key", str(KEY_CODES[name]), SPECIAL.get(name, name), ",".join(mods) or "-"]


def send(tools: Path, pid: int, command: list[str], timeout: float) -> str:
    run = Run.load(tools, pid)
    if not alive(pid):
        raise DriveError(f"the app with pid {pid} is no longer running; its output:\n{tail(run.app_log)}")
    if any(SEPARATOR in part for part in command):
        raise DriveError("a command cannot contain the character U+001F")
    ident = uuid.uuid4().hex[:12]
    start = run.drive_log.stat().st_size
    subprocess.run([str(tools / "send"), SEPARATOR.join([str(pid), ident, *command])], check=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with run.drive_log.open(errors="replace") as log:
            log.seek(start)
            for line in log.read().splitlines():
                if line.startswith(ident + "\t"):
                    return line.split("\t", 1)[1]
        time.sleep(0.05)
    raise DriveError(f"no answer from pid {pid} within {timeout:g}s")


def stop(tools: Path, pid: int) -> None:
    Run.load(tools, pid)
    if alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    Run.record(tools, pid).unlink()


def environment_pair(value: str) -> tuple[str, str]:
    name, equals, setting = value.partition("=")
    if not name or not equals:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {value!r}")
    return name, setting


def seconds(value: str) -> float:
    number = float(value)
    if not number > 0:
        raise argparse.ArgumentTypeError(f"expected a number of seconds above 0, got {value!r}")
    return number


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    top.add_argument("--tools", type=Path, default=DEFAULT_TOOLS, help=f"helpers and run records (default {DEFAULT_TOOLS})")
    commands = top.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch", help="start an app binary behind the user's windows")
    start.add_argument("binary", type=Path, help="the app's executable (a debug build; not a .app folder)")
    start.add_argument("args", nargs="*", help="arguments for the app")
    start.add_argument(
        "--env", action="append", default=[], type=environment_pair, metavar="KEY=VALUE",
        help="extra environment, repeatable",
    )
    start.add_argument("--timeout", type=seconds, default=60, help="seconds to wait for the page to load")
    give = commands.add_parser("send", help="give a launched app one command and print its result")
    give.add_argument("pid", type=int)
    give.add_argument("--timeout", type=seconds, default=10, help="seconds to wait for the answer")
    actions = give.add_subparsers(dest="action", required=True)
    actions.add_parser("key", help="press a key, such as cmd+z, shift+tab, escape, r").add_argument("spec")
    actions.add_parser("text", help="type characters one key press each (no IME)").add_argument("text")
    point = actions.add_parser("click", help="left-click at page point X Y of the web view (CSS pixels)")
    point.add_argument("x", type=float)
    point.add_argument("y", type=float)
    point.add_argument("--count", type=int, default=1, help="2 for a double click")
    actions.add_parser("shot", help="save the web view as a PNG").add_argument("path", type=Path)
    actions.add_parser("js", help="run page script and print its result (for reading state)").add_argument("code")
    end = commands.add_parser("stop", help="end an app this script launched")
    end.add_argument("pid", type=int)
    return top


def action_command(options: argparse.Namespace) -> list[str]:
    if options.action == "key":
        return key_command(options.spec)
    if options.action == "text":
        return ["text", options.text]
    if options.action == "click":
        return ["click", f"{options.x:g}", f"{options.y:g}", str(options.count)]
    if options.action == "shot":
        return ["shot", str(options.path.resolve())]
    return ["js", options.code]


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    tools = options.tools.expanduser().resolve()
    try:
        if options.command == "launch":
            print(json.dumps(launch(tools, options.binary.resolve(), options.args, options.env, options.timeout).as_json()))
        elif options.command == "send":
            result = send(tools, options.pid, action_command(options), options.timeout)
            print(result)
            return 1 if result.startswith("error") else 0
        else:
            stop(tools, options.pid)
    except DriveError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
