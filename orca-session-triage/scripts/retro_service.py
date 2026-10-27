#!/usr/bin/env python3
"""Render, activate, and inspect the external triage retrospective launchd service."""

import argparse
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time

from retro_ledger import LedgerClient


LABEL = "com.eotel.orca-session-triage.retro"


def paths(home: str | Path | None = None) -> tuple[Path, Path]:
    home = Path(home or Path.home())
    return (home / "Library/LaunchAgents" / f"{LABEL}.plist",
            home / ".cache/orca-session-triage/retro/service.log")


def plist_bytes(
    home: str | Path | None = None,
    python: str | None = None,
    claude: str | None = None,
    script: str | Path | None = None,
) -> bytes:
    plist, log = paths(home)
    python = python or shutil.which("python3")
    claude = claude or shutil.which("claude")
    script = script or Path(__file__).with_name("retro.py")
    if not python or not claude or not Path(script).is_file():
        raise RuntimeError("python3, claude, and retro.py are required")
    service_path = ":".join(dict.fromkeys((str(Path(python).parent), str(Path(claude).parent),
                                           "/usr/bin", "/bin", "/usr/sbin", "/sbin")))
    environment = {"PATH": service_path}
    if os.environ.get("TRIAGE_RETRO_SKILLS_REPO"):
        environment["TRIAGE_RETRO_SKILLS_REPO"] = os.environ["TRIAGE_RETRO_SKILLS_REPO"]
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script), "run-service"],
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    })


def status() -> tuple[bool, str]:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode or not re.search(r"^\s*state = running\s*$", result.stdout, re.M):
        return False, "service is not running"
    return True, "external retrospective worker is running"


def activate() -> None:
    plist, log = paths()
    ledger = LedgerClient()
    if not ledger.script.is_file() or not ledger.writer_enabled():
        raise RuntimeError("adoption-triage must be installed and this Mac must be its decision writer")
    running, message = status()
    if running:
        raise RuntimeError(message)
    expected = plist_bytes()
    if plist.exists() and plist.read_bytes() != expected:
        raise RuntimeError(f"different plist already exists: {plist}")
    plist.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    if not plist.exists():
        with plist.open("xb") as output:
            output.write(expected)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                   check=True, timeout=20)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if status()[0]:
            return
        time.sleep(0.2)
    raise RuntimeError(f"service did not start; inspect {log}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("render", "status", "activate"))
    args = parser.parse_args(argv)
    try:
        if args.action == "render":
            sys.stdout.buffer.write(plist_bytes())
            return 0
        if args.action == "activate":
            activate()
        running, message = status()
        print(message)
        return 0 if running else 1
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"retro-service: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
