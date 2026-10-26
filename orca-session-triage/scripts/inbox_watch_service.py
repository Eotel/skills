#!/usr/bin/env python3
"""Manage the macOS user services that watch the triage inbox outside Orca tabs.

Use `render` to review its launchd plist, `status` to check the live watcher,
and `activate` only after the tab's Monitor has stopped. Activation refuses a
second reader of the existing triage offset.
`activate-alerts` adds an independent reader for Mac notifications on new asks.
"""

import argparse
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time

from inbox_read import complete_end


LABEL = "com.eotel.orca-session-triage.inbox"
ALERT_LABEL = "com.eotel.orca-session-triage.alert"


def paths(home=None):
    home = Path(home or Path.home())
    cache = home / ".cache/orca-briefs"
    return (home / "Library/LaunchAgents" / f"{LABEL}.plist",
            cache / "bin/watch-inbox",
            cache / "offsets/close-done-triage.offset",
            cache / "triage-watch.log")


def plist_bytes(home=None, python=None, orca=None):
    _, watch, offset, log = paths(home)
    python = python or shutil.which("python3")
    orca = orca or shutil.which("orca")
    if not python or not orca:
        raise RuntimeError("python3 and orca must both be on PATH when rendering the service")
    service_path = ":".join(dict.fromkeys((str(Path(orca).parent), str(Path(python).parent),
                                         "/usr/bin", "/bin", "/usr/sbin", "/sbin")))
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, str(watch), str(offset)],
        "EnvironmentVariables": {"PATH": service_path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    })


def alert_paths(home=None):
    home = Path(home or Path.home())
    return (home / "Library/LaunchAgents" / f"{ALERT_LABEL}.plist",
            home / ".cache/orca-briefs/triage-inbox.jsonl",
            home / ".cache/orca-session-triage/inbox-alert.offset",
            home / ".cache/orca-briefs/triage-alert.log")


def alert_plist_bytes(home=None, python=None):
    _, _, _, log = alert_paths(home)
    python = python or shutil.which("python3")
    if not python:
        raise RuntimeError("python3 must be on PATH when rendering the alert service")
    return plistlib.dumps({
        "Label": ALERT_LABEL,
        "ProgramArguments": [python, str(Path(__file__).with_name("inbox_alert.py"))],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    })


def launchd_pid(label=LABEL):
    target = f"gui/{os.getuid()}/{label}"
    result = subprocess.run(["launchctl", "print", target], capture_output=True,
                            text=True, timeout=10)
    if result.returncode or not re.search(r"^\s*state = running\s*$", result.stdout, re.MULTILINE):
        return None
    match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def uses_offset(command, offset):
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    for i, arg in enumerate(args[:-1]):
        if Path(arg).name == "watch-inbox":
            return os.path.realpath(os.path.expanduser(args[i + 1])) == os.path.realpath(offset)
    return False


def offset_readers(offset):
    result = subprocess.run(["ps", "-ww", "-axo", "pid=,command="], capture_output=True,
                            text=True, check=True, timeout=10)
    readers = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and uses_offset(parts[1], offset):
            readers.append(int(parts[0]))
    return readers


def status(home=None):
    _, _, offset, _ = paths(home)
    pid = launchd_pid()
    if pid is None:
        return False, "launchd service is not running"
    readers = offset_readers(offset)
    if readers != [pid]:
        return False, f"expected only launchd PID {pid} to read the triage offset; found {readers}"
    return True, f"watch-inbox running outside Orca tabs (PID {pid})"


def alert_status():
    pid = launchd_pid(ALERT_LABEL)
    return (True, f"ask alerts running (PID {pid})") if pid else (False, "ask alert service is not running")


def activate(home=None):
    plist_path, watch, offset, log = paths(home)
    if not watch.is_file() or not os.access(watch, os.X_OK):
        raise RuntimeError(f"watch-inbox is missing or not executable: {watch}")
    running, message = status(home)
    if running:
        raise RuntimeError(f"service is already active: {message}")
    readers = offset_readers(offset)
    if readers:
        raise RuntimeError(f"stop the existing watch-inbox reader of {offset} first (PID {', '.join(map(str, readers))})")
    expected = plist_bytes(home)
    if plist_path.exists() and plist_path.read_bytes() != expected:
        raise RuntimeError(f"different plist already exists: {plist_path}")
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    if not plist_path.exists():
        with plist_path.open("xb") as output:
            output.write(expected)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                   check=True, timeout=20)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if status(home)[0]:
            return
        time.sleep(0.2)
    raise RuntimeError("launchd accepted the service but watch-inbox did not start; inspect the log")


def activate_alerts(home=None):
    plist_path, inbox, cursor, log = alert_paths(home)
    if alert_status()[0]:
        raise RuntimeError("ask alert service is already active")
    expected = alert_plist_bytes(home)
    if plist_path.exists() and plist_path.read_bytes() != expected:
        raise RuntimeError(f"different plist already exists: {plist_path}")
    cursor.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    if not cursor.exists():
        with inbox.open("rb") as stream:
            data = stream.read()
        with cursor.open("x") as output:
            output.write(str(complete_end(data)))
    if not plist_path.exists():
        with plist_path.open("xb") as output:
            output.write(expected)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                   check=True, timeout=20)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if alert_status()[0]:
            return
        time.sleep(0.2)
    raise RuntimeError("launchd accepted the alert service but it did not start; inspect the log")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("render", "status", "activate", "render-alerts", "status-alerts", "activate-alerts"))
    args = parser.parse_args(argv)
    try:
        if args.action == "render":
            sys.stdout.buffer.write(plist_bytes())
            return 0
        if args.action == "render-alerts":
            sys.stdout.buffer.write(alert_plist_bytes())
            return 0
        if args.action == "activate":
            activate()
        if args.action == "activate-alerts":
            activate_alerts()
        running, message = alert_status() if args.action.endswith("alerts") else status()
        print(message)
        return 0 if running else 1
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"inbox-watch-service: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
