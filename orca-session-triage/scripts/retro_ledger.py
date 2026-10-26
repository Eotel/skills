#!/usr/bin/env python3
"""Call the adoption-triage ledger's public CLI from the retrospective worker."""

import json
from pathlib import Path
import re
import subprocess
import sys


DEFAULT_SCRIPT = Path(__file__).resolve().parents[2] / "adoption-triage/scripts/ledger.py"
DEFAULT_DECISION_CONFIG = Path("~/.config/adoption-triage/hosts.json").expanduser()


class LedgerClient:
    def __init__(self, script=DEFAULT_SCRIPT, events=None, imports=None, decision_config=None):
        self.script = Path(script)
        self.events = Path(events) if events else None
        self.imports = Path(imports) if imports else None
        self.decision_config = Path(decision_config) if decision_config else None

    def call(self, action, body=None, *args):
        if not self.script.is_file():
            raise FileNotFoundError(f"adoption-triage ledger is missing: {self.script}")
        command = [sys.executable, str(self.script)]
        if self.events:
            command.extend(("--events", str(self.events)))
        if self.imports:
            command.extend(("--imports", str(self.imports)))
        if self.decision_config:
            command.extend(("--decision-config", str(self.decision_config)))
        command.extend((action, *args))
        result = subprocess.run(command, input=json.dumps(body, ensure_ascii=False) if body else None,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f"adoption-triage {action}: {result.stderr.strip()[-500:]}")
        return json.loads(result.stdout)

    def list(self):
        return self.call("list", None, "--json")["items"]

    def propose(self, body):
        return self.call("propose", body)

    def decide(self, body):
        return self.call("decide", body)

    def record(self, body):
        return self.call("record", body)

    def add_evidence(self, body):
        return self.call("support", body)

    def writer_enabled(self):
        config = self.decision_config or DEFAULT_DECISION_CONFIG
        try:
            values = json.loads(config.read_text(encoding="utf-8"))
            return (values.get("decision_writer") is True
                    and isinstance(values.get("name"), str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,39}", values["name"]) is not None)
        except (OSError, ValueError, AttributeError):
            return False
