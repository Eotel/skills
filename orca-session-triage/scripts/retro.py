#!/usr/bin/env python3
"""Queue hourly triage transcripts and turn verified lessons into reversible skill notes.

A live hourly tab reviews itself through a forked Agent and records the result with ``submit``.
The Stop hook calls ``enqueue`` only after its other checks allow the turn to end;
``process-once`` and ``run-service`` review a queued finished session outside Orca tabs
in a forked copy (``claude -p --resume --fork-session``) that leaves the original unchanged.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, NamedTuple, Protocol, TypedDict
import uuid

from retro_ledger import LedgerClient, DEFAULT_SCRIPT


STATE = Path("~/.cache/orca-session-triage/retro").expanduser()
SOURCE = Path(os.environ.get("TRIAGE_RETRO_SKILLS_REPO", "~/ghq/github.com/Eotel/skills")).expanduser()
NOTIFY = Path(os.environ.get("TRIAGE_NOTIFY", "~/.cache/orca-briefs/bin/triage-notify")).expanduser()
MAX_NIGHTLY_AUTO = 3
MAX_SKILL_BYTES = 12_000
MAX_NOTES_PER_SKILL = 3
MAX_NOTE_CHARS = 190
MAX_REJECTED = 20
# Adopted tool fixes and upstream reports go to a lane; the worker records the hand-off, never a note.
SEPARATE_WORK = frozenset(("script", "upstream"))
SESSION_ID = re.compile(r"^[a-f0-9-]{36}$")
MARKER = "<!-- triage-retro:"
FACTS_NAME = "observed-triage-facts.md"
FACTS_HEADER = "# Observed triage facts\n\n<!-- triage-retro-managed -->\n\n"
ROUTER_SECTION = ("\n## Observed triage facts\n\n"
                  "Read [references/observed-triage-facts.md](references/observed-triage-facts.md) "
                  "when troubleshooting a tool used in hourly triage.\n")
SCHEMA = {"type": "object", "required": ["proposals"], "properties": {
    "proposals": {"type": "array", "maxItems": 5, "items": {"type": "object", "required": [
        "title", "first_failure", "final_success", "note", "target_skill", "evidence_quotes", "category"],
        "properties": {
            "title": {"type": "string"}, "first_failure": {"type": "string"},
            "final_success": {"type": "string"}, "note": {"type": "string"},
            "target_skill": {"type": "string"},
            "evidence_quotes": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string", "enum": ["fact", "rule", "procedure", "script", "upstream"]},
        }}}
}}
REVIEW_SCHEMA = {"type": "object", "required": ["decisions"], "properties": {
    "decisions": {"type": "array", "items": {"type": "object", "required": [
        "index", "supported", "duplicate", "duplicate_of", "low_risk", "reason"], "properties": {
            "index": {"type": "integer"}, "supported": {"type": "boolean"},
            "duplicate": {"type": "boolean"}, "low_risk": {"type": "boolean"},
            "duplicate_of": {"type": "string"},
            "reason": {"type": "string"},
        }}}
}}
REWRITE_SCHEMA = {"type": "object", "required": ["note"], "properties": {"note": {"type": "string"}}}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def night_key(value):
    """Group the evening and following morning under one local adoption cap."""
    local = value.astimezone()
    return (local - timedelta(days=1) if local.hour < 12 else local).date().isoformat()


def paths(state=STATE):
    return state / "queue.jsonl", state / "worker-state.json", state / "lock"


@contextmanager
def locked(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def atomic_text(path, content, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".retro-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        Path(tmp).replace(path)
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink()


def load(state: Path = STATE) -> dict[str, Any]:
    state_file = paths(state)[1]
    if not state_file.exists():
        return {"offset": 0, "sessions": {}, "target_versions": {},
                "action_attempts": {}, "applied_actions": {}, "rewrites": {}}
    data = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(k in data for k in ("offset", "sessions")):
        raise ValueError(f"invalid worker state: {state_file}")
    data.setdefault("target_versions", {})
    data.setdefault("action_attempts", {})
    data.setdefault("applied_actions", {})
    data.setdefault("rewrites", {})
    return data


def save(data, state=STATE):
    atomic_text(paths(state)[1], json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def transcript_location(path):
    transcript = Path(path).resolve()
    projects = (Path.home() / ".claude/projects").resolve()
    return (transcript.is_relative_to(projects) and transcript.suffix == ".jsonl"
            and bool(SESSION_ID.fullmatch(transcript.stem)))


def hourly_transcript(path):
    """Accept only a real hourly transcript under Claude's local project store."""
    if not transcript_location(path):
        return False
    transcript = Path(path).resolve()
    if not transcript.is_file():
        return False
    # Claude may wrap command text in two user records; one is sufficient.
    try:
        with transcript.open("rb") as stream:
            for _, line in zip(range(250), stream):
                if b"<command-args>hourly" not in line:
                    continue
                row = json.loads(line)
                if row.get("type") != "user" or row.get("isSidechain"):
                    continue
                content = (row.get("message") or {}).get("content")
                if isinstance(content, str) and re.search(r"<command-args>hourly(?:\s|<)", content):
                    return True
    except (OSError, ValueError):
        return False
    return False


def enqueue(hook_input, state=STATE):
    """A tiny, idempotent Stop-hook write. Returns whether a new line was added."""
    transcript = hook_input.get("transcript_path")
    session_id = hook_input.get("session_id")
    if not transcript or not session_id or not SESSION_ID.fullmatch(session_id):
        return False
    if Path(transcript).stem != session_id or not hourly_transcript(transcript):
        return False
    queue = paths(state)[0]
    queue.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(queue, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        for line in stream:
            if line.rstrip(b"\n") == str(Path(transcript).resolve()).encode():
                return False
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
        stream.write((str(Path(transcript).resolve()) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    return True


def ingest(data, state=STATE):
    queue = paths(state)[0]
    if not queue.exists():
        return
    with queue.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        if os.fstat(stream.fileno()).st_size < data["offset"]:
            raise ValueError("queue is shorter than the saved offset")
        stream.seek(data["offset"])
        while line := stream.readline():
            if not line.endswith(b"\n"):
                break
            data["offset"] = stream.tell()
            transcript = line.decode("utf-8", errors="replace").strip()
            session_id = Path(transcript).stem
            if transcript_location(transcript) and (not Path(transcript).exists() or hourly_transcript(transcript)):
                data["sessions"].setdefault(session_id, {"session_id": session_id,
                                                         "transcript": transcript, "queued_at": now(),
                                                         "status": "pending", "attempts": 0})


class Ledger(Protocol):
    """The adoption-ledger calls the worker makes; ``LedgerClient`` is the real one."""
    def list(self) -> Sequence[dict[str, Any]]: ...
    def writer_enabled(self) -> bool: ...
    def propose(self, body: dict[str, Any]) -> object: ...
    def decide(self, body: dict[str, Any]) -> object: ...
    def record(self, body: dict[str, Any]) -> object: ...
    def add_evidence(self, body: dict[str, Any]) -> object: ...


class Event(TypedDict):
    """One visible part of a transcript row, with an ID stable for that transcript."""
    id: str
    at: str
    role: str
    text: str


class ForkTarget(NamedTuple):
    """Where and how to resume a finished session as a forked copy."""
    session_id: str
    cwd: Path
    model: str
    prior_cost: float  # The original's cost; claude -p adds it to the copy's reported total.


def transcript_rows(transcript: Path) -> Iterator[dict[str, Any]]:
    """JSON rows of a Claude transcript; a line cut off mid-write is skipped."""
    with transcript.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # A tool result holds a string or its own text blocks; both must match a verbatim quote.
        return "\n".join(part.get("text", "") if part.get("type") == "text" else content_text(part.get("content", ""))
                         for part in content if isinstance(part, dict) and part.get("type") in ("text", "tool_result"))
    return ""


def tool_text(part: dict[str, Any]) -> str:
    arguments = part.get("input") or {}
    command = arguments.get("command") if isinstance(arguments, dict) else ""
    return f"{part.get('name', 'tool')}: {command or json.dumps(arguments, ensure_ascii=False)[:500]}"


def row_parts(row: dict[str, Any]) -> list[tuple[str, str]]:
    """The (role, text) parts of one user or assistant row that the conversation showed."""
    content = (row.get("message") or {}).get("content")
    if row.get("type") == "user":
        return [("user", content)] if isinstance(content, str) else [("result", content_text(content))]
    if row.get("type") != "assistant" or not isinstance(content, list):
        return []
    return [("assistant", part.get("text", "")) if part.get("type") == "text" else ("tool", tool_text(part))
            for part in content if isinstance(part, dict) and part.get("type") in ("text", "tool_use")]


def extract_events(transcript: Path | str) -> list[Event]:
    """Visible turns, commands, and tool results of the main chain, with stable timestamped IDs."""
    parts = [(row.get("timestamp", ""), role, re.sub(r"\s+", " ", str(text)).strip())
             for row in transcript_rows(Path(transcript)) if not row.get("isSidechain")
             for role, text in row_parts(row)]
    return [Event(id=f"E{index:05d}", at=at, role=role, text=text)
            for index, (at, role, text) in enumerate((part for part in parts if part[2]), start=1)]


def first_containing(events: Sequence[Event], needle: str) -> Event | None:
    return next((event for event in events if needle in event["text"]), None)


def around(event: Event, needle: str) -> Event:
    start = max(0, event["text"].index(needle) - 100)
    return Event(id=event["id"], at=event["at"], role=event["role"], text=event["text"][start:start + 750])


def quoted_events(quotes: Sequence[object], events: Sequence[Event]) -> list[Event] | None:
    """Events holding the copy's verbatim quotes, cut around each quote; None below two events.

    A paraphrase cannot be traced and is dropped; the separate review judges what remains.
    """
    needles = [needle for needle in (re.sub(r"\s+", " ", str(quote)).strip() for quote in quotes[:6])
               if len(needle) >= 8]
    hits = [(event, needle) for needle in needles if (event := first_containing(events, needle))]
    # The first quote found in an event decides its excerpt.
    found = {event["id"]: around(event, needle) for event, needle in reversed(hits)}
    return sorted(found.values(), key=lambda event: event["id"]) if len(found) >= 2 else None


class Deployment(NamedTuple):
    """Paths APM copied from one source skill, relative to the directory holding ``apm.lock.yaml``."""
    repo: Path
    source: Path
    copies: frozenset[str]


def apm_deployments(base: Path) -> list[Deployment]:
    """The GitHub dependencies in ``base/apm.lock.yaml``, each with its local ghq source checkout."""
    lock = base / "apm.lock.yaml"
    dependencies = lock.read_text(encoding="utf-8").split("\ndeployments:", 1)[0] if lock.is_file() else ""
    blocks = [(dict(re.findall(r"^ *(repo_url|host|virtual_path): (\S+)$", block, re.M)),
               frozenset(re.findall(r"^  - (\S+)$", block, re.M)))
              for block in re.split(r"^- ", dependencies, flags=re.M)[1:]]
    return [Deployment(repo, repo / fields.get("virtual_path", ""), copies)
            for fields, copies in blocks
            if "repo_url" in fields and fields.get("host", "github.com") == "github.com"
            for repo in [Path.home() / "ghq/github.com" / fields["repo_url"]]]


def apm_source(path: Path, deployments: Sequence[Deployment]) -> Path:
    """The source of a repository-local ``SKILL.md`` that APM deployed; otherwise ``path`` itself."""
    repo, copy = path.parents[3].resolve(), path.parent.resolve()
    relative = copy.relative_to(repo).as_posix() if copy.is_relative_to(repo) else None
    return next((deployment.source / "SKILL.md" for deployment in deployments if relative in deployment.copies), path)


def skill_catalog(root: Path = SOURCE) -> dict[str, Path]:
    """Source skills: Eotel/skills, repository-local skills, and the APM sources of deployed copies.

    A skill APM deployed into a repository maps to its source checkout, so an adoption never edits the copy.
    """
    checkouts = Path.home() / "ghq/github.com"
    local = [path for pattern in ("*/*/.claude/skills/*/SKILL.md", "*/*/.agents/skills/*/SKILL.md")
             for path in sorted(checkouts.glob(pattern)) if path.parents[3].resolve() != root.resolve()]
    deployed = {repo: apm_deployments(repo) for repo in {path.parents[3] for path in local}}
    # The lock may spell a repository in another case; samefile still recognizes Eotel/skills itself.
    installed = [deployment.source / "SKILL.md" for deployment in apm_deployments(Path.home() / ".apm")
                 if (deployment.source / "SKILL.md").is_file()
                 and not (root.is_dir() and deployment.repo.samefile(root))]
    return ({path.parent.name: path for path in sorted(root.glob("*/SKILL.md"))}
            | {"/".join((*path.relative_to(checkouts).parts[:2], path.parent.name)): path for path in installed}
            | {f"{path.parents[4].name}/{path.parents[3].name}/{path.parent.name}":
               apm_source(path, deployed[path.parents[3]]) for path in local})


def skill_target(name: str, root: Path = SOURCE) -> Path | None:
    target = skill_catalog(root).get(name)
    if target is None or not target.is_file():
        return None
    checkouts = Path.home() / "ghq/github.com"
    expected = checkouts.joinpath(*target.relative_to(checkouts).parts[:2]) if "/" in name else root
    return target if target.resolve().is_relative_to(expected.resolve()) else None


def skill_description(path: Path) -> str | None:
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return None
    description = re.search(r"^description:\s*(.*)$", body, re.M)
    return description.group(1)[:180] if description else ""


def skill_index(root: Path = SOURCE, mentioned: str | None = None) -> str:
    """Describe candidate skills; with ``mentioned``, only repositories named in that text."""
    described = [(name, skill_description(path)) for name, path in skill_catalog(root).items()
                 if mentioned is None or "/" not in name or name.split("/")[1].casefold() in mentioned]
    return "\n".join(f"{name}: {description}" for name, description in described if description is not None)


def claude_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.setdefault("USER", pwd.getpwuid(os.getuid()).pw_name)
    selected = Path.home() / "Library/Application Support/orca/claude-profiles/selected-host"
    if selected.is_file():
        profile = selected.read_text(encoding="utf-8").strip()
        if profile:
            env["CLAUDE_CONFIG_DIR"] = profile
    return env


def run_claude(prompt: str, schema: dict[str, Any], claude: str = "claude",
               resume: ForkTarget | None = None) -> dict[str, Any]:
    """One structured ``claude -p`` answer, from a forked copy of a finished session when ``resume`` is set."""
    # The copy gets a new ID and is not saved, so the original transcript stays as it was.
    # Hooks are off: the hourly Stop hook would queue or block the copy as another hourly run.
    # No budget cap: the resumed history's earlier cost counts toward it.
    mode = (["--resume", resume.session_id, "--fork-session", "--model", resume.model,
             "--settings", json.dumps({"disableAllHooks": True})] if resume
            else ["--model", "sonnet", "--max-budget-usd", "1"])
    cmd = [claude, "-p", "--no-session-persistence", "--tools", "", "--strict-mcp-config",
           "--output-format", "json", "--json-schema", json.dumps(schema), *mode]
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600,
                            cwd=resume.cwd if resume else None, env=claude_environment())
    try:
        wrapper = json.loads(result.stdout)
    except ValueError:
        wrapper = None
    if result.returncode or not isinstance(wrapper, dict) or wrapper.get("is_error"):
        # Claude reports authentication and API failures in stdout JSON, often with an empty stderr.
        detail = (f"is_error={wrapper.get('is_error')} subtype={wrapper.get('subtype')} "
                  f"result={str(wrapper.get('result'))[:300]}" if isinstance(wrapper, dict)
                  else f"stdout={result.stdout[-300:]!r}")
        raise RuntimeError(f"claude -p exited {result.returncode}: {detail} stderr={result.stderr[-200:]!r}")
    usage = wrapper.get("usage") or {}
    tokens = {key: usage.get(key) for key in ("cache_creation_input_tokens", "cache_read_input_tokens",
                                              "input_tokens", "output_tokens")}
    total = wrapper.get("total_cost_usd")
    cost = total - resume.prior_cost if resume and isinstance(total, (int, float)) else total
    print(f"claude -p{' fork' if resume else ''} cost_usd={cost if cost is not None else 'unknown'} "
          f"tokens={json.dumps(tokens)}", file=sys.stderr, flush=True)
    output = wrapper.get("structured_output")
    if not isinstance(output, dict):
        raise ValueError(f"Claude did not return structured_output: {str(wrapper.get('result'))[:300]}")
    return output


def nearby_knowledge(note, root=SOURCE, target_skill=None):
    """Retrieve related skill, memory, and ledger passages for the independent review."""
    files = list(skill_catalog(root).values())
    target = skill_target(target_skill, root) if target_skill else None
    references = set((target.parent / "references").rglob("*.md")) if target else set()
    files.extend(references)
    files += list((Path.home() / ".claude/projects").glob("*/memory/MEMORY.md"))
    files += [Path.home() / ".claude/CLAUDE.md"]
    grams = {note[i:i+3] for i in range(max(0, len(note)-2)) if note[i:i+3].strip()}
    scored = []
    for path in files:
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for section in re.split(r"\n(?=#|\n)", body):
            if not section.strip():
                continue
            overlap = sum(1 for gram in grams if gram in section)
            if overlap:
                scored.append((overlap + (100 if path in references else 0), str(path), section[:1200]))
    scored.sort(reverse=True)
    return "\n".join(f"{path}: {section}" for _, path, section in scored[:12])[:15_000]


RETRO_PROMPT = (
    "ここまでの定時 triage の回を振り返る。あなたはこの会話の文脈を引き継いだ複製で、元の会話の記録は変わらない。"
    "最初の失敗と、後で実際に成功した対を最大5件挙げる。成功が会話で確認できないもの、一度きりで繰り返さないものは挙げない。"
    "note は190字以内の一行にする。fact なら既存 skill に追記できる具体的な道具・パス・エラー対処だけを書き、"
    "script・upstream なら直す内容を書く。"
    "道具の使いにくさから出た教訓は、使い方として書かず道具を直す案にする。自分たちの道具"
    "（下の skill の scripts、~/.cache/orca-briefs/bin、orca-board）なら、引数・option を足すか出力を直す案を"
    "category script で出す。直せない外の道具（gh・bee など）なら上流への issue の案を category upstream で出し、"
    "直るまでの使い方は別に category fact で出してよい。"
    "手順や決まりの案でも、道具の機能（引数・既定値・検査・出力）で間違いを決定論的に防げるなら、"
    "手順を書き足さず category script で道具を直す案にする。"
    "利用者の決まり、権限、削除・merge・投稿の条件、手順やスクリプトの変更は category を fact 以外にする。"
    "target_skill は下の skill 名から選ぶ。repo/project 固有 skill も候補にしてよい。"
    "evidence_quotes には失敗と成功のそれぞれについて、会話中の道具の出力・実行したコマンド・発言から"
    "20〜200字を一字一句そのまま引用する。要約や言い換えは照合できず捨てられる。")


def transcript_text(transcript: Path) -> str:
    return transcript.read_text(encoding="utf-8", errors="replace").casefold()


def decision_comment(item: dict[str, Any]) -> str:
    """The deciding person's comment on the latest decision; the worker's own reasons are not comments."""
    decision = (item.get("decisions") or [{}])[-1]
    return "" if decision.get("actor") == "triage-retro" else str(decision.get("reason") or "").strip()


def rejected_lessons(items: Sequence[dict[str, Any]]) -> str:
    """The newest rejected proposals that came with a comment, for the next retrospective to avoid."""
    rejected = sorted((item for item in items if item.get("status") == "rejected" and decision_comment(item)),
                      key=lambda item: item["rejected_at"], reverse=True)[:MAX_REJECTED]
    return "\n".join(f"- 案: {str(item['proposal'])[:300]} / 却下の理由: {decision_comment(item)[:300]}"
                     for item in rejected)


def fork_prompt(transcript: Path, root: Path = SOURCE, rejected: str = "") -> str:
    return (RETRO_PROMPT + "\n\nSKILLS\n" + skill_index(root, transcript_text(transcript))
            + (f"\n\n却下された案と理由（同じ案や言い換えた案は出さない）\n{rejected}" if rejected else ""))


def fork_target(transcript: Path) -> ForkTarget:
    """The session's worktree, its last main-chain model, and its recorded cost."""
    rows = list(transcript_rows(transcript))
    models = [row["message"]["model"] for row in rows
              if row.get("type") == "assistant" and not row.get("isSidechain")
              and str((row.get("message") or {}).get("model", "")).startswith("claude-")]
    costs = [row["totalCostUSD"] for row in rows
             if row.get("type") == "cost-state" and isinstance(row.get("totalCostUSD"), (int, float))]
    if not models:
        raise ValueError("hourly transcript has no model")
    return ForkTarget(transcript.stem, Path(transcript_worktree(transcript)), models[-1],
                      costs[-1] if costs else 0.0)


def build_proposals(transcript: Path, root: Path = SOURCE, claude: str = "claude",
                    existing: Sequence[dict[str, Any]] = (), rejected: str = "") -> list[dict[str, Any]]:
    """Ask a forked copy of the finished session for lessons, then review them independently."""
    if not root.is_dir():
        raise FileNotFoundError(f"skill source checkout is missing: {root}")
    output = run_claude(fork_prompt(transcript, root, rejected), SCHEMA, claude, resume=fork_target(transcript))
    return review_proposals(output.get("proposals") or [], transcript, root, claude, existing)


def omit(title: str, reason: str) -> None:
    """Omitted lessons are not ledger rows; the log keeps what was dropped and why."""
    print(f"omitted: {title[:100]}: {reason[:300]}", file=sys.stderr, flush=True)


def is_time(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def candidate_from(item: dict[str, Any], events: Sequence[Event], root: Path,
                   mentioned: str) -> tuple[dict[str, Any] | None, str]:
    """A review candidate built from one proposal, or None and the reason it is omitted."""
    evidence = quoted_events(item.get("evidence_quotes") or [], events)
    target = str(item.get("target_skill", ""))
    note = str(item.get("note", "")).strip()
    if evidence is None:
        return None, "quotes not found in the transcript"
    if not all(is_time(event["at"]) for event in evidence):
        return None, "evidence has no timestamp"
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9][A-Za-z0-9-]*){0,2}", target)
            or skill_target(target, root) is None
            or ("/" in target and target.split("/")[1].casefold() not in mentioned)):
        return None, f"unknown target skill {target}"
    if not note or "\n" in note or len(note) > 500:
        return None, "note is empty, multi-line, or too long"
    return {"title": str(item.get("title", ""))[:100],
            "first_failure": str(item.get("first_failure", ""))[:500],
            "final_success": str(item.get("final_success", ""))[:500],
            "note": note, "target_skill": target, "category": item.get("category"),
            "evidence": evidence}, ""


def review_proposals(raw: Sequence[object], transcript: Path, root: Path = SOURCE, claude: str = "claude",
                     existing: Sequence[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Keep proposals whose quotes are found in the transcript and pass a separate review."""
    events = extract_events(transcript)
    mentioned = transcript_text(transcript)
    checked = [(item, *candidate_from(item, events, root, mentioned))
               for item in raw[:5] if isinstance(item, dict)]
    for item, candidate, reason in checked:
        if candidate is None:
            omit(str(item.get("title", "")), reason)
    candidates = [candidate for _, candidate, _ in checked if candidate is not None]
    if not candidates:
        return []
    knowledge = "\n\n".join(
        f"RELATED FILE PASSAGES FOR CANDIDATE INDEX {i} (not ledger proposals):\n"
        f"{nearby_knowledge(c['note'], root, c['target_skill'])}"
        for i, c in enumerate(candidates))
    for candidate in candidates:
        target_path = skill_target(candidate["target_skill"], root)
        if target_path is None:
            continue
        knowledge += f"\n\nTARGET {target_path}: {target_path.read_text(encoding='utf-8')[:15000]}"
        repository = target_path.parent.parent.parent.parent if ".claude" in target_path.parts or ".agents" in target_path.parts else root
        for instruction in (repository / "AGENTS.md", repository / "CLAUDE.md"):
            if instruction.is_file():
                knowledge += f"\n\n{instruction}: {instruction.read_text(encoding='utf-8')[:8000]}"
    knowledge += "\n\nEXISTING LEDGER PROPOSALS\n" + "\n".join(
        f"{p.get('id', '')} ({p.get('target_skill', '')}): {p.get('note', '')}" for p in existing)
    review_prompt = ("あなたは別の文脈の検証役。会話の抜粋だけから、各提案の失敗→成功の実証、"
                     "既存知識との重複、低リスクの事実追記かを厳しく判定する。"
                     "低リスクは道具の使い方・パス・エラー対処の事実のみ。"
                     "人の決まり、権限、削除・merge・投稿、手順やスクリプトの動作変更は low_risk=false。"
                     "既存ファイルや台帳に同じ教訓があれば duplicate=true。"
                     "duplicate_of には上の EXISTING LEDGER PROPOSALS にある ID だけを書く。"
                     "既存ファイルだけで重複する場合と重複しない場合は空文字。"
                     "RELATED FILE PASSAGES の候補番号は台帳 ID ではない。"
                     "抜粋に成功が無ければ supported=false。\n\nCANDIDATES\n"
                     + json.dumps(candidates, ensure_ascii=False) + "\n\nEXISTING KNOWLEDGE\n" + knowledge)
    decisions = run_claude(review_prompt, REVIEW_SCHEMA, claude).get("decisions") or []
    reviewed = {d.get("index"): d for d in decisions if isinstance(d, dict)}
    for i, candidate in enumerate(candidates):
        decision = reviewed.get(i, {})
        candidate["review"] = {k: decision.get(k) for k in
                               ("supported", "duplicate", "duplicate_of", "low_risk", "reason")}
        if candidate["review"]["duplicate"] is True:
            target_id = candidate["review"].get("duplicate_of")
            if any(p.get("id") == target_id and p.get("target_skill") == candidate["target_skill"]
                   for p in existing):
                candidate["duplicate_of"] = target_id
    kept = [c for c in candidates if c["review"]["supported"] is True
            and (c["review"]["duplicate"] is False or c.get("duplicate_of"))]
    for candidate in candidates:
        if candidate not in kept:
            review = candidate["review"]
            omit(candidate["title"], f"review supported={review['supported']} "
                                     f"duplicate={review['duplicate']}: {review['reason']}")
    return kept


def proposal_id(session_id, candidate):
    # The same lesson from later hourly runs must add evidence to one ledger row.
    normalized = re.sub(r"\s+", "", candidate["note"]).casefold()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "triage-retro:" + candidate["target_skill"] + ":" + normalized))


def facts_path(target):
    return target.parent / "references" / FACTS_NAME


def source_checkout_ready(target, data):
    checkout = subprocess.run(["git", "-C", str(target.parent), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10)
    if checkout.returncode:
        return "書き込み先の Git repository を確認できない"
    repo = Path(checkout.stdout.strip())
    branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                            capture_output=True, text=True, timeout=10).stdout.strip()
    if branch not in ("main", "master"):
        return "書き込み先が main 以外"
    for path in (target, facts_path(target)):
        resolved = path.resolve()
        if not resolved.is_relative_to(repo.resolve()):
            return "書き込み先が Git repository の外"
        relative = str(resolved.relative_to(repo.resolve()))
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", relative],
                                capture_output=True, text=True, timeout=10)
        if status.returncode:
            return "書き込み先の Git 状態を確認できない"
        if path.exists() and not path.is_file():
            return "書き込み先がファイルではない"
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        versions = data.get("target_versions", {})
        if status.stdout.strip() and (str(resolved) not in versions or versions[str(resolved)] != digest):
            return "書き込み先に別の変更がある"
    return None


def remember_versions(data, *targets):
    for target in targets:
        key = str(target.resolve())
        if target.exists():
            data["target_versions"][key] = hashlib.sha256(target.read_bytes()).hexdigest()
        else:
            data["target_versions"][key] = None


def own_tools(root: Path = SOURCE) -> frozenset[str]:
    """File names of the tools this setup maintains; a lesson about one becomes a fix to it, not a usage note."""
    tools = (*root.glob("*/scripts/*"), *(Path.home() / ".cache/orca-briefs/bin").glob("*"))
    return frozenset(path.name for path in tools
                     if path.is_file() and re.search(r"[._-]", path.name)
                     and not re.search(r"\.bak|backup", path.name) and path.suffix != ".md")


def eligible_auto(proposal: dict[str, Any], data: dict[str, Any], ledger_items: Sequence[dict[str, Any]] = (),
                  root: Path = SOURCE) -> tuple[bool, str]:
    review = proposal.get("review") or {}
    note = proposal["note"]
    target = skill_target(proposal["target_skill"], root)
    if (proposal.get("category") != "fact" or review.get("supported") is not True
            or review.get("duplicate") is not False or review.get("low_risk") is not True):
        return False, "要確認"
    if any(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", note) for name in own_tools(root)):
        return False, "自分たちの道具は直す案にする"
    if target is None or not target.is_file():
        return False, "書き込み先が未設定"
    if (len(note) > MAX_NOTE_CHARS or "\n" in note or MARKER in note
            or not re.search(r"`[^`]+`|\.(?:py|json|ya?ml|toml)\b|Selected model|エラー|失敗|パス|コマンド", note)):
        return False, "追記文の形式を確認"
    if re.search(r"権限|許可|削除|merge|マージ|投稿|承認|無視|skip|ignore|force", note, re.I):
        return False, "人の判断が必要な条件"
    source_reason = source_checkout_ready(target, data)
    if source_reason:
        return False, source_reason
    router_body = target.read_text(encoding="utf-8")
    facts = facts_path(target)
    body = facts.read_text(encoding="utf-8") if facts.exists() else FACTS_HEADER
    if note in router_body or note in body:
        return False, "skill に同じ事実がある"
    new_line = note_line({"id": proposal.get("id", "0" * 36), "note": note})
    growth = ("" if body.endswith("\n") else "\n") + new_line
    router_growth = ("" if router_body.endswith("\n") else "\n") + ROUTER_SECTION
    if (len(router_body.encode()) + (0 if ROUTER_SECTION in router_body else len(router_growth.encode()))
            > MAX_SKILL_BYTES or len(body.encode()) + len(growth.encode()) > MAX_SKILL_BYTES
            or body.count(MARKER) >= MAX_NOTES_PER_SKILL):
        return False, "skill を短く書き直す提案が必要"
    tonight = night_key(datetime.now().astimezone())
    count = sum(1 for p in ledger_items for decision in p.get("decisions", [])
                if decision.get("actor") == "triage-retro" and decision.get("decision") == "adopt"
                and night_key(datetime.fromisoformat(decision["ts"])) == tonight)
    if count >= MAX_NIGHTLY_AUTO:
        return False, "今夜の自動採用上限"
    return True, ""


def note_line(proposal):
    return f"- {proposal['note']} {MARKER}{proposal['id']} -->\n"


def adopt(proposal, root=SOURCE, auto=False):
    router = skill_target(proposal["target_skill"], root)
    if router is None or not router.is_file():
        raise ValueError("target skill is not in the configured source repository")
    target = facts_path(router)
    if not target.resolve().is_relative_to(router.parent.resolve()):
        raise ValueError("facts reference is outside the skill directory")
    line = note_line(proposal)
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
        if proposal["note"] in existing and existing.count(line) != 1:
            raise ValueError("note is already present in target skill")
    else:
        existing = ""
    initial = FACTS_HEADER if not existing else existing
    addition = ("" if initial.endswith("\n") else "\n") + line
    if auto and (len(initial.encode()) + len(addition.encode()) > MAX_SKILL_BYTES
                 or existing.count(MARKER) >= MAX_NOTES_PER_SKILL):
        raise ValueError("skill size cap reached")
    target.parent.mkdir(parents=True, exist_ok=True)
    with router.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        body = stream.read().decode("utf-8")
        if proposal["note"] in body:
            raise ValueError("note is already present in target skill")
        if ROUTER_SECTION not in body:
            stream.write((("" if body.endswith("\n") else "\n") + ROUTER_SECTION).encode())
            stream.flush()
            os.fsync(stream.fileno())
    with target.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        body = stream.read().decode("utf-8")
        if MARKER + proposal["id"] in body:
            if body.count(line) == 1:
                return target.resolve()
            raise ValueError("adopted line changed; review target before retry")
        if proposal["note"] in body:
            raise ValueError("note is already present in target skill")
        initial = FACTS_HEADER if not body else body
        addition = ("" if initial.endswith("\n") else "\n") + line
        if auto and (len(initial.encode()) + len(addition.encode()) > MAX_SKILL_BYTES
                     or body.count(MARKER) >= MAX_NOTES_PER_SKILL):
            raise ValueError("skill size cap reached")
        stream.write(((FACTS_HEADER if not body else "") + addition).encode())
        stream.flush()
        os.fsync(stream.fileno())
    return target.resolve()


def undo(proposal):
    target = Path(proposal.get("target_path") or "")
    router = Path(proposal.get("router_path") or "")
    changed = False
    if target.is_file():
        with target.open("r+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            body = stream.read().decode("utf-8")
            line = note_line(proposal)
            if line not in body and MARKER + proposal["id"] in body:
                raise ValueError("adopted line changed; review target before undo")
            if line in body:
                if body.count(line) != 1:
                    raise ValueError("adopted line changed; review target before undo")
                body = body.replace(line, "", 1)
                stream.seek(0)
                stream.write(body.encode())
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                changed = True
            if body == FACTS_HEADER:
                target.unlink()
    if not target.exists() and router.is_file():
        with router.open("r+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            body = stream.read().decode("utf-8")
            if ROUTER_SECTION in body:
                stream.seek(0)
                stream.write(body.replace(ROUTER_SECTION, "", 1).encode())
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                changed = True
    return changed


def transcript_worktree(transcript):
    with Path(transcript).open(encoding="utf-8") as stream:
        for _, line in zip(range(300), stream):
            if '"cwd"' not in line or '"type":"user"' not in line.replace(" ", ""):
                continue
            row = json.loads(line)
            if isinstance(row.get("cwd"), str) and row["cwd"]:
                return row["cwd"]
    raise ValueError("hourly transcript has no worktree path")


def evidence_lines(candidate, session_id):
    lines = []
    for event in candidate["evidence"][:100]:
        timestamp = datetime.fromisoformat(event["at"]).astimezone().isoformat(timespec="minutes")
        lines.append(f"{session_id} {timestamp} {event['id']} {event['role']}: {event['text'][:750]}")
    return lines


def ledger_payload(candidate, sid, transcript, auto_reason):
    reason = str(candidate["review"].get("reason") or "")
    if auto_reason:
        reason += f" 自動採用しない理由: {auto_reason}。"
    return {"proposal_id": candidate["id"],
            "source": {"session": sid, "worktree": transcript_worktree(transcript)},
            "fact": f"{candidate['first_failure']}\n{candidate['final_success']}",
            "proposal": candidate["note"],
            "reason": (reason or "根拠を確認済み")[:3000],
            "recommendation": "adopt", "priority": "normal",
            "evidence": evidence_lines(candidate, sid),
            "target": f"triage-retro:skill:{candidate['target_skill']}:{candidate['category']}"}


def as_existing(items):
    result = []
    for item in items:
        target = item.get("target") or ""
        if target.startswith("triage-retro:skill:"):
            parts = target.split(":", 3)
            if len(parts) == 4:
                result.append({"id": item["proposal_id"], "note": item["proposal"],
                               "target_skill": parts[2]})
    return result


def rewritten_note(item: dict[str, Any], comment: str, claude: str = "claude") -> str:
    """The adopted note rewritten as the deciding person's comment asks."""
    evidence = "\n".join(map(str, item.get("evidence") or []))[:6000]
    prompt = ("採用された振り返りの追記文を、採用した人のコメントに合わせて書き直す。"
              "コメントが文の直しを求めていなければ、元の文をそのまま返す。根拠に無い事実を足さない。"
              f"改行を入れず、{MAX_NOTE_CHARS}字以内の一行にする。\n\n"
              f"元の文\n{item['proposal']}\n\nコメント\n{comment}\n\n根拠\n{item.get('fact') or ''}\n{evidence}")
    note = str(run_claude(prompt, REWRITE_SCHEMA, claude).get("note") or "").strip()
    if not note or "\n" in note or MARKER in note or len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"rewritten note is not one line of {MAX_NOTE_CHARS} characters or fewer: {note[:200]}")
    return note


def applied_note(item: dict[str, Any], data: dict[str, Any]) -> str:
    """The note the last application wrote, which a comment may have rewritten from the proposal."""
    receipts = [json.loads(execution["undo"]) for execution in item.get("executions", [])
                if execution.get("outcome") == "applied" and execution.get("undo")]
    token = receipts[-1] if receipts else data["applied_actions"].get(item["proposal_id"]) or {}
    line, suffix = str(token.get("line") or ""), f" {MARKER}{item['proposal_id']} -->\n"
    return line[2:-len(suffix)] if line.startswith("- ") and line.endswith(suffix) else item["proposal"]


def sync_decisions(ledger: Ledger, data: dict[str, Any], root: Path = SOURCE, state: Path | None = None) -> int:
    """Reconcile decisions; callers hold the worker and state locks when state is set."""
    changed = 0
    for item in ledger.list():
        target = item.get("target") or ""
        if not target.startswith("triage-retro:skill:"):
            continue
        parts = target.split(":", 3)
        if len(parts) != 4:
            continue
        _, _, skill, category = parts
        status = item["status"]
        proposal_id = item["proposal_id"]
        if status not in ("adopted", "revoked"):
            continue
        decision_id = item["decisions"][-1]["id"]
        if ((status == "adopted" and item.get("implemented_at"))
                or (status == "revoked" and item.get("reverted_at"))):
            data["applied_actions"].pop(proposal_id, None)
            data["rewrites"].pop(decision_id, None)
            continue
        action_key = f"{proposal_id}:{status}:{item.get(status + '_at')}"
        if category in SEPARATE_WORK:
            # A lane makes the tool fix or the upstream report; the board or the deciding person tells the lead.
            if status == "adopted" and not any(execution.get("outcome") == "handed_off"
                                               and execution.get("decision_id") == decision_id
                                               for execution in item.get("executions", [])):
                ledger.record({"proposal_id": proposal_id, "decision_id": decision_id, "outcome": "handed_off",
                               "actor": "triage-retro",
                               "result": "worker は skill に書かず、担当へ渡した。担当が実施したら applied を記録する。"})
                data["action_attempts"].pop(action_key, None)
                changed += 1
            continue
        if data["action_attempts"].get(action_key, 0) >= 3 and proposal_id not in data["applied_actions"]:
            continue
        proposal = {"id": proposal_id, "note": item["proposal"], "target_skill": skill}
        writing_completed = False
        try:
            router = skill_target(skill, root)
            if router is None:
                raise ValueError("target skill is missing")
            reference = facts_path(router)
            comment = decision_comment(item)
            if status == "adopted" and comment:
                # One rewrite per decision, kept so that a retry writes the same line.
                proposal["note"] = data["rewrites"][decision_id] = (
                    data["rewrites"].get(decision_id) or rewritten_note(item, comment))
                if state is not None:
                    save(data, state)
            elif status == "revoked":
                proposal["note"] = applied_note(item, data)
            line = note_line(proposal)
            expected = {"target": str(reference.resolve()), "router": str(router.resolve()), "line": line}
            contents = reference.read_text(encoding="utf-8") if reference.is_file() else ""
            if MARKER + proposal_id in contents and contents.count(line) != 1:
                raise ValueError("adopted line changed; review target before retry")
            if status == "adopted":
                stored = data["applied_actions"].get(proposal_id)
                if stored is not None and stored != expected:
                    raise ValueError("saved adoption target does not match the skill")
                if stored is not None or (contents.count(line) == 1
                                          and ROUTER_SECTION in router.read_text(encoding="utf-8")):
                    if contents.count(line) != 1 or ROUTER_SECTION not in router.read_text(encoding="utf-8"):
                        raise ValueError("adopted note or router pointer is missing")
                    path = reference.resolve()
                else:
                    source_reason = source_checkout_ready(router, data)
                    if source_reason:
                        raise ValueError(source_reason)
                    auto = any(d.get("decision") == "adopt" and d.get("actor") == "triage-retro"
                               for d in item.get("decisions", []))
                    path = adopt(proposal, root, auto=auto)
                    remember_versions(data, router, path)
                data["applied_actions"][proposal_id] = expected
                writing_completed = True
                if state is not None:
                    save(data, state)
                rewrite = f"\nコメントで書き直した文: {proposal['note']}" if decision_id in data["rewrites"] else ""
                ledger.record({"proposal_id": proposal_id, "decision_id": decision_id,
                               "outcome": "applied",
                               "actor": "triage-retro", "result": f"appended to {path}{rewrite}",
                               "undo": json.dumps(expected, ensure_ascii=False)})
            else:
                applied = next((e for e in reversed(item.get("executions", []))
                                if e.get("outcome") == "applied"), None)
                token = (json.loads(applied["undo"]) if applied else
                         data["applied_actions"].get(proposal_id))
                if token is None and contents.count(line) == 1:
                    token = expected  # The note was written before its execution receipt.
                if token is not None:
                    if token != expected:
                        raise ValueError("undo target does not match the skill")
                    proposal.update({"target_path": token["target"], "router_path": token["router"]})
                    track_versions = source_checkout_ready(router, data) is None
                    undo(proposal)
                    if track_versions:
                        remember_versions(data, router, reference)
                    else:
                        for changed_path in (router, reference):
                            data["target_versions"].pop(str(changed_path.resolve()), None)
                    result = f"removed adopted line from {reference}"
                else:
                    result = "no skill line was applied"
                writing_completed = True
                if state is not None:
                    save(data, state)
                ledger.record({"proposal_id": proposal_id, "decision_id": decision_id,
                               "outcome": "reverted",
                               "actor": "triage-retro", "result": result})
            data["applied_actions"].pop(proposal_id, None)
            data["action_attempts"].pop(action_key, None)
            data["rewrites"].pop(decision_id, None)
            changed += 1
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            if writing_completed:
                raise  # A written note or reversal needs a receipt, not another local edit.
            attempts = data["action_attempts"][action_key] = (
                data["action_attempts"].get(action_key, 0) + 1)
            if state is not None:
                save(data, state)
            ledger.record({"proposal_id": proposal_id, "decision_id": decision_id,
                           "outcome": "failed",
                           "actor": "triage-retro", "result": str(exc)[:500]})
            if attempts == 3:
                notify(f"振り返りの{'採用' if status == 'adopted' else '取り消し'}を worker が反映できず、"
                       f"再試行を止めました。案 {proposal_id}、書き先 {skill}（{category}）、理由: {str(exc)[:300]}")
    return changed


def record_candidates(sid: str, transcript: Path, candidates: Sequence[dict[str, Any]], ledger: Ledger,
                      state: Path = STATE, root: Path = SOURCE) -> int:
    """Record reviewed lessons, adopt eligible facts, and mark the session done; callers hold the worker lock."""
    with locked(paths(state)[2]):
        data = load(state)
    changed = 0
    items = ledger.list()
    known = {item["proposal_id"]: item for item in items}
    for candidate in candidates:
        candidate["id"] = candidate.get("duplicate_of") or proposal_id(sid, candidate)
        if candidate["id"] in known:
            old = known[candidate["id"]]
            evidence = [line for line in evidence_lines(candidate, sid)
                        if line not in old.get("evidence", [])]
            if evidence:
                ledger.add_evidence({"proposal_id": candidate["id"],
                                     "source": {"session": sid,
                                                "worktree": transcript_worktree(transcript)},
                                     "evidence": evidence})
            elif old["status"] == "pending":
                eligible, _ = eligible_auto(candidate, data, items, root)
                if eligible:
                    ledger.decide({"proposal_id": candidate["id"], "decision": "adopt",
                                   "actor": "triage-retro", "reason": "verified low-risk fact"})
                    with locked(paths(state)[2]):
                        data = load(state)
                        changed += sync_decisions(ledger, data, root, state)
                        save(data, state)
            items = ledger.list()
            known = {item["proposal_id"]: item for item in items}
            continue
        eligible, reason = eligible_auto(candidate, data, items, root)
        ledger.propose(ledger_payload(candidate, sid, transcript, reason))
        if eligible:
            ledger.decide({"proposal_id": candidate["id"], "decision": "adopt",
                           "actor": "triage-retro", "reason": "verified low-risk fact"})
            with locked(paths(state)[2]):
                data = load(state)
                changed += sync_decisions(ledger, data, root, state)
                save(data, state)
        items = ledger.list()
        known = {item["proposal_id"]: item for item in items}
    with locked(paths(state)[2]):
        data = load(state)
        changed += sync_decisions(ledger, data, root, state)
        session = data["sessions"].setdefault(sid, {"session_id": sid, "transcript": str(transcript),
                                                    "queued_at": now(), "attempts": 0})
        session.update({"status": "done", "processed_at": now()})
        session.pop("error", None)
        data.pop("last_failure", None)
        save(data, state)
    return changed


PUBLISH_BRANCH = "triage-retro/observed-facts"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main_digest(repo: Path, name: str) -> str | None:
    shown = subprocess.run(["git", "-C", str(repo), "show", f"origin/main:{name}"], capture_output=True, timeout=60)
    return None if shown.returncode else hashlib.sha256(shown.stdout).hexdigest()


def worker_changes(data: dict[str, Any], repo: Path) -> list[Path]:
    """Files that differ from HEAD exactly as the worker last wrote them; other edits stay out."""
    status = git(repo, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    dirty = {(repo / entry[3:]).resolve() for entry in status.split("\0") if entry}
    return sorted(path for key, recorded in data["target_versions"].items()
                  if (path := Path(key)) in dirty and digest(path) == recorded)


def settle(repo: Path, changed: Sequence[Path], data: dict[str, Any]) -> str:
    """The notes are in main: drop the local copies and fast-forward, restoring them if that fails."""
    saved = {path: path.read_bytes() for path in changed if path.is_file()}
    names = [str(path.relative_to(repo)) for path in changed]
    tracked = [name for name in names if git(repo, "cat-file", "-e", f"HEAD:{name}", check=False).returncode == 0]
    if tracked:
        git(repo, "checkout", "--", *tracked)
    for path in changed:
        if str(path.relative_to(repo)) not in tracked:
            path.unlink(missing_ok=True)
    merged = git(repo, "merge", "--ff-only", "--quiet", "origin/main", check=False)
    if merged.returncode:
        for path, content in saved.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return f"publish: notes are in main, but the checkout did not fast-forward: {merged.stderr.strip()[:200]}"
    data.pop("published", None)
    return "publish: settled after the merge"


def publish(data: dict[str, Any], root: Path = SOURCE) -> str:
    """Put the worker's notes in the source checkout on one PR branch; after the merge, return it to main."""
    repo = root.resolve()
    if git(repo, "remote", "get-url", "origin", check=False).returncode:
        return "publish: the source checkout has no origin"
    changed = worker_changes(data, repo)
    if not changed:
        data.pop("published", None)
        return "publish: nothing to publish"
    git(repo, "fetch", "--quiet", "origin")
    names = [str(path.relative_to(repo)) for path in changed]
    if all(main_digest(repo, name) == digest(path) for name, path in zip(names, changed)):
        return settle(repo, changed, data)
    digests = {name: digest(path) for name, path in zip(names, changed)}
    if data.get("published") == digests:
        return "publish: the PR branch is up to date"
    with tempfile.TemporaryDirectory(prefix="triage-retro-publish-") as scratch:
        tree = Path(scratch) / "tree"
        git(repo, "worktree", "add", "--detach", "--quiet", str(tree), "origin/main")
        try:
            for name, path in zip(names, changed):
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                if path.is_file():
                    shutil.copyfile(path, tree / name)
                else:
                    (tree / name).unlink(missing_ok=True)
            git(tree, "add", "--all", "--", *names)
            git(tree, "commit", "--quiet", "-m", "docs: record adopted retrospective notes")
            lease = git(repo, "ls-remote", "origin", f"refs/heads/{PUBLISH_BRANCH}").stdout.split()
            git(tree, "push", "--quiet", f"--force-with-lease=refs/heads/{PUBLISH_BRANCH}:{lease[0] if lease else ''}",
                "origin", f"HEAD:refs/heads/{PUBLISH_BRANCH}")
        finally:
            git(repo, "worktree", "remove", "--force", str(tree), check=False)
    listed = subprocess.run(["gh", "pr", "list", "--head", PUBLISH_BRANCH, "--state", "open", "--json", "number"],
                            capture_output=True, text=True, timeout=60, cwd=repo, check=True)
    if not json.loads(listed.stdout or "[]"):
        body = ("振り返りの worker が、採用された文を skill に書いた分です。branch は worker が作り直します。\n\n"
                + "\n".join(f"- `{name}`" for name in names) + "\n\ntriage-retro worker")
        subprocess.run(["gh", "pr", "create", "--base", "main", "--head", PUBLISH_BRANCH, "--assignee", "Eotel",
                        "--title", "docs: record adopted retrospective notes", "--body", body],
                       capture_output=True, text=True, timeout=60, cwd=repo, check=True)
    data["published"] = digests
    return f"publish: pushed {len(changed)} files to {PUBLISH_BRANCH}"


def publish_safely(state: Path = STATE, root: Path = SOURCE) -> str:
    """Publish under the state lock; a failure is logged and noticed once per reason, never fatal."""
    with locked(paths(state)[2]):
        data = load(state)
        try:
            message = publish(data, root)
            data.pop("publish_failure", None)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            message = f"publish failed: {exc}"
            if data.get("publish_failure") != message:
                notify(f"振り返りの worker が、採用した文の PR を出せませんでした: {message[:300]}")
            data["publish_failure"] = message
        save(data, state)
    return message


def failure_repeated(data: dict[str, Any], reason: str) -> bool:
    """Count one failure reason across runs; true only when it has just happened twice in a row."""
    key = re.sub(r"[0-9a-f-]{8,}|\d+", "#", reason)
    last = data.get("last_failure") or {}
    count = last.get("count", 0) + 1 if last.get("key") == key else 1
    data["last_failure"] = {"key": key, "reason": reason, "count": count, "at": now()}
    return count == 2


def notify(message: str) -> None:
    """Ask the triage lead to look; a missing notifier must not stop the worker."""
    try:
        subprocess.run([str(NOTIFY), "--kind", "ask", message], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"triage-notify failed: {exc}", file=sys.stderr, flush=True)


def process_once(state: Path = STATE, root: Path = SOURCE, claude: str = "claude", ignore_load: bool = False,
                 ledger: Ledger | None = None) -> str:
    """Reconcile decisions and process at most one hourly transcript outside Orca tabs."""
    ledger = ledger or LedgerClient()
    if not ledger.writer_enabled():
        return "deferred: adoption ledger decision writer is not enabled"
    with locked(state / "worker.lock"):
        with locked(paths(state)[2]):
            data = load(state)
            ingest(data, state)
            pending = next(((sid, s) for sid, s in data["sessions"].items()
                            if s["status"] == "pending" and s["attempts"] < 3), None)
            changed = sync_decisions(ledger, data, root, state)
            save(data, state)
        publication = publish_safely(state, root)
        if not publication.endswith(("nothing to publish", "has no origin", "is up to date")):
            print(publication, file=sys.stderr, flush=True)
        if pending is None:
            return f"empty; reconciled {changed} decisions"
        cpu = os.cpu_count() or 8
        limit = min(12, cpu * 0.75)
        if not ignore_load and os.getloadavg()[0] > limit:
            return f"deferred: load {os.getloadavg()[0]:.1f} > {limit:.1f}"
        sid, session = pending
        transcript = Path(session["transcript"])
        try:
            items = ledger.list()
            candidates = build_proposals(transcript, root, claude, as_existing(items), rejected_lessons(items))
            changed += record_candidates(sid, transcript, candidates, ledger, state, root)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            reason = str(exc)[:500]
            with locked(paths(state)[2]):
                data = load(state)
                data["sessions"][sid]["attempts"] += 1
                data["sessions"][sid]["error"] = reason
                if data["sessions"][sid]["attempts"] >= 3:
                    data["sessions"][sid]["status"] = "failed"
                repeated = failure_repeated(data, reason)
                save(data, state)
            if repeated:
                notify("定時 triage の振り返りが同じ理由で 2 回続けて失敗しました"
                       f"（{sid}）: {reason[:300]} 対処は orca-session-triage の references/retro.md を参照。")
            return f"error: {sid}: {exc}"
        return f"processed: {sid}: {len(candidates)} proposals; reconciled {changed} decisions"


def session_transcript(session_id: str) -> Path:
    if not SESSION_ID.fullmatch(session_id or ""):
        raise ValueError("session ID must be a UUID")
    found = sorted((Path.home() / ".claude/projects").glob(f"*/{session_id}.jsonl"))
    if len(found) != 1 or not hourly_transcript(found[0]):
        raise ValueError(f"no hourly transcript for session {session_id}")
    return found[0].resolve()


def submit(answer: object, session_id: str, state: Path = STATE, root: Path = SOURCE, claude: str = "claude",
           ledger: Ledger | None = None) -> str:
    """Record a live hourly tab's own forked retrospective; the queued copy is then skipped."""
    ledger = ledger or LedgerClient()
    if not ledger.writer_enabled():
        raise RuntimeError("adoption ledger decision writer is not enabled; the Stop hook queues this session")
    if not root.is_dir():
        raise FileNotFoundError(f"skill source checkout is missing: {root}")
    transcript = session_transcript(session_id)
    raw = answer.get("proposals") if isinstance(answer, dict) else None
    if not isinstance(raw, list):
        raise ValueError('submission must be {"proposals": [...]}')
    with locked(state / "worker.lock"):
        with locked(paths(state)[2]):
            data = load(state)
            ingest(data, state)
            save(data, state)
        if (data["sessions"].get(session_id) or {}).get("status") == "done":
            return f"already processed: {session_id}"
        candidates = review_proposals(raw, transcript, root, claude, as_existing(ledger.list()))
        changed = record_candidates(session_id, transcript, candidates, ledger, state, root)
    return f"processed: {session_id}: {len(candidates)} proposals; reconciled {changed} decisions"


def live_prompt(session_id: str, root: Path = SOURCE, rejected: str = "") -> str:
    """Instructions for the Agent fork that a live hourly tab starts at the end of its run."""
    script = Path(__file__).resolve()
    return (fork_prompt(session_transcript(session_id), root, rejected)
            + "\n\n答えは次の JSON Schema に合う JSON にし、道具はこの記録のコマンドだけに使う。"
            "標準入力に渡して実行し、出力を一行で報告する:\n"
            f"python3 {script} submit --session-id {session_id} <<'JSON'\n...\nJSON\n\n"
            "SCHEMA\n" + json.dumps(SCHEMA, ensure_ascii=False))


def run_service(state: Path = STATE, root: Path = SOURCE, ledger: Ledger | None = None) -> None:
    """Poll one queued session at a time outside the triage tab."""
    try:
        auth = subprocess.run(["claude", "auth", "status", "--json"],
                              capture_output=True, text=True, timeout=15,
                              env=claude_environment())
        status = json.loads(auth.stdout) if auth.returncode == 0 else {}
        status = status if isinstance(status, dict) else {}
        print(f"claude auth: authMethod={status.get('authMethod', 'unavailable')} "
              f"subscriptionType={status.get('subscriptionType', 'unavailable')}", flush=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"claude auth check failed: {exc}", file=sys.stderr, flush=True)
    while True:
        try:
            print(process_once(state, root, ledger=ledger), flush=True)
        except Exception as exc:
            print(f"retro worker: {exc}", file=sys.stderr, flush=True)
        time.sleep(300)


def retry_failed(state, session_id):
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError("session ID must be a UUID")
    with locked(state / "worker.lock"):
        with locked(paths(state)[2]):
            data = load(state)
            ingest(data, state)
            session = data["sessions"].get(session_id)
            if session is None or session["status"] != "failed":
                raise ValueError("session is not failed")
            session["status"] = "pending"
            session["attempts"] = 0
            session.pop("error", None)
            save(data, state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("process-once", "run-service", "status", "enqueue", "retry",
                                           "fork-prompt", "submit"))
    parser.add_argument("--state", type=Path, default=STATE, help=f"worker state directory (default: {STATE})")
    parser.add_argument("--source", type=Path, default=SOURCE, help=f"Eotel/skills source checkout (default: {SOURCE})")
    parser.add_argument("--ledger-script", type=Path, default=DEFAULT_SCRIPT,
                        help=f"adoption-triage ledger CLI (default: {DEFAULT_SCRIPT})")
    parser.add_argument("--events", type=Path, help="adoption ledger events path for an isolated test")
    parser.add_argument("--imports", type=Path, help="adoption ledger imports path for an isolated test")
    parser.add_argument("--decision-config", type=Path,
                        help="adoption ledger decision-writer configuration")
    parser.add_argument("--ignore-load", action="store_true", help="manual processing only")
    parser.add_argument("--session-id", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                        help="hourly session to retry, prompt, or submit (default: $CLAUDE_CODE_SESSION_ID)")
    args = parser.parse_args(argv)
    ledger = LedgerClient(args.ledger_script, args.events, args.imports, args.decision_config)
    try:
        if args.action == "enqueue":
            print("queued" if enqueue(json.load(sys.stdin), args.state) else "skipped")
        elif args.action == "process-once":
            print(process_once(args.state, args.source, ignore_load=args.ignore_load, ledger=ledger))
        elif args.action == "run-service":
            run_service(args.state, args.source, ledger)
        elif args.action == "fork-prompt":
            print(live_prompt(args.session_id or "", args.source, rejected_lessons(ledger.list())))
        elif args.action == "submit":
            print(submit(json.load(sys.stdin), args.session_id or "", args.state, args.source, ledger=ledger))
        elif args.action == "retry":
            retry_failed(args.state, args.session_id or "")
            print(f"retry queued: {args.session_id}")
        elif args.action == "status":
            with locked(paths(args.state)[2]):
                data = load(args.state)
                ingest(data, args.state)
                save(data, args.state)
            print(json.dumps({"sessions": len(data["sessions"]),
                              "pending_sessions": sum(s["status"] == "pending" for s in data["sessions"].values()),
                              "failed_sessions": sum(s["status"] == "failed" for s in data["sessions"].values()),
                              "failed_session_ids": [sid for sid, session in data["sessions"].items()
                                                     if session["status"] == "failed"],
                              "failed_actions": sum(n >= 3 for n in data["action_attempts"].values()),
                              "last_failure": (data.get("last_failure") or {}).get("reason"),
                              "proposals": len(ledger.list())}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"triage-retro: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
