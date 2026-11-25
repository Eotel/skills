#!/usr/bin/env -S uv run --no-project --no-config --python >=3.12
"""Check that an Orca worktree is safe to remove."""

from __future__ import annotations

import argparse
from collections.abc import Collection, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import filecmp
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from typing import NamedTuple, NotRequired, TypedDict
from unicodedata import normalize
from urllib.parse import urlsplit

import lead_decisions
import hourly_runtime
from worktree_notes import NotesUnavailable, OrcaNotes, WorktreeNotes, keep_note

# A path counts as regenerable when a tool writes it again from tracked files or installed
# packages, with no one's input, and it holds nothing else. Two signs show that:
# - The file format is the tool's output: CPython bytecode (.pyc) is never a source anyone
#   writes, so it is regenerable even untracked. An untracked path is never judged by its
#   name: a build/ or test-results/ the repository does not ignore may be a new source.
# - The repository ignores the path (it declares it is not source) and the path is a known
#   tool's output: the names below.
# A `.gitignore` of `*` inside a directory is no sign on its own: yomiyasu writes one to
# keep its records (.yomiyasu/inputs/) out of git.
CACHE_PARTS = {
    "node_modules", ".venv", ".devenv", ".direnv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".svelte-kit", "apm_modules", ".import_linter_cache", ".moon", "dist", "build",
    "coverage", ".coverage", "test-results", ".cache",
}
# Playwright's HTML reporter writes to playwright-report/ or the outputFolder a config names after it, and its
# blob reporter to blob-report/.
CACHE_PREFIXES = ("playwright-report", "blob-report")
CACHE_SUFFIXES = (".pyc", ".mo", ".tsbuildinfo")
CACHE_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
TOPIC_PR = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
TOPIC_FIXER_JSON = "topic-fixer-json: "
TOPIC_STATE = re.compile(r"(?:^|、)(?:agent なし|生きている agent: .+|作業場はもう無い)$")
TOPIC_BOUNDARY = re.compile(r"https://\S+|[A-Z][A-Z0-9_]*-\d+|\d+|照合 \d+ 件・.*")
BYTECODE_SUFFIX = ".pyc"
# Generated sources and e2e output a setup or a test run writes again. Matched by
# path, not by any part, so a hand-made docs/screenshots/ still goes to review.
# APM 0.35 binds each checkout it installs to with a random .apm-local/installation and
# writes a new one on the next install, so a removed checkout's identity binds nothing.
# husky and vite-hooks write the scripts under their `_` directory on each install. GraphQL codegen
# writes graphql/generated/; a generated/ elsewhere may hold hand-made output, so it goes to review.
# taihei-cbm's tests write the uploaded data files under apps/api/media/files/datafile/storage/.
REGENERABLE_PARTS = {"__generated__"}
REGENERABLE_PART_PAIRS = {("graphql", "generated"), ("var", "test-storage")}
REGENERABLE_PREFIXES = ("src/paraglide/", "e2e/screenshots/", "e2e/.state/", "e2e/.auth/", ".apm-local/",
                        ".husky/_/", ".vite-hooks/_/", "apps/api/media/files/datafile/storage/")
# Ignored outputs a tracked file of the repository writes, by that file. Each counts as regenerable only while
# its generator is tracked in the worktree and names the output's file name: a repository without that
# generator gets no such pass. A worktree setup hook's marker for a failed `uv sync`, a SessionStart hook's
# log, a GraphQL schema document, and the git info a prebuild script writes for the UI.
GENERATED_BY = {
    ".claude/.venv-missing": ".claude/hooks/worktree_create.sh",
    ".worktree-setup.log": ".claude/settings.json",
    "docs/generated/graphql-schema.md": "backend/cxc_tools/regen_graphql_doc.py",
    "public/git-info.json": "scripts/gen-git-info.js",
}
# Ignored outputs a named tool builds again from a tracked file beside them: Cargo's target/ beside Cargo.toml,
# Tauri's gen/schemas/ under the directory of its config, Composer's vendor/ beside composer.json and its lock, and
# the outDir a vite.config.* names, read from its line (`outDir: …` with its string literals joined as path parts)
# from the config's directory.
CARGO_MANIFEST = "Cargo.toml"
TAURI_CONFIGS = ("tauri.conf.json", "tauri.conf.json5", "Tauri.toml")
COMPOSER_DIR = "vendor"
COMPOSER_MANIFESTS = ("composer.json", "composer.lock")
VITE_CONFIGS = ":(glob)**/vite.config.*"
VITE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
VITE_OUT_DIR = re.compile(r"^\s*outDir\s*:(.*)$", re.MULTILINE)
# More than one outDir (per mode, say) leaves which one builds to the config's code, so such a config is not read.
VITE_OUT_DIR_ANYWHERE = re.compile(r"""\boutDir['"]?\s*:""")
# A config that moves Vite's root resolves a relative outDir from there, so beside it only an outDir that is an absolute
# path from the config's own file is read: fileURLToPath(new URL(…, import.meta.url)) or path.resolve/join(__dirname, …),
# matched with its string literals removed.
VITE_ROOT = re.compile(r"""\broot['"]?\s*:""")
VITE_FROM_CONFIG = re.compile(r"\s*(?:fileURLToPath\(\s*new\s+URL\(\s*,\s*import\.meta\.url\s*\)\s*\)"
                              r"|(?:path\.)?(?:resolve|join)\(\s*__dirname\s*(?:,\s*)+\))\s*,?\s*")
JS_STRING = re.compile(r"""(['"`])((?:(?!\1).)*)\1""")
JS_STRING_OR_COMMENT = re.compile(r"""("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)|/\*.*?\*/|//[^\n]*""",
                                  re.DOTALL)
# What an outDir value may hold besides string literals: calls such as fileURLToPath(new URL(…, import.meta.url)) or
# path.resolve(__dirname, …) that join them as path parts. An operator such as + makes another value, so it is not read.
VITE_PATH_CALL = re.compile(r"[\w\s.,()]*")
# A template interpolation, or a URL escape, query or fragment, makes a path other than the literal's own text.
VITE_UNREAD_LITERAL = re.compile(r"\$\{|[%?#]")
# `terraform init` downloads providers and modules into .terraform/ and records the backend in its terraform.tfstate;
# with any backend but local, the state itself is kept by the backend.
TERRAFORM_DIR = ".terraform"
# A tracked Terraform file's archive_file writing into a directory beside it: output_path = "${path.module}/zip/…",
# captured after the directory; an interpolation in it (${each.key}) stands for any one path segment.
ARCHIVE_OUTPUT = 'output_path\\s*=\\s*"\\$\\{path\\.module\\}/%s/([^"]+)"'
INTERPOLATION = re.compile(r"\$\{[^}]*\}")
# HCL's comments, and the top-level block headers terraform fmt starts at the line's start (data "archive_file" "x" {).
HCL_COMMENT = re.compile(r"/\*.*?\*/|(?:#|//)[^\n]*", re.DOTALL)
HCL_BLOCK = re.compile(r"^[a-z_]+\b[^\n{]*\{", re.MULTILINE)
ARCHIVE_BLOCK = re.compile(r'(?:data|resource)\s+"archive_file"\s')
# An ignored directory of nothing but logs (django-environ's DEBUG app.log) holds no one's work.
LOG_SUFFIX = ".log"
# The blocker for nested worktrees --detach-nested can remove.
NESTED_READY = "worktrees of other repositories remain inside: "
NESTED_RESTORE = "approved tracked changes to restore in nested repositories: "
# Records agents keep in a worktree outside git; --archive-scratch moves them aside.
SCRATCH_DIRS = (".yomiyasu", ".investigate")
# Records --archive-scratch copies to main. Other ignored files, such as .claude/settings.local.json or .env,
# are settings tools in main would run with, so they stay for a decision.
RECORD_DIRS = (".claude/reviews/",)
# Work records among the ignored paths, by kind: investigation notes, review records (agents' and impeccable's
# design critiques), and verification images. Nothing in main runs with them, so --archive-scratch moves them
# out of the worktree with a manifest instead of asking. Anything else ignored that holds work is held.
RECORD_KINDS = {".yomiyasu": "notes", ".investigate": "notes", ".claude/reviews": "reviews",
                ".impeccable": "reviews"}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
DATA_SUFFIXES = (".sqlite3", ".sqlite", ".db", ".csv", ".tsv", ".xlsx", ".sql", ".dump", ".parquet")
# Where --archive-scratch and --archive-path record each move: when, from where, to where, and the comparison.
ARCHIVE_MANIFEST = "archived.jsonl"
ARCHIVE_DIR = Path.home() / ".cache/orca-briefs/archive"
# Closes a busy tab only while its worktree has not moved since the scan that listed it.
CLOSE_TABS = Path(__file__).absolute().with_name("close_tabs.py")
# A per-worktree .env may move these away from its .env.example and still be written again.
ENV_LOCAL_KEY_SUFFIXES = ("_PORT", "_PORT_RANGE", "_BIND_IP")
ENV_LOCAL_KEYS = {"COMPOSE_PROJECT_NAME"}
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Remote refs older than this are fetched again before the check judges merged work by them.
FETCH_AFTER = timedelta(hours=1)
FETCH_TIMEOUT = 60
# `orca worktree rm` answers `removing: true` and finishes in the background within about a minute.
GONE_WAIT_SECONDS = 60
GONE_POLL_SECONDS = 2
# A process outside Orca tabs is read again after this wait before it blocks: one that ended by then used the
# worktree for a moment (2026-11-06, one gone PID blocked all 14 worktrees of a batch).
PROCESS_RECHECK_SECONDS = 2
# Claude Code's lock on a session's scheduled tasks, {"pid": …, "procStart": <ps lstart in UTC>, …}: it says only
# that its holder runs, so a lock whose holder ended is regenerable and a live holder is a process using the worktree.
SCHEDULED_LOCK = ".claude/scheduled_tasks.lock"
# How many uncommitted paths `reasons` names after their count.
REASON_PATHS = 10


def git(cwd: str | Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)


def is_cache(relpath: str) -> bool:
    parts = Path(relpath.rstrip("/")).parts
    # pip writes <name>.egg-info beside an editable install's sources again on each install.
    return (bool(CACHE_PARTS.intersection(parts)) or relpath.endswith(CACHE_SUFFIXES)
            or any(part.endswith(".egg-info") or part.startswith(CACHE_PREFIXES) for part in parts))


def has_cache_tag(directory: Path) -> bool:
    if directory.is_symlink() or not directory.is_dir():
        return False
    tag = directory / "CACHEDIR.TAG"
    try:
        if not stat.S_ISREG(tag.lstat().st_mode):
            return False
        with tag.open("rb") as stream:
            return stream.read(len(CACHE_TAG_SIGNATURE)) == CACHE_TAG_SIGNATURE
    except OSError:
        return False


def holds_only_bytecode(path: Path) -> bool:
    """A .pyc file, or a directory whose files, in any subdirectory, are all .pyc."""
    if path.is_symlink() or not path.is_dir():
        return path.is_file() and not path.is_symlink() and path.suffix == BYTECODE_SUFFIX
    files = [child for child in path.rglob("*") if child.is_symlink() or not child.is_dir()]
    return bool(files) and all(holds_only_bytecode(child) for child in files)


def log_files(path: Path) -> list[Path] | None:
    """The files of a directory whose files, in any subdirectory, are all *.log; None for anything else."""
    if path.is_symlink() or not path.is_dir():
        return None
    files = [child for child in path.rglob("*") if child.is_symlink() or not child.is_dir()]
    if not files or any(child.is_symlink() or child.suffix != LOG_SUFFIX for child in files):
        return None
    return files


def log_note(rel: str, files: list[Path]) -> str:
    stats = [child.stat() for child in files]
    latest = datetime.fromtimestamp(max(stat.st_mtime for stat in stats), timezone.utc)
    return "%s holds only logs: %d bytes, last written %s" % (
        rel, sum(stat.st_size for stat in stats), latest.strftime("%Y-%m-%dT%H:%M:%SZ"))


def working_changes(worktree: str | Path, *, all_untracked: bool = False) -> tuple[list[str], list[str], list[str]]:
    tracked_changes, untracked, regenerable = [], [], []
    # -z gives each path unquoted; a rename's second entry names its source and is skipped.
    mode = "all" if all_untracked else "normal"
    entries = iter(git(worktree, "--no-optional-locks", "status", "--porcelain", "-z", f"--untracked-files={mode}")
                   .stdout.split("\0"))
    for entry in entries:
        if not entry:
            continue
        if set(entry[:2]) & set("RC"):
            next(entries, None)
        rel = entry[3:]
        if entry.startswith("?? "):
            (regenerable if holds_only_bytecode(Path(worktree) / rel) else untracked).append(rel)
        else:
            tracked_changes.append(rel)
    return tracked_changes, untracked, regenerable


def is_regenerable(relpath: str) -> bool:
    rel = relpath if relpath.endswith("/") else relpath + "/"
    parts = Path(relpath.rstrip("/")).parts
    return (bool(REGENERABLE_PARTS.intersection(parts)) or rel.startswith(REGENERABLE_PREFIXES)
            or bool(REGENERABLE_PART_PAIRS.intersection(zip(parts, parts[1:]))))


def same_tree(left: Path, right: Path) -> bool:
    """Every file on the left is copied on the right; the right may hold more files."""
    if left.is_symlink() or right.is_symlink():
        # Path.readlink() normalizes trailing slashes and '/.' in a link target.
        return left.is_symlink() and right.is_symlink() and os.readlink(left) == os.readlink(right)  # noqa: PTH115
    if left.is_file():
        return right.is_file() and filecmp.cmp(left, right, shallow=False)
    if not (left.is_dir() and right.is_dir()):
        return False
    # Level by level, so a symlinked directory on either side is compared as a link.
    return all(same_tree(child, right / child.name) for child in left.iterdir())


def holds_nothing(path: Path) -> bool:
    """An empty file, or a directory whose files are all empty."""
    if path.is_symlink():
        return False
    if path.is_file():
        return path.stat().st_size == 0
    return path.is_dir() and all(holds_nothing(child) for child in path.iterdir())


def holds_only_regenerable(path: Path, worktree: Path) -> bool:
    """An ignored tool output, or a directory holding only tool output and empty files."""
    rel = str(path.relative_to(worktree))
    if is_cache(rel) or is_regenerable(rel):
        return True
    if path.is_symlink():
        return False
    if path.is_file():
        return path.match("test_*.sqlite3")
    if not path.is_dir():
        return False
    if path.name == "site" and any((path.parent / name).is_file() for name in ("mkdocs.yml", "mkdocs.yaml")):
        return True
    return all(holds_nothing(child) or holds_only_regenerable(child, worktree) for child in path.iterdir())


def env_values(path: Path) -> dict[str, str] | None:
    """KEY=value pairs, or None when a line is not one (a multiline value such as a PEM key):
    its continuation lines would otherwise vanish or surface as key names."""
    values = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not ENV_KEY.fullmatch(key):
            return None
        if value[:1] in ("'", '"') and (len(value) < 2 or value[-1] != value[0]):
            return None
        values[key] = value
    return values


def env_keys_beyond_example(env: Path) -> list[str] | None:
    """Keys whose values the .env.example beside `env` lacks, ports aside; None without an example.

    Only key names are returned: the values may be API keys held nowhere else.
    """
    example = env.with_name(env.name + ".example")
    if not example.is_file():
        return None
    expected, actual = env_values(example), env_values(env)
    if expected is None or actual is None:
        return None
    return [key for key, value in actual.items()
            if expected.get(key) != value
            and key not in ENV_LOCAL_KEYS and not key.endswith(ENV_LOCAL_KEY_SUFFIXES)]


def git_directories(source: Path) -> tuple[Path, Path] | None:
    metadata = source / ".git"
    if source.is_symlink() or metadata.is_symlink() or not metadata.exists():
        return None
    result = git(source, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir")
    paths = result.stdout.splitlines()
    if result.returncode or len(paths) != 3 or Path(paths[0]).resolve() != source.resolve():
        return None
    return Path(paths[1]).resolve(), Path(paths[2]).resolve()


def nested_worktrees(source: Path) -> list[Path]:
    """Linked Git worktrees at an ignored path or directly inside it."""
    if not source.is_dir() or source.is_symlink():
        return []
    candidates = [source] if (source / ".git").is_file() else sorted(
        child for child in source.iterdir() if (child / ".git").is_file())
    return [path for path in candidates if (private := git_directories(path)) and private[0] != private[1]]


def running(pid: int, started: str | None = None) -> bool:
    """Whether `pid` is a live process, not a zombie, and, given `started` as ps's lstart in UTC (the procStart of
    a lock), the process that started then rather than a later one given the same PID. True when ps cannot run."""
    try:
        fields = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True, text=True,
                                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout.split(None, 1)
    except OSError:
        return True
    if len(fields) != 2 or fields[0].startswith("Z"):
        return False
    return started is None or fields[1].split() == started.split()


class LockHolder(NamedTuple):
    pid: int
    running: bool


def lock_holder(lock: Path) -> LockHolder | None:
    """The process a scheduled tasks lock names and whether it still runs; None when the file is no such lock."""
    try:
        record = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = record.get("pid") if isinstance(record, dict) else None
    if type(pid) is not int or pid <= 0:
        return None
    started = record.get("procStart")
    return LockHolder(pid, running(pid, started if isinstance(started, str) else None))


def generated(worktree: Path, rel: str) -> bool:
    """Whether GENERATED_BY names a generator of `rel` that the worktree tracks and that names the file."""
    generator = GENERATED_BY.get(rel.rstrip("/"))
    if generator is None or git(worktree, "ls-files", "--error-unmatch", "--", generator).returncode != 0:
        return False
    try:
        return PurePosixPath(rel.rstrip("/")).name in (worktree / generator).read_text(errors="replace")
    except OSError:
        return False


def tracked(worktree: Path, rel: PurePosixPath) -> bool:
    return git(worktree, "ls-files", "--error-unmatch", "--", str(rel)).returncode == 0


def tool_output(worktree: Path, rel: str) -> str | None:
    """What a named tool writes again at this ignored path, from a tracked file the worktree holds, or None:
    Composer's vendor/ (or a path in it) beside composer.json and composer.lock, the outDir of a vite.config.*,
    Cargo's target/ beside Cargo.toml, Tauri's gen/schemas/ beside its config, a .terraform/ whose backend is not
    local, and a directory of zip files that a Terraform archive_file beside it writes."""
    path = PurePosixPath(rel.rstrip("/"))
    source = worktree / path
    if COMPOSER_DIR in path.parts:
        project = PurePosixPath(*path.parts[:path.parts.index(COMPOSER_DIR)])
        manifests = [project / name for name in COMPOSER_MANIFESTS]
        if all(tracked(worktree, manifest) for manifest in manifests) and composer_vendor_dir(worktree / manifests[0]):
            return "Composer's packages (%s; composer install writes them again)" % ", ".join(map(str, manifests))
    if source.is_symlink() or not source.is_dir():
        return None
    if config := vite_config_writing(worktree, path):
        return "Vite's build output (outDir in %s; npm run build writes it again)" % config
    if path.name == "target" and tracked(worktree, path.parent / CARGO_MANIFEST):
        return "Cargo's build output (%s)" % (path.parent / CARGO_MANIFEST)
    if path.parts[-2:] == ("gen", "schemas"):
        config = next((path.parent.parent / name for name in TAURI_CONFIGS
                       if tracked(worktree, path.parent.parent / name)), None)
        return "Tauri's generated schemas (%s)" % config if config else None
    if path.name == TERRAFORM_DIR:
        try:
            backend = json.loads((source / "terraform.tfstate").read_text(encoding="utf-8")).get("backend") or {}
        except (OSError, ValueError, AttributeError):
            return None
        kind = backend.get("type") if isinstance(backend, dict) else None
        return ("terraform init's providers and modules; the %s backend keeps the state" % kind
                if isinstance(kind, str) and kind and kind != "local" else None)
    files = files_in(source)
    if not files or any(file.is_symlink() or file.suffix != ".zip" for file in files):
        return None
    writes = re.compile(ARCHIVE_OUTPUT % re.escape(path.name))
    outputs: dict[PurePosixPath, list[re.Pattern[str]]] = {}
    for name in git(worktree, "ls-files", "-z", "--", str(path.parent)).stdout.split("\0"):
        module = PurePosixPath(name)
        if module.suffix == ".tf" and module.parent == path.parent:
            try:
                found = archive_outputs(writes, (worktree / module).read_text(errors="replace"))
            except OSError:
                continue
            if found:
                outputs[module] = [re.compile("[^/]+".join(map(re.escape, INTERPOLATION.split(output))))
                                   for output in found]
    # Every zip has to be one an archive_file declares: a hand-made zip beside them is someone's.
    names = [file.relative_to(source).as_posix() for file in files]
    if not outputs or not all(any(output.fullmatch(name) for found in outputs.values() for output in found)
                              for name in names):
        return None
    return "archives Terraform's archive_file writes (%s)" % ", ".join(map(str, outputs))


def composer_vendor_dir(manifest: Path) -> bool:
    """Whether the composer.json at `manifest` installs into the default vendor/ (its config sets no other vendor-dir)."""
    try:
        config = json.loads(manifest.read_text()).get("config") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(config, dict) and posixpath.normpath(str(config.get("vendor-dir", COMPOSER_DIR))) == COMPOSER_DIR


def vite_config_writing(worktree: Path, path: PurePosixPath) -> PurePosixPath | None:
    """The tracked vite.config.* whose one outDir, a directory below the config's own, is `path` or holds it, or None."""
    for name in git(worktree, "ls-files", "-z", "--", VITE_CONFIGS).stdout.split("\0"):
        config = PurePosixPath(name)
        if config.suffix not in VITE_SUFFIXES:
            continue
        try:
            text = JS_STRING_OR_COMMENT.sub(lambda found: found.group(1) or "",
                                            (worktree / config).read_text(errors="replace"))
        except OSError:
            continue
        if len(VITE_OUT_DIR_ANYWHERE.findall(text)) != 1 or (found := VITE_OUT_DIR.search(text)) is None:
            continue
        parts = [literal for _, literal in JS_STRING.findall(found.group(1))]
        calls = JS_STRING.sub("", found.group(1))
        if not parts or any(VITE_UNREAD_LITERAL.search(part) for part in parts) or not VITE_PATH_CALL.fullmatch(calls):
            continue
        if VITE_ROOT.search(text) and not VITE_FROM_CONFIG.fullmatch(calls):
            continue
        out = PurePosixPath(posixpath.normpath(config.parent.joinpath(*parts)))
        if ".." not in out.parts and config.parent in out.parents and within(str(path), str(out)):
            return config
    return None


def archive_outputs(writes: re.Pattern[str], text: str) -> list[str]:
    """The output_path names `writes` captures inside the archive_file blocks of a Terraform file, comments left out."""
    text = HCL_COMMENT.sub("", text)
    starts = [found.start() for found in HCL_BLOCK.finditer(text)] + [len(text)]
    blocks = [text[start:end] for start, end in zip(starts, starts[1:])]
    return [name for block in blocks if ARCHIVE_BLOCK.match(block) for name in writes.findall(block)]


def ignore_source(worktree: Path, rel: str) -> str:
    """The ignore file, line, and pattern that exclude `rel`, as `git check-ignore -v` prints them."""
    found = git(worktree, "check-ignore", "-v", "--", rel).stdout.strip()
    return found.split("\t", 1)[0] or "an ignore rule git did not name"


def files_in(path: Path) -> list[Path]:
    """The path itself, or every file and symlink inside a directory."""
    if path.is_symlink() or not path.is_dir():
        return [path]
    return [child for child in path.rglob("*") if child.is_symlink() or not child.is_dir()]


class Record(TypedDict):
    path: str
    kind: str


class Held(TypedDict):
    path: str
    kind: str
    reason: str
    decision: str
    content_summary: NotRequired[str]


def record_kind(worktree: Path, rel: str) -> str | None:
    """notes, reviews, or images (or mixed) for an ignored path whose files are all work records and none
    tracked; None for anything else."""
    files = files_in(worktree / rel)
    kinds = set()
    for file in files:
        inside = file.relative_to(worktree).as_posix()
        kind = next((kind for top, kind in RECORD_KINDS.items() if within(inside, top)), None)
        if kind is None and not file.is_symlink() and file.suffix.lower() in IMAGE_SUFFIXES:
            kind = "images"
        if kind is None:
            return None
        kinds.add(kind)
    if not kinds or git(worktree, "ls-files", "--", rel).stdout.strip():
        return None
    return kinds.pop() if len(kinds) == 1 else "mixed"


def held_details(source: Path) -> str:
    size = "不明"
    try:
        usage = subprocess.run(["du", "-sh", str(source.absolute())], capture_output=True, text=True, timeout=10)
        if usage.returncode == 0 and usage.stdout.strip():
            size = usage.stdout.split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    if source.is_dir() and not source.is_symlink():
        names = sorted(child.name for child in source.iterdir())[:3]
    else:
        names = [source.name]
    return f"du {size}、先頭 {len(names)} 件: {', '.join(names) or 'なし'}"


def held_file(source: Path, rel: str, in_main: bool, keys: list[str] | None) -> Held:
    """A setting, data, or unknown path that keeps the worktree, with why and what the lead decides; a .env
    names the keys its .env.example lacks, never their values."""
    where = "main と内容が違います" if in_main else "main にありません"
    clones: dict[Path, Path] = {}
    settings_by_clone: dict[Path, list[str]] = {}
    files: list[Path] = []
    if source.is_dir() and not source.is_symlink():
        for folder, children, names in os.walk(source):
            directory = Path(folder)
            if ".git" in children:
                children.remove(".git")
            if (directories := git_directories(directory)) and directories[0] == directories[1]:
                clones[directory] = directories[0]
            clone = next((parent for parent in (directory, *directory.parents) if parent in clones), None)
            if clone is None:
                files.extend(directory / name for name in names)
                files.extend(directory / name for name in children if (directory / name).is_symlink())
            else:
                settings_by_clone.setdefault(clone, []).extend(
                    (directory / name).relative_to(clone).as_posix()
                    for name in names if name.startswith(".env") and not name.endswith(".example"))
    else:
        files.append(source)
    if clones:
        kind = "clone"
        reason = "。".join(
            f"{PurePosixPath(rel) / clone.relative_to(source)}: "
            f"{', '.join(held_clone_details(clone, clones[clone], settings_by_clone.get(clone, [])))}"
            for clone in sorted(clones))
        decision = "変更、未 push の commit、stash の保存先を確かめてください"
        if files:
            settings = [file.relative_to(source).as_posix() for file in files
                        if file.name.startswith(".env") or ".local." in file.name]
            data = [file.relative_to(source).as_posix() for file in files if file.suffix.lower() in DATA_SUFFIXES]
            if settings:
                reason += "。clone 外の設定: " + ", ".join(sorted(settings)[:3])
            if data:
                reason += "。clone 外のデータ: " + ", ".join(sorted(data)[:3])
            decision += "。clone 外のファイルも残すか決めてください"
    elif any(file.name.startswith(".env") or ".local." in file.name for file in files):
        kind = "setting"
        reason = f"作業場だけの設定です（{where}）"
        if keys:
            reason += "。.env.example にない値があります: " + ", ".join(keys)
            decision = "値を main の .env か秘密の置き場へ移すか、捨ててよいかを決めてください"
        else:
            decision = "main へ写すか、捨ててよいかを決めてください"
    elif all(file.suffix.lower() in DATA_SUFFIXES for file in files):
        kind = "data"
        reason = f"作業場で作ったデータです（{where}）"
        decision = "残す場所を決めて移すか、捨ててよいかを決めてください"
    else:
        kind = "unknown"
        reason = f"再生成する元も用途も分かりません（{where}）"
        decision = "中身を開き、何のファイルかを確かめて、残すか捨てるかを決めてください"
    content = held_details(source)
    return {"path": rel, "kind": kind, "reason": reason, "decision": decision,
            "content_summary": content}


def held_clone_details(clone: Path, gitdir: Path, settings: list[str]) -> list[str]:
    local_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG")}
    local_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_LAZY_FETCH="1")
    branch = git(clone, "symbolic-ref", "--short", "-q", "HEAD", env=local_env).stdout.strip()
    details = [f"branch {branch or '(detached HEAD)'}"]
    config = git(clone, "config", "--no-includes", "--name-only", "--list", env=local_env)
    if config.returncode or any(key.startswith(("include.", "includeif.", "filter."))
                                for key in config.stdout.lower().splitlines()):
        details.append("未 commit の変更は未確認（外部プログラムを参照する Git 設定）")
    else:
        status = git(clone, "-c", "core.fsmonitor=false", "--no-optional-locks",
                     "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all", env=local_env)
        staged = git(clone, "diff", "--cached", "--raw", "--ignore-submodules=none", "--no-ext-diff",
                     "--no-textconv", env=local_env)
        dirty = status.returncode or staged.returncode or status.stdout.strip() or staged.stdout.strip()
        details.append("uncommitted changes or git status failed" if dirty else "uncommitted changes: none")
    if settings:
        details.append("clone 内の設定: " + ", ".join(sorted(settings)[:3])
                       + (f" ほか {len(settings) - 3} 件" if len(settings) > 3 else ""))
    index = git(clone, "ls-files", "--stage", "-z", env=local_env)
    submodules = [name for entry in index.stdout.split("\0") if entry.startswith("160000 ")
                  and (name := entry.partition("\t")[2]) and (clone / name / ".git").exists()]
    if submodules or index.returncode:
        details.append("submodule は未確認" + (": " + ", ".join(submodules[:3]) if submodules else ""))
    modules = gitdir / "modules"
    if modules.is_dir() and any(modules.iterdir()):
        details.append("submodule のリポジトリが残っています")
    stash = git(clone, "rev-parse", "--verify", "--quiet", "refs/stash", env=local_env)
    details.append("stash remains" if stash.returncode == 0 else "stash: none" if stash.returncode == 1
                   else "stash: 未確認")
    worktrees = git(clone, "worktree", "list", "--porcelain", "-z", env=local_env)
    linked = []
    for entry in worktrees.stdout.split("\0\0"):
        fields = entry.split("\0")
        if not fields or not fields[0].startswith("worktree "):
            continue
        path = fields[0].removeprefix("worktree ")
        if Path(path).resolve() != clone.resolve():
            linked.append((path, next((line.removeprefix("HEAD ") for line in fields if line.startswith("HEAD ")), "")))
    if linked:
        details.append("linked worktrees: " + ", ".join(path for path, _ in linked))
    elif worktrees.returncode:
        details.append("linked worktree は未確認")
    commits = git(clone, "rev-list", "--max-count=11", "HEAD", "--branches", "--tags",
                  *(oid for _, oid in linked if oid), "--not", "--remotes", env=local_env)
    short = [sha[:12] for sha in commits.stdout.splitlines()]
    unsaved = ", ".join(short[:10] + (["…"] if len(short) > 10 else []))
    details.append("remote-tracking refs に無い commit: " +
                   ("未確認" if commits.returncode else unsaved or "なし"))
    details.append("remote の現在の状態は未確認")
    return details


class IgnoredPaths(NamedTuple):
    review: list[str]
    regenerable: list[str]
    nested: list[Path]
    # Why paths counted as regenerable for what they hold: a directory of logs (its size and last write), and a
    # tool's output (the tool and the ignore line that excludes it).
    notes: list[str]
    # The live PIDs that hold the worktree's scheduled tasks lock.
    lock_holders: list[int]
    # The paths of `review` split by what they hold: work records --archive-scratch moves aside, and the rest.
    records: list[Record]
    held: list[Held]
    error: str | None = None


def dependency_clones(worktree: Path, rel: str, linked: Sequence[Path] = ()) -> list[Path]:
    """Standalone clones at an untracked/ignored local-deps path containing nothing else."""
    path = PurePosixPath(rel)
    if not path.parts or path.parts[0] != "local-deps" or len(path.parts) > 2:
        return []
    source = worktree / rel
    if source.is_symlink() or not source.is_dir():
        return []
    copies = sorted(set(source.iterdir()) - set(linked)) if len(path.parts) == 1 else [source]
    return copies if copies and all(not copy.is_symlink() and (copy / ".git").is_dir()
                                   and not (copy / ".git").is_symlink() for copy in copies) else []


def nested_dependencies(worktree: Path, rel: str) -> tuple[list[Path], list[Path]]:
    source = worktree / rel
    linked = nested_worktrees(source)
    if linked == [source]:
        return [], linked
    clones = dependency_clones(worktree, rel, linked)
    if linked and set(source.iterdir()) != set(linked + clones):
        return [], []
    return clones, linked


class CloneResult(NamedTuple):
    blockers: list[str]
    details: list[str]


@dataclass(frozen=True)
class NestedRestorePlan:
    head: str
    index: Path
    index_stamp: list[int]
    files: dict[str, list[int] | None]

    def matches(self, copy: Path) -> bool:
        try:
            if (file_stamp(self.index) != self.index_stamp
                    or any(optional_stamp(copy / rel) != stamp for rel, stamp in self.files.items())):
                return False
            head = git(copy, "rev-parse", "--verify", "HEAD")
            return head.returncode == 0 and head.stdout.strip() == self.head
        except OSError:
            return False


@dataclass
class NestedRestore:
    root: Path
    requested: set[Path]
    discard: frozenset[Path] = frozenset()
    since: datetime | None = None
    already_missing: frozenset[Path] = frozenset()
    plans: dict[Path, NestedRestorePlan] = field(default_factory=dict)
    found: set[Path] = field(default_factory=set)

    def allows_changes(self, copy: Path) -> bool:
        if copy not in self.requested or copy.resolve() != copy or git_directories(copy) is None:
            return False
        self.found.add(copy)
        head = git(copy, "rev-parse", "--verify", "HEAD")
        status = git(copy, "--no-optional-locks", "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all")
        tracked = git(copy, "ls-tree", "-rz", "HEAD")
        index = git(copy, "rev-parse", "--path-format=absolute", "--git-path", "index")
        if head.returncode or status.returncode or tracked.returncode or index.returncode:
            return False
        entries = {rel: info.split()[0] for item in tracked.stdout.split("\0") if item
                   for info, _, rel in (item.partition("\t"),)}
        cutoff = self.since.timestamp() * 1e9 if self.since else None
        changes: dict[str, list[int] | None] = {}
        listings: dict[Path, dict[str, Path]] = {}
        for item in filter(None, status.stdout.split("\0")):
            rel = item[3:]
            path = copy / rel
            if item.startswith("?? "):
                if self.discard:
                    try:
                        if disk_path(copy, Path(rel), listings) in self.discard:
                            continue
                    except OSError:
                        pass
                return False
            if (entries.get(rel) not in {"100644", "100755", "120000"}
                    or (item.startswith("D ") and (path.exists() or path.is_symlink()))
                    or path.parent.resolve() != path.parent or (path.is_dir() and not path.is_symlink())
                    or any(parent.exists() and not parent.is_dir()
                           for parent in path.parents if parent.is_relative_to(copy))):
                return False
            try:
                stamp = optional_stamp(path)
                last_change = stamp if stamp is not None else file_stamp(next(parent for parent in path.parents if parent.exists()))
            except OSError:
                return False
            if (cutoff is not None and max(last_change[3:]) > cutoff
                    and not (stamp is None and path in self.already_missing)):
                return False
            changes[rel] = stamp
        if changes:
            index_path = Path(index.stdout.strip())
            try:
                stamp = file_stamp(index_path)
            except OSError:
                return False
            if cutoff is not None and max(stamp[3:]) > cutoff:
                return False
            self.plans[copy] = NestedRestorePlan(head.stdout.strip(), index_path, stamp, changes)
        return True

    def display(self) -> dict[str, list[str]]:
        return {str(copy.relative_to(self.root)): list(plan.files) for copy, plan in self.plans.items()}


def clone_has_local_changes(copy: Path, status: str, index: str) -> bool:
    changes = [entry for entry in status.split("\0") if entry]
    if not changes or any(not entry.startswith("?? ") for entry in changes):
        return bool(changes)
    tracked: dict[str, list[tuple[Path, str, str]]] = {}
    for entry in index.split("\0"):
        metadata, _, name = entry.partition("\t")
        if metadata.startswith(("100644 ", "100755 ")):
            mode, blob, _ = metadata.split()
            tracked.setdefault(filename_key(name), []).append((Path(name), mode, blob))
    listings: dict[Path, dict[str, Path]] = {}
    for entry in changes:
        name = entry[3:]
        aliases = tracked.get(filename_key(name))
        if not aliases:
            return True
        try:
            actual = disk_path(copy, Path(name), listings)
            if actual.is_symlink() or not actual.is_file():
                return True
            mode = "100755" if actual.stat().st_mode & 0o100 else "100644"
            expected = {blob for path, tracked_mode, blob in aliases
                        if tracked_mode == mode and disk_path(copy, path, listings) == actual}
            if not expected:
                return True
            hashed = git(copy, "hash-object", "--no-filters", "--", str(actual))
            if hashed.returncode or hashed.stdout.strip() not in expected:
                return True
        except OSError:
            return True
    return False


def clone_check(copy: Path, checked: dict[Path, CloneResult], restore: NestedRestore | None = None) -> CloneResult:
    """Reasons a dependency clone cannot be discarded, checking its actual origin branches and tags."""
    copy = copy.resolve()
    if copy in checked:
        return checked[copy]
    refs = origin_refs(copy, branches_and_tags=True)
    blockers = []
    branch = git(copy, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    details = [f"branch {branch or '(detached HEAD)'}"]
    stash = git(copy, "stash", "list")
    if stash.returncode or stash.stdout.strip():
        blockers.append("stash remains or could not be checked")
    else:
        details.append("stash: none")
    discard = restore.discard if restore and copy in restore.requested else frozenset()
    ignored = ignored_paths(copy, None, clone_checks=checked, discard=discard)
    if ignored.error:
        blockers.append(ignored.error)
    if ignored.review or ignored.nested or ignored.lock_holders:
        blockers.append("ignored paths or live locks to preserve: " + ", ".join(
            [*ignored.review, *(str(path) for path in ignored.nested), *map(str, ignored.lock_holders)]))
    worktrees = git(copy, "worktree", "list", "--porcelain")
    linked = [line.removeprefix("worktree ") for line in worktrees.stdout.splitlines()
              if line.startswith("worktree ") and Path(line.removeprefix("worktree ")).resolve() != copy.resolve()]
    if worktrees.returncode or linked:
        blockers.append("linked worktrees remain or could not be checked: " + ", ".join(linked))
    entries = git(copy, "ls-files", "--stage", "-z")
    initialized = [rel for entry in entries.stdout.split("\0") if entry.startswith("160000 ")
                   and (rel := entry.partition("\t")[2]) and (copy / rel / ".git").exists()]
    modules = copy / ".git/modules"
    if entries.returncode or initialized or (modules.is_dir() and any(modules.iterdir())):
        blockers.append("submodule repositories need preservation: " + ", ".join(initialized or [str(modules)]))
    local = git(copy, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags")
    if local.returncode:
        blockers.append("local branches and tags could not be checked")
    tips = [("HEAD", git(copy, "rev-parse", "--verify", "HEAD").stdout.strip()),
            *(line.split(" ", 1) for line in local.stdout.splitlines())]
    unsaved = [(name, oid) for name, oid in tips if not oid or refs is None or not remote_contains(copy, oid, refs)]
    if unsaved:
        blockers.append("local refs are on no verified remote branch or tag: " + ", ".join(name for name, _ in unsaved))
        details.append("local tips: " + ", ".join(f"{name} {oid[:12] or '?'}" for name, oid in unsaved))
    else:
        details.append("commits absent from origin: none")
    status = git(copy, "--no-optional-locks", "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all")
    approved = restore is not None and restore.allows_changes(copy)
    if status.returncode or (not approved and clone_has_local_changes(copy, status.stdout, entries.stdout)):
        blockers.append("uncommitted changes or git status failed")
    elif restore and copy in restore.plans:
        details.append("uncommitted changes: approved for restoration")
    else:
        details.append("uncommitted changes: none")
    result = CloneResult(blockers, [*details, *blockers])
    checked[copy] = result
    return result


def ignored_paths(worktree: str | Path, main: str | Path | None, *,
                  clone_checks: dict[Path, CloneResult] | None = None,
                  restore: NestedRestore | None = None, discard: frozenset[Path] = frozenset()) -> IgnoredPaths:
    """(review, regenerable, nested, notes, lock_holders, records, held): ignored paths that hold something and are
    neither caches nor copies of the main checkout, split into those that keep the worktree and those a setup or
    test run writes again, the worktrees of other repositories found at them (such as `local-deps/<repo>`), the
    live holder of a scheduled tasks lock, which is not a path to open, and the paths that keep the worktree
    again, split into work records and files held for a decision.

    `--ignored=matching` names each path an ignore pattern matched, so a generated
    directory deep inside an otherwise untracked tree is seen by its own path.
    """
    review, regenerable, nested, notes, holders = [], [], [], [], []
    if clone_checks is None:
        clone_checks = {}
    records: list[Record] = []
    held: list[Held] = []
    root = Path(worktree).resolve()
    cache_tags: dict[Path, bool] = {}
    listings: dict[Path, dict[str, Path]] = {}
    # -z gives each path unquoted, so a Japanese or spaced name is read as written.
    status = git(worktree, "--no-optional-locks", "status", "--porcelain", "-z", "--ignored=matching", "--untracked-files=normal")
    if status.returncode:
        return IgnoredPaths(review, regenerable, nested, notes, holders, records, held,
                            f"ignored paths could not be checked (Git exit {status.returncode})")
    for entry in status.stdout.split("\0"):
        if not entry.startswith("!! ") or is_cache(entry[3:]):
            continue
        rel = entry[3:]
        source, copy = Path(worktree) / rel, Path(main) / rel if main is not None else None
        if discard:
            try:
                actual = disk_path(root, Path(rel), listings)
            except OSError:
                actual = source
            if any(path.is_relative_to(actual) for path in discard) and all(
                    path in discard or holds_nothing(path) for path in files_in(actual)):
                continue
        if holds_nothing(source) or (copy is not None and same_tree(source, copy)):
            continue
        if rel == SCHEDULED_LOCK and not source.is_symlink() and (holder := lock_holder(source)) is not None:
            if holder.running:
                holders.append(holder.pid)
            else:
                regenerable.append(rel)
            continue
        clones, found = nested_dependencies(Path(worktree), rel)
        if found:
            nested.extend(found)
            if not clones:
                continue
        if clones:
            for clone in clones:
                examined = clone_check(clone, clone_checks, restore)
                if examined.blockers:
                    review.append("%s (%s)" % (clone, "; ".join(examined.blockers)))
                    content = held_details(clone)
                    held.append({"path": str(clone), "kind": "clone",
                                 "reason": "。".join(examined.details), "content_summary": content,
                                 "decision": "変更と stash を保存し、HEAD が remote にあることを確かめてください"})
                else:
                    regenerable.append(str(clone.relative_to(worktree)))
                    if not restore or clone not in restore.plans:
                        notes.append("%s: disposable clone; clean, no stash, HEAD on a verified remote branch or tag" % clone)
            continue
        ancestors = (root / rel, *(root / rel).parents)
        tagged = False
        for directory in ancestors:
            if directory == root:
                break
            if directory not in cache_tags:
                cache_tags[directory] = has_cache_tag(directory) and directory.resolve() == directory
            if cache_tags[directory]:
                tagged = True
                break
        if tagged:
            regenerable.append(rel)
            notes.append(f"{rel}: cache (CACHEDIR.TAG)")
            continue
        if holds_only_regenerable(source, Path(worktree)) or generated(Path(worktree), rel):
            regenerable.append(rel)
            continue
        if (files := log_files(source)) is not None:
            regenerable.append(rel)
            notes.append(log_note(rel, files))
            continue
        if tool := tool_output(Path(worktree), rel):
            regenerable.append(rel)
            notes.append("%s is %s, ignored by %s" % (rel, tool, ignore_source(Path(worktree), rel)))
            continue
        where = "not in main checkout" if copy is None or not copy.exists() else "differs from main checkout"
        keys = None
        if source.name == ".env" and source.is_file():
            keys = env_keys_beyond_example(source)
            if keys == []:
                regenerable.append(rel)
                continue
            if keys:
                where += "; values .env.example does not have: " + ", ".join(keys)
        review.append("%s (%s)" % (rel, where))
        if kind := record_kind(Path(worktree), rel):
            records.append({"path": rel, "kind": kind})
        else:
            held.append(held_file(source, rel, copy is not None and copy.exists(), keys))
    return IgnoredPaths(review, regenerable, nested, notes, holders, records, held)


def origin_refs(worktree: Path, *, branches_and_tags: bool = False) -> list[tuple[str, str]] | None:
    """One live snapshot of origin's refs; None when origin could not be read."""
    try:
        refs = subprocess.run(["git", "-C", str(worktree), "ls-remote",
                               *(["--heads", "--tags"] if branches_and_tags else []), "origin"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [(parts[0], parts[1]) for line in refs.stdout.splitlines()
            if len(parts := line.split("\t")) == 2] if refs.returncode == 0 else None


def remote_contains(worktree: Path, oid: str, refs: Sequence[tuple[str, str]]) -> bool:
    """A local object is on one advertised origin ref, using locally available history for branch ancestry."""
    return any(remote_oid == oid or (name.startswith("refs/heads/")
                                    and git(worktree, "merge-base", "--is-ancestor", oid, remote_oid).returncode == 0)
               for remote_oid, name in refs)


def on_remote(worktree: Path, head: str) -> bool:
    """Whether a remote branch holds HEAD, or (asked over the network) a ref of origin points at it."""
    return (bool(git(worktree, "branch", "-r", "--contains", head).stdout.strip())
            or any(oid == head for oid, _ in origin_refs(worktree) or []))


def linked_restore_alias(copy: Path, paths: Collection[str]) -> str | None:
    config = git(copy, "config", "--bool", "core.precomposeunicode")
    precompose = sys.platform == "darwin" and config.returncode == 0 and config.stdout.strip() == "true"
    listings: dict[Path, dict[str, Path]] = {}
    for relative in paths:
        if precompose and normalize("NFC", relative) != relative:
            return relative
        actual = copy
        for part in Path(relative).parts:
            try:
                actual = disk_entry(actual, part, listings)
            except FileNotFoundError:
                break
            except OSError:
                return relative
            observed = normalize("NFC", actual.name) if precompose else actual.name
            if observed != part:
                return relative
    return None


def nested_check(copy: Path, restore: NestedRestore | None = None) -> dict:
    """Whether a nested worktree can be detached from its repository: clean, nothing ignored to open,
    and HEAD on a remote. `restore` resets recompiled catalogs before `detach` removes it."""
    repository = main_checkout(copy) or ""
    blockers = []
    head = git(copy, "rev-parse", "HEAD").stdout.strip()
    if not head or not on_remote(copy, head):
        blockers.append("HEAD %s is on no remote branch" % head[:8])
    elif git(copy, "rev-parse", "HEAD").stdout.strip() != head:
        blockers.append("HEAD changed during the remote check")
    tracked_changes, untracked, _ = working_changes(copy, all_untracked=True)
    changes = [*tracked_changes, *untracked]
    modified = set(git(copy, "--no-optional-locks", "diff", "HEAD", "--name-only", "--diff-filter=M", "-z").stdout.split("\0"))
    regenerable = [rel for rel in changes if rel.endswith(".mo") and rel in modified
                   and (copy / rel).with_suffix(".po").is_file()
                   and str(Path(rel).with_suffix(".po")) not in changes]
    changes = [rel for rel in changes if rel not in regenerable]
    approved = restore is not None and restore.allows_changes(copy)
    if changes and not approved:
        blockers.append("%d uncommitted changes" % len(changes))
    if restore is not None and copy in restore.requested and not approved:
        blockers.append("--restore-nested の復元条件を満たしません")
    if restore is not None and (plan := restore.plans.get(copy)) and (alias := linked_restore_alias(copy, plan.files)):
        blockers.append(f"--restore-nested の Git とディスクのパス表記が異なります（{alias}）")
    discard = restore.discard if approved and restore else frozenset()
    ignored = ignored_paths(copy, repository, discard=discard)
    if ignored.error:
        blockers.append(ignored.error)
    review, deeper = ignored.review, ignored.nested
    if review or deeper:
        blockers.append("ignored paths to open: " + ", ".join([*review, *(str(d) for d in deeper)]))
    if ignored.lock_holders:
        blockers.append("%s is held by running PID %s" % (SCHEDULED_LOCK, ", ".join(map(str, ignored.lock_holders))))
    return {"path": str(copy), "repository": repository, "head": head[:12], "blockers": blockers,
            "regenerable": regenerable,
            "restore": ["git", "-C", str(copy), "checkout", "HEAD", "--", *regenerable] if regenerable else [],
            "detach": ["git", "-C", repository, "-c", "status.showUntrackedFiles=all", "worktree", "remove", str(copy)]}


def patches_in_base(worktree: str | Path, head: str, base: str) -> list[str] | None:
    """Commits on HEAD outside base whose patch base already has (`git cherry`), or None.

    None when git cannot tell, when any commit's patch is missing from base, or
    when HEAD holds a merge commit: `git cherry` skips merges, so an edit made
    while merging would pass unseen. A commit reverted in base after its patch
    landed still counts as in base.
    """
    merges = git(worktree, "rev-list", "--merges", "%s..%s" % (base, head))
    out = git(worktree, "cherry", base, head)
    if merges.returncode != 0 or merges.stdout.strip() or out.returncode != 0:
        return None
    lines = out.stdout.splitlines()
    if not lines or any(not line.startswith("- ") for line in lines):
        return None
    return [line[2:] for line in lines]


def only_base_merged_after(worktree: str | Path, head: str, pr_head: str, base: str) -> bool:
    """Whether every commit past the merged PR head merges base in and HEAD's tree is base's.

    Both are asked: an own commit counts as work even when its patch reached base, and the tree
    check keeps an edit made while merging. False when git cannot tell (the PR head object is absent).
    """
    past = git(worktree, "rev-list", head, "^" + pr_head, "^" + base)
    own = git(worktree, "rev-list", "--no-merges", head, "^" + pr_head, "^" + base)
    trees = git(worktree, "rev-parse", head + "^{tree}", base + "^{tree}")
    if any(done.returncode != 0 for done in (past, own, trees)):
        return False
    head_tree, base_tree = trees.stdout.split()
    return bool(past.stdout.strip()) and not own.stdout.strip() and head_tree == base_tree


def pr_head_relation(worktree: str | Path, head: str, pr_head: str, head_ahead_of_pr: int | None) -> str:
    """Whether HEAD is in the merged PR head's history; trust compare only if that object is absent."""
    if git(worktree, "cat-file", "-e", pr_head + "^{commit}").returncode != 0:
        if head_ahead_of_pr is not None:
            return "contained" if head_ahead_of_pr == 0 else "ahead"
        return "unknown"
    if git(worktree, "merge-base", "--is-ancestor", head, pr_head).returncode == 0:
        return "contained"
    if git(worktree, "merge-base", "--is-ancestor", pr_head, head).returncode == 0:
        return "ahead"
    return "diverged"


def compose_in(worktree: str | Path, compose_projects: Sequence[ComposeProject]) -> list[dict]:
    """Compose projects (from `docker compose ls --all --format json`) defined inside the worktree."""
    prefix = str(Path(worktree).resolve()) + "/"
    found = []
    for project in compose_projects:
        files = [f for f in (project.get("ConfigFiles") or "").split(",") if Path(f).is_absolute()]
        if any(str(Path(f).resolve()).startswith(prefix) for f in files):
            found.append({"project": project.get("Name"), "status": project.get("Status")})
    return found


def process_cwds(pids: Collection[int]) -> dict[int, str] | None:
    """Current directories by PID, from /proc on Linux or one lsof inventory on macOS."""
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        found = {}
        for pid in pids:
            try:
                found[pid] = str(Path("/proc/%d/cwd" % pid).readlink())
            except OSError:
                continue
        return found
    try:
        output = subprocess.run(["lsof", "-n", "-P", "-Fpn", "-d", "cwd"],
                                capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    found = {}
    pid = None
    for line in output.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
        elif pid is not None and pid in pids and line.startswith("n"):
            found[pid] = line[1:]
    return found


# Orca starts each tab as `login` -> login shell under its terminal host: the relocated
# `orca-terminal-host` or, from older runs, `Orca Helper` running the same daemon-entry.js.
# A tab whose processes are only that shell, or the shell waiting on its setup hooks,
# runs nothing: `orca worktree rm` stops it with the worktree.
ORCA_TERMINAL_HOSTS = ("orca-terminal-host", "/daemon-entry.js")
LOGIN_SHELL = re.compile(r"-?(?:\S*/)?(?:zsh|bash|sh|fish)(?: -l| --login)?")
SETUP_WAITS = ("direnv export ", "direnv exec ", "setup-runner.sh")
TERMINAL_HANDLE = re.compile(r"\bORCA_TERMINAL_HANDLE=(term_[0-9A-Za-z-]+)")


def process_table() -> dict[int, tuple[int, str]] | None:
    """{pid: (ppid, command)} from ps, or None when ps fails."""
    try:
        output = subprocess.run(
            ["ps", "-axww", "-o", "pid=,ppid=,command="],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    processes = {}
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        processes[pid] = (ppid, fields[2])
    return processes


def process_handles(pids: list[int]) -> dict[int, str]:
    """The Orca terminal handle in each process's environment, where the system shows it.

    macOS hides the environment of a process started through the setuid `login`, so a
    tab's own login shell shows none while a program started from it does.
    """
    if not pids:
        return {}
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        found = {}
        for pid in pids:
            try:
                match = TERMINAL_HANDLE.search(Path("/proc/%d/environ" % pid).read_bytes().decode(errors="replace")
                                               .replace("\0", " "))
            except OSError:
                continue
            if match:
                found[pid] = match.group(1)
        return found
    try:
        output = subprocess.run(["ps", "-E", "-ww", "-o", "pid=,command=", "-p", ",".join(map(str, pids))],
                                capture_output=True, text=True).stdout
    except OSError:
        return {}
    lines = (line.strip().split(None, 1) for line in output.splitlines())
    return {int(fields[0]): match.group(1) for fields in lines
            if len(fields) == 2 and fields[0].isdigit() and (match := TERMINAL_HANDLE.search(fields[1]))}


def tab_root(pid: int, table: dict[int, tuple[int, str]]) -> int | None:
    """The process Orca's terminal host started for the tab that runs `pid`, or None outside Orca tabs."""
    seen = set()
    while pid in table and pid not in seen:
        seen.add(pid)
        parent = table[pid][0]
        if parent in table and any(host in table[parent][1] for host in ORCA_TERMINAL_HOSTS):
            return pid
        pid = parent
    return None


def descendants(root: int, table: dict[int, tuple[int, str]]) -> list[int]:
    children = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    found, stack = [], [root]
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, []))
    return found


def runs_nothing(root: int, members: list[int], table: dict[int, tuple[int, str]]) -> bool:
    return all(pid == root or LOGIN_SHELL.fullmatch(table[pid][1].strip())
               or any(wait in table[pid][1] for wait in SETUP_WAITS) for pid in members)


def pipe_ends() -> dict[int, tuple[str | None, str | None]]:
    """{pid: (stdin pipe, stdout pipe's other end)} for this check's process group, the pipes named
    alike on both ends: /proc's pipe:[inode] on Linux, lsof's pipe addresses on macOS. A descriptor
    that is not a pipe (/dev/null, a file, a socket) is None, so it links no reader."""
    group = os.getpgrp()
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        ends = {}
        for proc in Path("/proc").glob("[0-9]*"):
            try:
                if os.getpgid(int(proc.name)) != group:
                    continue
                stdin, stdout = (str((proc / "fd" / fd).readlink()) for fd in ("0", "1"))
                ends[int(proc.name)] = (stdin if stdin.startswith("pipe:") else None,
                                        stdout if stdout.startswith("pipe:") else None)
            except (OSError, ValueError):
                continue
        return ends
    try:
        output = subprocess.run(["lsof", "-n", "-P", "-a", "-g", str(group), "-d", "0,1", "-F", "pftdn"],
                                capture_output=True, text=True).stdout
    except OSError:
        return {}
    ends: dict[int, tuple[str | None, str | None]] = {}
    pid = fd = kind_of_file = None
    for line in output.splitlines():
        kind, value = line[:1], line[1:]
        if kind == "p":
            pid, fd = int(value), None
            ends.setdefault(pid, (None, None))
        elif kind == "f":
            fd, kind_of_file = value, None
        elif kind == "t":
            kind_of_file = value
        elif pid is None or kind_of_file != "PIPE":
            continue
        elif fd == "0" and kind == "d":
            ends[pid] = (value, ends[pid][1])
        elif fd == "1" and kind == "n" and value.startswith("->"):
            ends[pid] = (ends[pid][0], value[2:])
    return ends


def output_readers(processes: dict[int, tuple[int, str]] | None = None) -> set[int]:
    """Readers of this check's output or an ancestor wrapper's output, following only actual pipe connections."""
    if not stat.S_ISFIFO(os.fstat(1).st_mode):
        return set()
    ends = pipe_ends()
    readers: set[int] = set()
    # A batch wrapper captures this check's stdout before printing it into its own pipeline.
    table = processes if processes is not None else process_table() or {}
    ancestors = set()
    pid = os.getpid()
    while pid not in ancestors:
        ancestors.add(pid)
        if pid not in table:
            break
        pid = table[pid][0]
    pipes = {ends[pid][1] for pid in ancestors if pid in ends} - {None}
    while pipes:
        found = {pid for pid, (stdin, _) in ends.items() if stdin in pipes} - readers
        readers |= found
        pipes = {ends[pid][1] for pid in found} - {None}
    return readers


class UsingProcess(TypedDict):
    pid: int
    command: str
    cwd: NotRequired[str | None]
    lock: NotRequired[str]


def processes_using(worktree: str, processes: dict[int, tuple[int, str]], cwds: dict[int, str],
                    readers: set[int], locks: dict[int, str] | None = None) -> list[UsingProcess]:
    """The processes of one table whose command names the worktree, whose current directory is inside it, or
    that hold a lock in it (`locks`, PID to the lock's path), without this process's ancestors and `readers`;
    several worktrees can share the table and directories."""
    path = re.compile(r"(?<![\w./-])" + re.escape(worktree) + r"(?=$|[/\s'\";,):])")
    locks = locks or {}
    ancestors = set()
    pid = os.getpid()
    while pid in processes and pid not in ancestors:
        ancestors.add(pid)
        pid = processes[pid][0]
    found: list[UsingProcess] = []
    for pid, (_, command) in processes.items():
        cwd = cwds.get(pid)
        inside = cwd == worktree or (cwd or "").startswith(worktree + os.sep)
        if pid not in ancestors and pid not in readers and (path.search(command) or inside or pid in locks):
            found.append({"pid": pid, "command": command[:160], **({"cwd": cwd} if inside else {}),
                          **({"lock": locks[pid]} if pid in locks else {})})
    return found


def processes_in(worktree: str, locks: dict[int, str] | None = None) -> list[dict] | None:
    """Processes using the worktree in their command or current directory, or holding a lock in it (`locks`),
    excluding ancestors and the commands reading this check's output.

    Each names its current directory where the system shows it (`cwd`), whether it runs in an Orca tab
    (`in_orca_tab`), the PID Orca's terminal host started for that tab (`tab_root`), the tab's handle when the
    system shows it (`tab`), and whether the tab runs nothing but its shell (`idle_tab`). A process of the tab
    running this check counts as outside a tab: it must not close itself.
    """
    processes = process_table()
    if processes is None:
        return None
    cwds = process_cwds(processes)
    if cwds is None:
        return None
    found: list[UsingProcess] = [{**item, **({"cwd": cwds[item["pid"]]} if item["pid"] in cwds else {})}
             for item in processes_using(worktree, processes, cwds, output_readers(processes), locks)]
    own_tab = tab_root(os.getpid(), processes)
    roots = {item["pid"]: root for item in found
             if (root := tab_root(item["pid"], processes)) is not None and root != own_tab}
    tabs = {}
    for root in set(roots.values()):
        members = descendants(root, processes)
        idle = runs_nothing(root, members, processes)
        handles = {} if idle else process_handles(sorted(members))
        tabs[root] = (idle, next((handles[pid] for pid in sorted(handles)), None))
    return [{**item, "in_orca_tab": item["pid"] in roots,
             "idle_tab": tabs[roots[item["pid"]]][0] if item["pid"] in roots else False,
             "tab": tabs[roots[item["pid"]]][1] if item["pid"] in roots else None,
             **({"tab_root": roots[item["pid"]]} if item["pid"] in roots else {})} for item in found]


def without_ended(processes: list[dict]) -> list[dict]:
    """The processes again after PROCESS_RECHECK_SECONDS when any runs outside Orca tabs, leaving out those
    outside tabs that ended by then."""
    if all(item["in_orca_tab"] for item in processes):
        return processes
    time.sleep(PROCESS_RECHECK_SECONDS)
    return [item for item in processes if item["in_orca_tab"] or running(item["pid"])]


# Written again by an install or a test run; their churn says nothing about who uses the worktree.
UNWATCHED_DIRS = {".git", "node_modules", ".venv"}


def file_stamp(path: Path) -> list[int]:
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def filename_key(name: str) -> str:
    return normalize("NFD", normalize("NFD", name).casefold())


def disk_entry(directory: Path, name: str, listings: dict[Path, dict[str, Path]]) -> Path:
    if directory not in listings:
        listings[directory] = {entry.name: entry for entry in directory.iterdir()}
    entries = listings[directory]
    if name in entries:
        return entries[name]
    path = directory / name
    identity = path.lstat()
    spelling = filename_key(name)
    for entry in entries.values():
        if filename_key(entry.name) == spelling and os.path.samestat(entry.lstat(), identity):
            return entry
    raise FileNotFoundError(path)


def disk_path(root: Path, relative: Path, listings: dict[Path, dict[str, Path]]) -> Path:
    for part in relative.parts:
        root = disk_entry(root, part, listings)
    return root


def optional_stamp(path: Path) -> list[int] | None:
    try:
        return file_stamp(path)
    except FileNotFoundError:
        return None


def moved_since(worktree: Path, since: datetime, restored: dict[str, list[int]]) -> list[str]:
    """Blockers for a worktree whose files (outside UNWATCHED_DIRS) or HEAD moved after `since`."""
    after = since.timestamp()
    moved = []
    for top, dirs, files in os.walk(worktree):
        dirs[:] = [name for name in dirs if name not in UNWATCHED_DIRS]
        for name in files:
            try:
                path = Path(top) / name
                stamp = file_stamp(path)
                latest_ns = max(stamp[3], stamp[4])
                if latest_ns > after * 1e9 and restored.get(str(path.relative_to(worktree))) != stamp:
                    moved.append((latest_ns, path))
            except OSError:
                continue
    blockers = []
    if moved:
        latest_ns, latest = max(moved)
        blockers.append("走査（%s）の後に作業場のファイルが %d 個変わりました（最新 %s の %s）。別の担当が使っているかもしれません"
                        % (since.isoformat(timespec="seconds"), len(moved),
                           datetime.fromtimestamp(latest_ns / 1e9, timezone.utc).isoformat(timespec="seconds"),
                           latest.relative_to(worktree)))
    # The reflog dates every move of HEAD (commit, checkout, reset); the commit date stands in without one.
    moves = git(worktree, "log", "-g", "-1", "--format=%gd", "--date=unix", "HEAD").stdout.strip()
    stamp = re.search(r"@\{(\d+)\}", moves)
    moved_at = int(stamp.group(1)) if stamp else int(git(worktree, "log", "-1", "--format=%ct", "HEAD").stdout.strip() or 0)
    if moved_at > after:
        blockers.append("走査（%s）の後に HEAD が動きました（%s）。別の担当が使っているかもしれません"
                        % (since.isoformat(timespec="seconds"),
                           datetime.fromtimestamp(moved_at, timezone.utc).isoformat(timespec="seconds")))
    return blockers


def check(worktree: str | Path, main: str | Path, base: str | None, compose_projects: Sequence[ComposeProject], pr: dict | None = None, pr_head: str | None = None, head_ahead_of_pr: int | None = None,
          *, decided: tuple[str, ...] = (), since: datetime | None = None, scan_file: Path | None = None,
          restore: NestedRestore | None = None, restored: dict[str, list[int]] | None = None) -> dict:
    """Return the removal verdict, including containers and processes using `worktree`.

    `decided` are blockers main found before the check: an Orca comment to keep it, an unanswered docker.
    `since` is when the scan that listed the worktree ran: work after it means someone uses the worktree.
    """
    blockers = list(decided)
    if Path(worktree).resolve() == Path(main).resolve():
        blockers.append("this is the repository's main checkout: close its tabs and keep it")
    tracked_changes, untracked, bytecode = working_changes(worktree)
    clone_notes = []
    clone_checks: dict[Path, CloneResult] = {}
    untracked_nested: list[Path] = []
    for rel in untracked[:]:
        if PurePosixPath(rel).parts[:1] != ("local-deps",):
            continue
        clones, linked = nested_dependencies(Path(worktree), rel)
        if clones or linked:
            if tracked(Path(worktree), PurePosixPath(rel)):
                continue
            checked = [(clone, clone_check(clone, clone_checks, restore).blockers) for clone in clones]
            for clone, problems in checked:
                if problems:
                    blockers.append("%s (%s)" % (clone, "; ".join(problems)))
                elif not restore or clone not in restore.plans:
                    clone_notes.append("%s: disposable clone; clean, no stash, HEAD on a verified remote branch or tag" % clone)
            if all(not problems for _, problems in checked):
                untracked.remove(rel)
                bytecode.append(rel)
                untracked_nested.extend(linked)
    changes = [*tracked_changes, *untracked]
    if changes:
        blockers.append("%d uncommitted changes" % len(changes))
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    merged_by_patch = []
    merge_notes = []
    open_pr_at_head = (pr or {}).get("state") == "OPEN" and (pr or {}).get("headRefOid") == head
    if not open_pr_at_head and (base is None or git(worktree, "merge-base", "--is-ancestor", head, base).returncode != 0):
        merged_head = pr_head or ((pr or {}).get("headRefOid") if (pr or {}).get("state") == "MERGED" else None)
        relation = pr_head_relation(worktree, head, merged_head, head_ahead_of_pr) if merged_head else None
        # Do not let an equivalent patch hide work committed after the PR head.
        if relation == "ahead":
            assert merged_head is not None
            if base and only_base_merged_after(worktree, head, merged_head, base):
                merge_notes.append("HEAD %s is merged PR head %s plus merges of %s, and its tree equals %s"
                                   % (head[:8], merged_head[:8], base, base))
            else:
                blockers.append("HEAD %s is ahead of merged PR head %s" % (head[:8], merged_head[:8]))
        elif relation != "contained":
            # A commit rebased before its PR merged has its patch in base under another hash.
            merged_by_patch = (patches_in_base(worktree, head, base) or []) if base else []
            if merged_by_patch:
                pass
            elif relation == "diverged":
                assert merged_head is not None
                blockers.append("HEAD %s diverged from merged PR head %s" % (head[:8], merged_head[:8]))
            elif relation == "unknown":
                assert merged_head is not None
                blockers.append("HEAD %s cannot be compared with merged PR head %s" % (head[:8], merged_head[:8]))
            elif base is None:
                blockers.append("no base ref found (%s); pass --base" % ", ".join((*BASE_CANDIDATES, *LOCAL_BASES)))
            elif not pr:
                blockers.append("HEAD %s is not in %s" % (head[:8], base))
            elif pr.get("headRefOid") != head or pr.get("state") != "MERGED":
                blockers.append("HEAD %s is not in %s and PR #%s is %s"
                                % (head[:8], base, pr.get("number"), pr.get("state")))
    review, regenerable, nested, notes, lock_holders, records, held_files, ignore_error = ignored_paths(
        worktree, main, clone_checks=clone_checks, restore=restore)
    if ignore_error:
        blockers.append(ignore_error)
    notes.extend(clone_notes)
    notes.extend(merge_notes)
    nested = [nested_check(copy, restore) for copy in dict.fromkeys([*untracked_nested, *nested])]
    ready = [item["path"] for item in nested if not item["blockers"]]
    held = ["%s (%s)" % (item["path"], "; ".join(item["blockers"])) for item in nested if item["blockers"]]
    if ready:
        blockers.append(NESTED_READY + "%s. Pass --detach-nested to remove them "
                        "from their repositories first, so none is left as a prunable record" % ", ".join(ready))
    if held:
        blockers.append("worktrees of other repositories inside cannot be detached: " + ", ".join(held))
    processes = processes_in(str(Path(worktree).resolve()), {pid: SCHEDULED_LOCK for pid in lock_holders})
    if processes is None:
        blockers.append("processes could not be checked; check running processes before removing the worktree")
    else:
        processes = without_ended(processes)
        outside = ["PID %d（%s、cwd %s）" % (item["pid"], item["command"], item.get("cwd") or "不明")
                   for item in processes if not item["in_orca_tab"]]
        busy = [item for item in processes if item["in_orca_tab"] and not item["idle_tab"]]
        if outside:
            blockers.append("作業場を使うプロセスがあります（Orca のタブの外）。%sを止めてから消してください。"
                            "起動に使った zsh のラッパーではなく、node などの本体の PID を止めてください。"
                            % "、".join(outside))
        if busy:
            closes = sorted({shlex.join([str(CLOSE_TABS), "--from", str(scan_file) if scan_file else "SCAN_JSON", item["tab"]])
                             for item in busy if item["tab"]})
            unknown = [str(item["pid"]) for item in busy if not item["tab"]]
            blockers.append("Orca のタブで作業場を使うプロセスがあります。scan の closable にあるタブなら、保存した走査の "
                            + ("JSON を SCAN_JSON に渡して" if scan_file is None else "JSON を使って")
                            + "閉じてから（走査の後に動いたタブは閉じません）、"
                            "rm_check をやり直してください: " + "; ".join(closes)
                            + ("。handle を読めないタブの PID %s は orca terminal list --worktree path:%s --json "
                               "で探してください" % (", ".join(unknown), worktree) if unknown else ""))
    if restore:
        missing = restore.requested - restore.found
        blockers.extend(f"--restore-nested の {copy.relative_to(restore.root)} を入れ子の Git リポジトリとして確認できません"
                        for copy in sorted(missing) if copy.exists() or copy.is_symlink())
    if since is not None:
        blockers.extend(moved_since(Path(worktree), since, restored or {}))
    return {"ok": not blockers and not review, "blockers": blockers, "uncommitted": changes, "review": review,
            "records": records, "held": held_files,
            "regenerable": bytecode + regenerable, "merged_by_patch": merged_by_patch, "nested_worktrees": nested,
            "containers": compose_in(worktree, compose_projects), "processes": processes or [], "notes": notes}


def main_checkout(worktree: str | Path) -> str | None:
    for line in git(worktree, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):]
    return None


BASE_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master")
# With none of those (a repository with no remote), work merges into the branch the main checkout keeps.
LOCAL_BASES = ("main", "master")


def ref_exists(worktree: str | Path, ref: str) -> bool:
    return git(worktree, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def default_base(worktree: str | Path) -> str | None:
    """The first existing remote default branch, or None when the repository has none."""
    for ref in BASE_CANDIDATES:
        if ref_exists(worktree, ref):
            return ref
    return None


def local_base(worktree: str | Path) -> str | None:
    """The first local main or master branch, for a repository with no remote default branch."""
    return next((name for name in LOCAL_BASES if ref_exists(worktree, "refs/heads/" + name)), None)


def remote_identity(url: str) -> tuple[str, str]:
    """Match FETCH_HEAD's display URL to a remote across HTTPS/SSH URL changes."""
    if "://" in url:
        parsed = urlsplit(url)
        host, path = parsed.hostname or "", parsed.path
    elif ":" in url and not url.startswith("/"):
        authority, path = url.split(":", 1)
        host = authority.rpartition("@")[2]
    else:
        host, path = "", url
    return host.lower(), path.strip("/").removesuffix(".git")


def base_remote(worktree: str | Path, base: str | None) -> str | None:
    """The remote a remote-tracking base belongs to, when the repository has that remote."""
    remote, separator, _ = (base or "").removeprefix("refs/remotes/").partition("/")
    if not separator or git(worktree, "remote", "get-url", remote).returncode != 0:
        return None
    return remote


def last_fetch(worktree: str | Path, remote: str) -> datetime | None:
    """When any worktree of the repository last fetched `remote`, read from the FETCH_HEAD files; None if never."""
    url = git(worktree, "remote", "get-url", remote)
    common = git(worktree, "rev-parse", "--git-common-dir")
    if url.returncode != 0 or common.returncode != 0:
        return None
    remote_url = remote_identity(url.stdout.strip())
    common_dir = (Path(worktree) / common.stdout.strip()).resolve()
    fetch_heads = [common_dir / "FETCH_HEAD", *(common_dir / "worktrees").glob("*/FETCH_HEAD")]
    fetched_at = []
    for path in fetch_heads:
        try:
            with path.open(errors="replace") as handle:
                if any(" of " in line and remote_identity(line.rpartition(" of ")[-1].strip()) == remote_url
                       for line in handle):
                    fetched_at.append(path.stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(max(fetched_at), timezone.utc) if fetched_at else None


def fetch_when_stale(worktree: str | Path, base: str | None) -> str | None:
    """Fetch the base's remote when no worktree fetched it within FETCH_AFTER; a note when that fetch fails."""
    remote = base_remote(worktree, base)
    if remote is None:
        return None
    fetched = last_fetch(worktree, remote)
    if fetched is not None and datetime.now(timezone.utc) - fetched <= FETCH_AFTER:
        return None
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
                   "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh") + " -o BatchMode=yes"}
    try:
        done = subprocess.run(["git", "-C", str(worktree), "fetch", "--quiet", remote], capture_output=True,
                              text=True, timeout=FETCH_TIMEOUT, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        return "git fetch %s failed before the check: %s" % (remote, error)
    if done.returncode != 0:
        return "git fetch %s failed before the check (exit %d): %s" % (
            remote, done.returncode, done.stderr.strip()[-300:])
    return None


def stale_fetch_note(worktree: str | Path, base: str | None, pr_head: str | None, has_pr: bool) -> str | None:
    """Warn when no worktree has fetched the base remote in the last day."""
    remote = base_remote(worktree, base)
    latest = last_fetch(worktree, remote) if remote else None
    if remote is None or latest is None or datetime.now(timezone.utc) - latest <= timedelta(days=1):
        return None
    when = latest.strftime("%Y-%m-%dT%H:%M:%SZ")
    note = "%s の参照は %s の fetch のままです。" % (remote, when)
    if pr_head:
        return note + "渡された --pr-head を使って判定しました。"
    if not has_pr:
        return note + "新しい参照を fetch してから判定し直してください。"
    return note + "merge 済み PR の head を別の Mac で確認し、--pr-head <sha> を渡してください。"


def start_base(worktree: str | Path) -> str | None:
    """The remote branch the checked-out branch was created from, read from its reflog, or None.

    Orca and `git worktree add -b` record "branch: Created from origin/<base>", which names
    where the work merges when that is not the remote's default branch. A branch started
    from its own remote branch (origin/<same name>) is skipped here; when no PR is known
    and HEAD is outside the chosen base, main uses the branch's upstream if it holds HEAD.
    """
    branch = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not branch:
        return None
    entries = git(worktree, "reflog", "show", "--format=%gs", "refs/heads/" + branch).stdout.splitlines()
    created = entries[-1] if entries else ""
    if not created.startswith("branch: Created from "):
        return None
    ref = created[len("branch: Created from "):].strip()
    ref = ref[len("refs/remotes/"):] if ref.startswith("refs/remotes/") else ref
    own = ref.split("/", 1)[-1] == branch
    if own or ref.endswith("/HEAD") or not ref_exists(worktree, "refs/remotes/" + ref):
        return None
    return ref


def merged_pr_head_command(worktree: str | Path, number: int) -> str | None:
    """The merged_pr_head.py command that prints --pr-head and --head-ahead-of-pr on a Mac whose gh works."""
    origin = git(worktree, "remote", "get-url", "origin")
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    host, slug = remote_identity(origin.stdout.strip()) if origin.returncode == 0 else ("", "")
    if host != "github.com" or slug.count("/") != 1 or not head:
        return None
    return ("~/.claude/skills/orca-session-triage/scripts/merged_pr_head.py --repo %s --pr %d --head %s"
            % (slug, number, head))


def pr_base(worktree: str | Path, pr: dict | None) -> str | None:
    """origin/<the PR's base branch> when that ref exists: where the work merges."""
    name = (pr or {}).get("baseRefName")
    ref = "origin/%s" % name if name else None
    return ref if ref and ref_exists(worktree, ref) else None


def pull_request(worktree: str | Path, number: int, *, repo: str | None = None) -> tuple[dict | None, str | None]:
    """Return (pr, error): the PR's number/state/head, or why `gh` could not tell."""
    try:
        out = subprocess.run(["gh", "pr", "view", str(number), "--json", "number,state,headRefOid,baseRefName",
                              *(["--repo", repo] if repo else [])],
                             cwd=worktree, capture_output=True, text=True, check=True, timeout=30).stdout
        return json.loads(out), None
    except subprocess.CalledProcessError as error:
        return None, (error.stderr or "").strip() or str(error)
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        return None, str(error)


def head_in(worktree: str | Path, base: str | None) -> bool:
    return base is not None and git(worktree, "merge-base", "--is-ancestor", "HEAD", base).returncode == 0


def upstream_pr_at_head(worktree: str | Path, upstream: str, number: int | None = None) -> dict | None:
    branch = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if not branch or not head:
        return None
    try:
        result = subprocess.run(["gh", "pr", "list", "--repo", upstream, "--head", branch, "--state", "open",
                                 "--json", "number,state,headRefOid,baseRefName"], cwd=worktree,
                                capture_output=True, text=True, check=True, timeout=30)
        return next((pr for pr in json.loads(result.stdout) if pr.get("state") == "OPEN"
                     and pr.get("headRefOid") == head and (number is None or pr.get("number") == number)), None)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def merged_pr_at_head(worktree: str | Path) -> tuple[dict | None, str | None]:
    """(pr, error): the MERGED PR of the checked-out branch whose head is HEAD, asked of gh for a GitHub origin."""
    origin = git(worktree, "remote", "get-url", "origin")
    branch = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if origin.returncode != 0 or remote_identity(origin.stdout.strip())[0] != "github.com" or not branch:
        return None, None
    try:
        out = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "merged",
                              "--json", "number,state,headRefOid,baseRefName"],
                             cwd=worktree, capture_output=True, text=True, check=True, timeout=30).stdout
        prs = json.loads(out)
    except subprocess.CalledProcessError as error:
        return None, (error.stderr or "").strip() or str(error)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        return None, str(error)
    return next((pr for pr in prs if pr.get("headRefOid") == head), None), None


def within(rel: str, named: str) -> bool:
    """Whether the worktree path `rel` is `named` or lies inside it."""
    path, inside = PurePosixPath(rel.rstrip("/")), PurePosixPath(named)
    return path == inside or inside in path.parents


def untracked_exec_plans(worktree: Path, base: str | None, pr: dict | None) -> tuple[str, ...]:
    """Untracked Markdown exec plans absent from a merged PR's base, with no tracked changes to preserve."""
    if (not pr or pr.get("state") != "MERGED" or not base or not ref_exists(worktree, base)
            or git(worktree, "diff", "--quiet", "HEAD", "--").returncode != 0):
        return ()
    paths = git(worktree, "ls-files", "--others", "--exclude-standard", "-z", "--", "docs/exec-plans/")
    return tuple(rel for rel in paths.stdout.split("\0") if rel and PurePosixPath(rel).suffix == ".md"
                 and (worktree / rel).is_file() and not (worktree / rel).is_symlink()
                 and git(worktree, "cat-file", "-e", f"{base}:{rel}").returncode != 0)


def named_archive_path(worktree: Path, requested: PurePosixPath) -> Path:
    source = worktree
    for part in requested.parts:
        if source.is_symlink() or (source != worktree and (
                (source / ".git").exists() or (source / "HEAD").is_file() and (source / "objects").is_dir())):
            raise ValueError("symlink または別の Git リポジトリの内側です")
        candidate = source / part
        if not (candidate.exists() or candidate.is_symlink()):
            raise ValueError("パスがありません")
        for entry in source.iterdir():
            if entry.name != part and entry.name.casefold() == part.casefold() and entry.samefile(candidate):
                candidate = entry
                break
        source = candidate
    rel = source.relative_to(worktree).as_posix()
    tracked = [git(worktree, "--literal-pathspecs", "ls-files", "-z", "--", rel),
               git(worktree, "--literal-pathspecs", "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", rel)]
    if any(item.returncode or item.stdout for item in tracked):
        raise ValueError("追跡済みのファイルがあるか、確認できません")
    if source.is_dir() and not source.is_symlink():
        for folder, children, files in os.walk(source):
            if ".git" in {name.casefold() for name in (*children, *files)} or (
                    "HEAD" in files and "objects" in children and ("refs" in children or "config" in files)):
                raise ValueError(f"Git リポジトリが含まれています（{Path(folder).relative_to(worktree)}）")
    return source


def archive_movers(worktree: Path, result: dict, movable: tuple[str, ...], *,
                   besides_nested: bool = False, explicit: Sequence[str] = ()) -> list[Path]:
    """The paths of `movable` (scratch directories and --archive-path names) that alone keep the worktree: every
    blocker is an uncommitted change, and every change and path to review lies in one of them or is a directory
    holding nothing outside them. Empty when anything else keeps it.
    `besides_nested` also lets nested worktrees ready to detach stay, for --detach-nested to remove."""
    # A review entry is "<path> (<where>)", and <where> holds no " (".
    paths = [*result["uncommitted"], *(entry.rpartition(" (")[0] for entry in result["review"])]
    others = [blocker for blocker in result["blockers"] if not re.fullmatch(r"\d+ uncommitted changes", blocker)
              and not (besides_nested and blocker.startswith(NESTED_READY))]
    if others or not (paths or explicit):
        return []
    # Only what is on disk moves: a deleted tracked file named here keeps the worktree as a change.
    present = [named for named in movable if (worktree / named).exists() or (worktree / named).is_symlink()]
    movers = set(explicit).intersection(present)
    for rel in paths:
        found = {named for named in present if within(rel, named)}
        holder = worktree / rel
        if not found and holder.is_dir() and not holder.is_symlink() and all(
                any(within(str(child.relative_to(worktree)), named) for named in present)
                for child in holder.rglob("*") if child.is_symlink() or not child.is_dir()):
            found = {named for named in present if within(named, rel)}
        if not found:
            return []
        movers |= found
    # A path inside another that moves goes with it.
    return sorted(worktree / named for named in movers
                  if not any(other != named and within(named, other) for other in movers))


def holds_only_records(result: dict, movable: tuple[str, ...]) -> bool:
    """Whether only paths to review, changes inside the paths that may move, and nested worktrees ready to detach
    keep the worktree, so copying its records to main is a step toward removing it."""
    others = [blocker for blocker in result["blockers"] if not re.fullmatch(r"\d+ uncommitted changes", blocker)
              and not blocker.startswith(NESTED_READY)]
    return not others and all(any(within(rel, named) for named in movable) for rel in result["uncommitted"])


def archive_obstacles(worktree: Path, result: dict, movable: tuple[str, ...]) -> list[str]:
    """Paths outside the archive scope, including siblings inside an ignored parent directory."""
    paths = [*result["uncommitted"], *(entry.rpartition(" (")[0] for entry in result["review"])]
    outside: set[str] = set()
    for rel in paths:
        if any(within(rel, named) for named in movable):
            continue
        holder = worktree / rel
        if holder.is_dir() and not holder.is_symlink() and any(within(named, rel) for named in movable):
            outside.update(str(child.relative_to(worktree)) for child in holder.rglob("*")
                           if (child.is_symlink() or not child.is_dir())
                           and not any(within(str(child.relative_to(worktree)), named) for named in movable))
        else:
            outside.add(rel)
    return sorted(outside)


def reasons(result: dict) -> list[str]:
    """Every reason the worktree stays, in one list: the blockers, with the uncommitted paths after their count,
    then each work record --archive-scratch moves, then each file held for a decision with why and what to
    decide. Empty when nothing keeps it."""
    uncommitted = result.get("uncommitted") or []
    listed = ", ".join(uncommitted[:REASON_PATHS]) + (", …" if len(uncommitted) > REASON_PATHS else "")
    return [*("%s: %s" % (blocker, listed) if uncommitted and re.fullmatch(r"\d+ uncommitted changes", blocker)
              else blocker for blocker in result["blockers"]),
            *("work record to move with --archive-scratch: %s (%s)" % (item["path"], item["kind"])
              for item in result.get("records") or []),
            *(f"判断が要るファイル: {item['path']}（{item['kind']}）— " +
              "。".join(part for part in (item["reason"], item.get("content_summary"), item["decision"]) if part)
              for item in result.get("held") or [])]


def copy_to_main(worktree: Path, main: Path, review: list[str]) -> list[dict[str, str]]:
    """Copy each record under RECORD_DIRS in a path to review that the main checkout also ignores and does
    not have yet to the same place in main, keeping the copy only when cmp finds it identical; a later check
    then finds the path in main. Files main has, symlinks, and records whose directory in main is a symlink
    stay for review."""
    copied = []
    for entry in review:
        rel = entry.rpartition(" (")[0]
        source = worktree / rel
        if (not any(rel.startswith(top) or top.startswith(rel) for top in RECORD_DIRS)
                or git(main, "check-ignore", "-q", "--", rel).returncode != 0):
            continue
        files = [source] if source.is_file() else [child for child in source.rglob("*") if child.is_file()]
        for file in sorted(files):
            rel_file = file.relative_to(worktree)
            target = main / rel_file
            if (not rel_file.as_posix().startswith(RECORD_DIRS) or file.is_symlink()
                    or target.exists() or target.is_symlink()
                    or any((main / parent).is_symlink() for parent in rel_file.parents)):
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, target)
            except OSError:  # a file in main where the record needs a directory: it stays for review
                continue
            if filecmp.cmp(file, target, shallow=False):
                copied.append({"from": str(file), "to": str(target)})
            else:
                target.unlink()
    return copied


def archive_target(archive: Path, worktree: Path, scratch: Path, day: str) -> Path:
    """<archive>/<worktree>-<name>-<day>/, numbered when another worktree of that name archived it that day."""
    name = "%s-%s-%s" % (worktree.name, scratch.name.lstrip("."), day)
    return next(target for target in (archive / name, *(archive / ("%s-%d" % (name, n)) for n in range(2, 1000)))
                if not target.exists())


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def archive(source: Path, target: Path, worktree: Path) -> dict:
    """Copy `source` to `target`, compare every file, record the move in the archive's manifest, then delete
    `source`. A copy that fails or differs is deleted and `source` stays, so nothing is lost on the way."""
    target.parent.mkdir(parents=True, exist_ok=True)
    moved = {"from": str(source), "to": str(target), "verified": False}
    try:
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)
        moved["verified"] = same_tree(source, target)
    except OSError as error:
        moved["error"] = str(error)
    if not moved["verified"]:
        remove_path(target)
        return moved
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "worktree": str(worktree), **moved,
             "files": len(files_in(source))}
    with (target.parent / ARCHIVE_MANIFEST).open("a", encoding="utf-8") as manifest:
        manifest.write(json.dumps(entry, ensure_ascii=False) + "\n")
    remove_path(source)
    return moved


# OrbStack has stopped answering `docker compose ls` for 20 minutes; a removal check must not wait on it.
DOCKER_TIMEOUT = 20


class ComposeProject(TypedDict, total=False):
    Name: str
    Status: str
    ConfigFiles: str


def has_compose_definition(worktree: Path) -> bool:
    """Whether a compose file outside tool caches is in the worktree, tracked, untracked, or ignored."""
    paths = git(worktree, "ls-files", "-z", "--cached", "--others",
                *("--exclude=%s/" % part for part in sorted(CACHE_PARTS)), "--",
                ":(glob)**/compose*.y*ml", ":(glob)**/docker-compose*.y*ml").stdout.split("\0")
    return any(rel and not is_cache(rel) and (worktree / rel).is_file() for rel in paths)


def compose_projects() -> list[ComposeProject] | None:
    """Compose projects docker knows, or None without docker; raises TimeoutExpired when docker does not answer."""
    try:
        out = subprocess.run(["docker", "compose", "ls", "--all", "--format", "json"],
                             capture_output=True, text=True, check=True, timeout=DOCKER_TIMEOUT).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return json.loads(out or "[]")


def listed_in_orca(orca: str, worktree: Path) -> bool | None:
    """Whether a row of this Mac's `orca worktree ps` has `worktree` as its path; None when Orca cannot be read or
    cut the list short. Only the path counts: a row's preview of its tabs' output may name a removed worktree."""
    try:
        done = subprocess.run([orca, "worktree", "ps", "--json"], capture_output=True, text=True, timeout=30,
                              check=True)
        payload = json.loads(done.stdout)
        result = payload.get("result", payload)
        if any(Path(row["path"]).resolve() == worktree for row in result["worktrees"] if row.get("path")):
            return True
        return None if result.get("truncated") else False
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, AttributeError):
        return None


def confirm_gone(worktree: Path, orca: str, wait: float) -> dict:
    """Read Orca and the disk until neither has the worktree, for up to `wait` seconds."""
    started = time.monotonic()
    while True:
        in_orca = listed_in_orca(orca, worktree)
        on_disk = worktree.exists() or worktree.is_symlink()
        waited = time.monotonic() - started
        if (in_orca is False and not on_disk) or waited >= wait:
            return {"worktree": str(worktree), "gone": in_orca is False and not on_disk, "in_orca": in_orca,
                    "on_disk": on_disk, "waited_seconds": round(waited)}
        time.sleep(GONE_POLL_SECONDS)


def print_check(result: dict, summary: bool) -> None:
    if summary:
        details = [*result["reasons"], *result.get("notes", []),
                   *(f"Compose {item['project']} {item['status']}" for item in result.get("containers", []))]
        print(f"{result['worktree']} | ok={str(result['ok']).lower()} | "
              + (" | ".join(" ".join(detail.splitlines()) for detail in details) or "-"))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def topic_check_outputs(directory: Path) -> list[Path]:
    retry = directory / "topic-check.retry.stdout"
    first = directory / "topic-check.stdout"
    if not retry.is_file():
        return [first] if first.is_file() else []
    lines = retry.read_text(encoding="utf-8").splitlines()
    if lines and re.fullmatch(r"照合 .*・読めない 0 件", lines[-1]):
        return [retry]
    return [first, retry] if first.is_file() else [retry]


def scan_topic_blockers(scan_file: Path) -> dict[Path, list[str]]:
    from scan_sessions import saved_rows

    blockers: dict[Path, list[str]] = {}
    outputs = topic_check_outputs(scan_file.expanduser().parent)
    worktrees = {Path(row["path"]) for row in saved_rows(str(scan_file))} if outputs else set()
    for path in outputs:
        for target, reasons in topic_drift_blockers(path, worktrees).items():
            blockers[target] = list(dict.fromkeys((*blockers.get(target, []), *reasons)))
    return blockers


def topic_fixer_record(line: str, path: Path) -> tuple[str, Path, list[str]] | None:
    if not line.startswith(TOPIC_FIXER_JSON):
        return None
    item = json.loads(line.removeprefix(TOPIC_FIXER_JSON))
    if (not isinstance(item, dict) or not TOPIC_PR.fullmatch(str(item.get("pr") or ""))
            or not isinstance(item.get("worktree"), str) or not Path(item["worktree"]).is_absolute()
            or not isinstance(item.get("commands"), list)
            or not all(isinstance(command, str) for command in item["commands"])):
        raise ValueError(f"invalid topic fixer record in {path}")
    return item["pr"], Path(item["worktree"]), item["commands"]


def split_topic_suffix(line: str) -> tuple[str, str]:
    if not line.endswith("）"):
        return line, ""
    depth = 1
    for index in range(len(line) - 2, -1, -1):
        if line[index] == "）":
            depth += 1
        elif line[index] == "（":
            depth -= 1
            if depth == 0:
                return line[:index], line[index + 1:-1]
    return line, ""


def legacy_topic_worktree(display: str, known: Collection[Path] = (), *, require_known: bool = False) -> Path | None:
    named, suffix = split_topic_suffix(display)
    worktree = Path(named)
    if not worktree.is_absolute():
        return None
    candidates = {path for delimiter in re.finditer("（", display)
                  if (path := Path(display[:delimiter.start()])) in known or (path / ".git").exists()}
    unbalanced = named.count("（") != named.count("）")
    if (not TOPIC_STATE.search(suffix) or require_known and candidates != {worktree}
            or (candidates or unbalanced) and worktree not in candidates):
        raise ValueError("cannot identify the topic repair worktree")
    return worktree


def topic_drift_blockers(path: Path, scanned: Collection[Path] = ()) -> dict[Path, list[str]]:
    blockers: dict[Path, list[str]] = {}
    structured: set[str] = set()
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if record := topic_fixer_record(line, path):
            reference, workspace, fixes = record
            fix = "・".join(dict.fromkeys(fixes)) or "修正コマンドの記録なし"
            blockers.setdefault(workspace, []).append(f"topic check で修正担当に指定されています（{reference}、{fix}）")
            structured.add(reference)
    pr = None
    target = None
    has_drift = has_owner = False
    commands: list[str] = []
    for line in [*lines, "-"]:
        if line.startswith(TOPIC_FIXER_JSON):
            continue
        if target and line and line != "-" and not (
                TOPIC_BOUNDARY.fullmatch(line) or line.startswith("  直し方: ")):
            raise ValueError(f"cannot identify the topic repair worktree for {pr} in {path}")
        if line and not line[0].isspace():
            if pr and has_drift and not has_owner:
                raise ValueError(f"cannot identify the topic repair worktree for {pr} in {path}")
            if pr and target and target.is_absolute():
                fix = "・".join(dict.fromkeys(commands)) or "修正コマンドの記録なし"
                blockers.setdefault(target, []).append(f"topic check で修正担当に指定されています（{pr}、{fix}）")
            pr = line if line not in structured and TOPIC_PR.fullmatch(line) else None
            target = None
            has_drift = has_owner = False
            commands = []
        elif pr:
            detail = line.strip()
            has_drift |= detail.startswith(("違い:", "Asana:"))
            if detail.startswith("直し方: "):
                commands.append(detail.removeprefix("直し方: "))
            elif detail.startswith("直す作業場:"):
                display = detail.removeprefix("直す作業場:").strip()
                try:
                    target = legacy_topic_worktree(display, scanned, require_known=True)
                    has_owner = target is not None or display.startswith("なし（")
                    if not has_owner:
                        raise ValueError("cannot identify the topic repair worktree")
                except ValueError as error:
                    raise ValueError(f"{error} for {pr} in {path}") from error
            elif detail.endswith(" で直る）"):
                has_drift = True
                _, suffix = split_topic_suffix(detail)
                if suffix:
                    commands.append(suffix.removesuffix(" で直る"))
    return blockers


def main(argv: list[str] | None = None, worktree_notes: WorktreeNotes = OrcaNotes()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", metavar="PATH", help="worktree path to check, the same as --worktree")
    parser.add_argument("pr_number", nargs="?", metavar="PR",
                        help='PR number, the same as --pr; "-" for none, as rm-check-batch specs write it')
    parser.add_argument("--worktree", help="worktree path to check")
    parser.add_argument("--main", help="main checkout to compare ignored files with (default: first `git worktree list` entry)")
    parser.add_argument("--base", help="ref that merged work must be in (default: the --pr base branch, else the remote branch the worktree started from, else origin/HEAD; without a PR, use the branch's upstream if only it holds HEAD)")
    parser.add_argument("--pr", help="PR number or upstream OWNER/REPO#NUMBER; a MERGED or OPEN PR whose head is HEAD clears the unmerged-commit "
                                               "check. Without it, a MERGED PR of the checked-out branch whose head is "
                                               "HEAD (asked of gh when HEAD is not in the base) counts the same")
    parser.add_argument("--pr-head", help="full SHA of a PR head confirmed MERGED on a host with gh; HEAD may be its ancestor")
    parser.add_argument("--head-ahead-of-pr", type=int, help="ahead_by from comparing <pr-head>...<HEAD> with gh; use when the PR head object is absent here")
    parser.add_argument("--fetch-failed", action="store_true", help="record that git fetch failed before this check; remote refs may be stale")
    parser.add_argument("--since", type=datetime.fromisoformat, metavar="ISO",
                        help="block when the worktree's files (outside %s) or HEAD moved after this time: someone "
                             "else may be using it (default: the scanned_at of --from)" % ", ".join(sorted(UNWATCHED_DIRS)))
    parser.add_argument("--from", dest="from_file", type=Path, metavar="SCAN_JSON",
                        help="a saved scan_sessions.py JSON whose scanned_at is the default --since")
    parser.add_argument("--topic-check", type=Path, metavar="TOPIC_STDOUT",
                        help="topic check --fixers-json output for this run (default: next to --from, if present); "
                             "keep named worktrees")
    parser.add_argument("--no-docker", action="store_true", help="skip the docker compose project lookup")
    parser.add_argument("--decisions-file", type=Path, default=lead_decisions.DEFAULT_LOG,
                        help="the lead's answers in an append-only JSONL log (default: %(default)s)")
    parser.add_argument("--detach-nested", action="store_true",
                        help="when nested worktrees are all that block the removal and each has no changes beyond "
                             "recompiled catalogs, nothing ignored to open, and HEAD on a remote, restore those "
                             "catalogs and remove the copies with git worktree remove, then check again")
    parser.add_argument("--restore-nested", action="append", default=[], metavar="PATH",
                        help="plan restoration of tracked changes in a nested Git repository named by a relative path")
    parser.add_argument("--apply-restore-nested", action="store_true",
                        help="apply the approved --restore-nested paths if preflight permits cleanup, then recheck")
    parser.add_argument("--discard-before-restore", action="append", default=[], metavar="PATH",
                        help="when planning --restore-nested, a file the batch will discard first")
    parser.add_argument("--restore-approval", help=argparse.SUPPRESS)
    parser.add_argument("--missing-restore-file", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--restored-file-stamps", type=json.loads, default={}, help=argparse.SUPPRESS)
    parser.add_argument("--archive-scratch", action="store_true",
                        help="first copy each file to review that the main checkout also ignores and lacks (new "
                             "records under .claude/reviews/) to the same place in main, confirmed with cmp; then "
                             "when %s and the work records listed in `records` (notes, review records such as "
                             ".impeccable/, and folders of only images) alone keep the worktree (nothing else "
                             "blocks and `held` is empty), move them to <archive-dir>/<worktree>-<name>-<YYYYMMDD>/: "
                             "copy, compare every file, append {at, worktree, from, to, verified, files} to "
                             "<archive-dir>/%s, delete the original, then check again; off by default, so move them "
                             "only where the lead's rules say to. With --detach-nested, nested worktrees and these "
                             "records together do not hold each other back" % (" and ".join(SCRATCH_DIRS),
                                                                                ARCHIVE_MANIFEST))
    parser.add_argument("--archive-path", action="append", default=[], metavar="PATH",
                        help="a path inside the worktree, relative to it, such as .tmp/ when the lead's answer to "
                             "remove:<path> names it; repeat for more. Moved like --archive-scratch's folders, to "
                             "<archive-dir>/<worktree>-<last name>-<YYYYMMDD>/, when the check passes or only those paths "
                             "(and with --archive-scratch, those folders) keep the worktree; an ignored directory holding "
                             "nothing beyond a named path inside it counts as that path")
    parser.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR,
                        help="where --archive-scratch and --archive-path move them (default: %(default)s)")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--summary", action="store_true",
                        help="print one line with ok, reasons, notes, and Compose status instead of JSON")
    output.add_argument("--confirm-gone", type=float, nargs="?", const=GONE_WAIT_SECONDS, metavar="SECONDS",
                        help="instead of checking, confirm after `orca worktree rm` that no row of this Mac's "
                             "`orca worktree ps` has the path and the disk lacks it, reading again for up to SECONDS "
                             "(default: %(const)s); print {worktree, gone, in_orca (null when Orca could not be "
                             "read or cut its list short), on_disk, waited_seconds} and exit 1 while it stays")
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable for --confirm-gone (default: $ORCA_CLI_COMMAND or orca)")
    args = parser.parse_args(argv)
    if args.path and args.worktree and Path(args.path).resolve() != Path(args.worktree).resolve():
        parser.error("PATH and --worktree name different worktrees")
    if not (args.path or args.worktree):
        parser.error("give the worktree as PATH or --worktree")
    if args.confirm_gone is not None:
        gone = confirm_gone(Path(args.worktree or args.path).resolve(), args.orca, args.confirm_gone)
        json.dump(gone, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0 if gone["gone"] else 1
    if args.pr_head and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", args.pr_head):
        parser.error("--pr-head must be a full commit SHA")
    if args.head_ahead_of_pr is not None and (not args.pr_head or args.head_ahead_of_pr < 0):
        parser.error("--head-ahead-of-pr needs --pr-head and a non-negative ahead_by")
    named = [PurePosixPath(rel) for rel in args.archive_path]
    if any(path.is_absolute() or not path.parts or ".." in path.parts
           or any(part.casefold() == ".git" for part in path.parts) for path in named):
        parser.error("--archive-path takes a path inside the worktree, relative to it")
    restore_paths = [PurePosixPath(rel) for rel in args.restore_nested]
    discard_paths = [PurePosixPath(rel) for rel in args.discard_before_restore]
    missing_paths = [PurePosixPath(rel) for rel in args.missing_restore_file]
    if any(path.is_absolute() or not path.parts or ".." in path.parts
           or any(part.casefold() == ".git" for part in path.parts) for path in (*restore_paths, *discard_paths, *missing_paths)):
        parser.error("--restore-nested and --discard-before-restore take relative paths inside the worktree")
    if args.apply_restore_nested and not restore_paths:
        parser.error("--apply-restore-nested needs --restore-nested")
    if args.restore_approval and (not args.apply_restore_nested or not args.from_file
                                  or not re.fullmatch(r"[0-9a-f]{64}", args.restore_approval)):
        parser.error("--restore-approval needs --apply-restore-nested, a saved scan and an approval digest")
    if missing_paths and not args.restore_approval:
        parser.error("--missing-restore-file needs --restore-approval")
    if discard_paths and (not restore_paths or args.apply_restore_nested or args.detach_nested or args.archive_scratch or named):
        parser.error("--discard-before-restore can only be used to plan --restore-nested")
    restored = args.restored_file_stamps
    if not isinstance(restored, dict) or not all(isinstance(stamp, list) and len(stamp) == 5
                                                 and all(isinstance(value, int) for value in stamp)
                                                 for stamp in restored.values()):
        parser.error("invalid restored file stamps")
    references = [ref for ref in (args.pr_number, args.pr) if ref not in (None, "-")]
    if len(set(references)) > 1:
        parser.error("positional PR and --pr must agree")
    reference = re.fullmatch(r"(?:([A-Za-z0-9-]+/[A-Za-z0-9._-]+)#)?([1-9]\d*)", references[0]) if references else None
    if references and not reference:
        parser.error("PR must be a number, upstream OWNER/REPO#NUMBER, or -")
    pr_repo, args.pr = (reference[1], int(reference[2])) if reference else (None, None)
    if pr_repo and args.pr_head:
        parser.error("--pr-head is only for a merged PR on origin")

    worktree = str(Path(args.worktree or args.path).resolve())
    inside = git(worktree, "rev-parse", "--is-inside-work-tree")
    gitdir = Path(worktree) / ".git"
    if inside.stdout.strip() != "true" and gitdir.is_file() and Path(
            gitdir.read_text(errors="replace").removeprefix("gitdir:").strip()).is_dir():
        # A gitdir that is there answers on a second try once git's lock or index refresh has passed.
        inside = git(worktree, "rev-parse", "--is-inside-work-tree")
    if inside.stdout.strip() != "true":
        blockers = ["not a git worktree: git rev-parse exited %d: %s"
                    % (inside.returncode, inside.stderr.strip() or "(no stderr)")]
        print_check({"worktree": worktree, "ok": False, "reasons": blockers, "blockers": blockers}, args.summary)
        return 2
    since = args.since
    if since is None and args.from_file:
        rows = json.loads(args.from_file.read_text(encoding="utf-8"))
        scanned = [row["scanned_at"] for row in rows if row.get("scanned_at")]
        since = datetime.fromisoformat(scanned[0]) if scanned else None
    if since is not None and since.tzinfo is None:
        since = since.astimezone()
    checkout = args.main or main_checkout(worktree)
    if checkout is None:
        parser.error("could not locate the main checkout; pass --main")
    main = str(Path(checkout).resolve())
    notes = []
    remote = git(worktree, "remote", "get-url", "upstream")
    upstream_host, upstream_repo = remote_identity(remote.stdout.strip()) if remote.returncode == 0 else ("", "")
    pr = pr_error = None
    if pr_repo and (upstream_host != "github.com" or upstream_repo.casefold() != pr_repo.casefold()):
        parser.error("qualified PR must point at the configured GitHub upstream")
    if upstream_host == "github.com" and not args.pr_head:
        pr = upstream_pr_at_head(worktree, upstream_repo, args.pr if pr_repo else None)
        if pr:
            notes.append(f"PR {upstream_repo}#{pr['number']} is OPEN at HEAD")
    if not pr and args.pr:
        if pr_repo:
            pr_error = f"{pr_repo}#{args.pr} is not an open PR for this branch at HEAD"
        else:
            pr, pr_error = pull_request(worktree, args.pr)
    keep_blocker = None
    try:
        comment = keep_note(worktree_notes.comment(Path(worktree)))
    except NotesUnavailable as error:
        comment = None
        notes.append("worktree note could not be read: %s" % error)
    if comment:
        key = "remove:" + worktree
        import scan_sessions
        approved = False
        try:
            decisions = lead_decisions.latest(lead_decisions.records(args.decisions_file.expanduser()))
            dated = [(lead_decisions.moment(entry.decided_at) or datetime.min.replace(tzinfo=timezone.utc), entry)
                     for name in (key, f"remove:local::{worktree}") if (entry := decisions.get(name))]
            if dated:
                newest_at = max(at for at, _ in dated)
                approved = True
                for at, entry in dated:
                    if at != newest_at:
                        continue
                    terms = entry.removal
                    state = None
                    if terms and (condition := terms.pull_request()):
                        found = (lead_decisions.github_pr_states(condition.repo, [condition.number]) or {}).get(condition.number)
                        state = found.state if found else None
                    fields = asdict(terms) if terms else {}
                    if not scan_sessions.removal_answer_approves({**fields, "answer": entry.answer, "after_state": state}):
                        approved = False
                    notes.append("Lead answered %s: %r" % (entry.decision, entry.answer))
        except (OSError, ValueError) as error:
            approved = False
            notes.append("lead decisions could not be read: %s" % error)
        if not approved:
            keep_blocker = ("Orca comment asks to keep it: %s. Remove it only after the lead approves %s"
                            % (comment, key))
    remote_default = default_base(worktree)
    local_default = None if remote_default else local_base(worktree)
    fallback = remote_default or local_default
    fetch_note = fetch_when_stale(worktree, args.base or start_base(worktree) or fallback)
    if fetch_note:
        notes.append(fetch_note)
    if not pr and not args.pr and not args.pr_head and not head_in(worktree, args.base or start_base(worktree) or fallback):
        pr, lookup_error = merged_pr_at_head(worktree)
        branch = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        if pr:
            notes.append("--pr was not given: PR #%s of branch %s is MERGED at HEAD, so it counts as --pr %s"
                         % (pr.get("number"), branch, pr.get("number")))
        elif lookup_error:
            notes.append("looking up a merged PR of branch %s failed: %s; pass --pr" % (branch, lookup_error))
    by_pr = pr_base(worktree, pr)
    base = args.base or by_pr or start_base(worktree) or fallback
    if not (args.base or args.pr or args.pr_head or pr or head_in(worktree, base)):
        # A branch can track a local branch (branch.<name>.remote=.), which proves nothing was pushed.
        upstream = git(worktree, "rev-parse", "--symbolic-full-name", "@{upstream}").stdout.strip()
        if upstream.startswith("refs/remotes/") and head_in(worktree, upstream):
            base = upstream.removeprefix("refs/remotes/")
            notes.append("Using %s as base: HEAD is pushed to the branch it tracks, and no PR was given or found"
                         % base)
    if local_default and base == local_default and not args.base:
        notes.append("Using %s as base: the repository has no %s, so the local branch the main checkout keeps "
                     "stands in" % (base, ", ".join(BASE_CANDIDATES)))
    decided = [keep_blocker] if keep_blocker else []
    if args.topic_check or args.from_file:
        try:
            drift = (topic_drift_blockers(args.topic_check.expanduser()) if args.topic_check
                     else scan_topic_blockers(args.from_file))
            decided.extend(reason for target, reasons in drift.items()
                           if target.resolve() == Path(worktree) for reason in reasons)
        except (OSError, ValueError) as error:
            decided.append(f"topic check の結果を確認できません（{error}）")
    root = Path(worktree)
    requested_archives: list[Path] = []
    for path in named:
        try:
            requested_archives.append(named_archive_path(root, path))
        except (OSError, ValueError) as error:
            decided.append(f"--archive-path の {path}: {error}")
    explicit = tuple(source.relative_to(root).as_posix() for source in requested_archives)
    movable = (*(SCRATCH_DIRS if args.archive_scratch else ()), *explicit)
    if pr_error and not args.pr_head:
        decided.append("PR #%d lookup failed: %s" % (args.pr, pr_error))
    if pr and args.pr_head and pr.get("headRefOid") != args.pr_head:
        decided.append("--pr-head does not match PR #%d head" % args.pr)
    automation_blockers = []
    try:
        automations = hourly_runtime.existing_workspace_automations(args.orca).get(worktree, [])
        automation_blockers = [f"自動実行『{name}』の作業場" for name in automations]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        automation_blockers = [f"自動実行の作業場を確認できない: {error}"]
    decided.extend(automation_blockers)
    try:
        projects = [] if args.no_docker else compose_projects()
    except subprocess.TimeoutExpired:
        projects = []
        if has_compose_definition(Path(worktree)):
            decided.append("docker が %d 秒応答しません。作業場に compose の定義があり、動いているコンテナを確かめられないので、"
                        "OrbStack か Docker が応答してから rm_check をやり直してください。" % DOCKER_TIMEOUT)
        else:
            notes.append("docker compose ls did not answer in %d seconds; the worktree defines no compose project"
                         % DOCKER_TIMEOUT)
    supplied_head = args.pr_head if not pr or pr.get("state") == "MERGED" else None
    recheck = partial(check, worktree, main, base, projects or [], pr, supplied_head, args.head_ahead_of_pr,
                      decided=tuple(decided), since=since, restored=restored,
                      scan_file=args.from_file.expanduser().absolute() if args.from_file else None)
    restore = (NestedRestore(root, {root / path for path in restore_paths},
                             frozenset(root / path for path in discard_paths), since,
                             frozenset(root / path for path in missing_paths))
               if restore_paths else None)
    result = recheck(restore=restore)

    def movable_now(result: dict) -> tuple[str, ...]:
        """The scratch folders and named paths, and with --archive-scratch the work records this check found."""
        found = (item["path"].rstrip("/") for item in result["records"]) if args.archive_scratch else ()
        plans = untracked_exec_plans(Path(worktree), base, pr) if args.archive_scratch else ()
        return (*movable, *found, *plans)

    def may_detach(nested: list[dict]) -> bool:
        if restore is None:
            return args.detach_nested
        return bool(args.apply_restore_nested and nested
                    and all(Path(item["path"]) in restore.requested for item in nested))

    def restore_approval_error() -> str | None:
        if not args.restore_approval:
            return None
        command = [sys.executable, str(Path(__file__).with_name("scan_sessions.py")),
                   "--from", str(args.from_file.expanduser()), "--decisions-file", str(args.decisions_file.expanduser())]
        try:
            replay = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
            rows = [row for row in json.loads(replay.stdout) if row.get("path") == worktree and not row.get("environment")]
            if len(rows) == 1:
                row = rows[0]
                decision = row.get("removal_decision") or {}
                digest = sha256(json.dumps(decision, sort_keys=True).encode()).hexdigest()
                if (decision.get("allows_removal") is True and digest == args.restore_approval
                        and not any(row.get(key) for key in ("cleanup_blockers", "automation_blocker", "pending_handoffs"))):
                    return None
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            return "--restore-nested の削除承認を復元直前に確認できません"
        return "--restore-nested の削除承認が変わりました。再検査してください"

    def restoration_lock() -> AbstractContextManager[None]:
        if args.restore_approval:
            return lead_decisions.decision_lock(args.decisions_file.expanduser())
        return nullcontext()

    restored_nested: dict[str, list[str]] = {}
    if restore and restore.plans:
        detach = may_detach(result["nested_worktrees"])
        bypassed = [blocker for blocker in result["blockers"] if detach and blocker.startswith(NESTED_READY)]
        other = [blocker for blocker in result["blockers"] if blocker not in bypassed]
        archivable = archive_movers(root, result, movable_now(result), besides_nested=detach, explicit=explicit)
        if args.apply_restore_nested and not result["containers"] and (archivable or (not other and not result["review"])):
            latest = NestedRestore(root, set(restore.plans), since=since, already_missing=restore.already_missing)
            for copy in restore.plans:
                latest.allows_changes(copy)
            with restoration_lock():
                approval_error = restore_approval_error()
                if approval_error:
                    decided.append(approval_error)
                changed = [copy for copy, plan in restore.plans.items() if latest.plans.get(copy) != plan or not plan.matches(copy)]
                for copy in changed:
                    decided.append(f"--restore-nested の {copy.relative_to(root)} が事前検査から変わりました")
                for copy, approved in (() if changed or approval_error else restore.plans.items()):
                    if not approved.matches(copy):
                        decided.append(f"--restore-nested の {copy.relative_to(root)} が復元直前に変わりました")
                        break
                    paths = list(approved.files)
                    done = git(copy, "-c", "core.precomposeunicode=false", "--literal-pathspecs", "restore",
                               f"--source={approved.head}", "--staged", "--worktree", "--", *paths)
                    if done.returncode:
                        decided.append(f"--restore-nested の {copy.relative_to(root)} を復元できません: {done.stderr.strip()}")
                        break
                    try:
                        listings: dict[Path, dict[str, Path]] = {}
                        for rel in paths:
                            path = disk_path(copy, Path(rel), listings)
                            restored[str(path.relative_to(root))] = file_stamp(path)
                    except OSError as error:
                        decided.append(f"--restore-nested の {copy.relative_to(root)} を復元後に確認できません: {error}")
                        break
                    restored_nested[str(copy.relative_to(root))] = paths
                    notes.append(f"後始末: {copy.relative_to(root)} の追跡済みファイル {len(paths)} 件を復元")
            recheck = partial(recheck, decided=tuple(decided))
            result = recheck()
        else:
            result["blockers"].append(NESTED_RESTORE + ", ".join(restore.display()))
            result["ok"] = False
            result["restore_nested"] = restore.display()
            result["restore_missing_files"] = [str((copy / rel).relative_to(root))
                                               for copy, plan in restore.plans.items()
                                               for rel, stamp in plan.files.items() if stamp is None]

    copied = []
    # Nothing is written to main while anything else keeps the worktree.
    if args.archive_scratch and holds_only_records(result, movable):
        copied = copy_to_main(Path(worktree), Path(main), result["review"])
        if copied:
            result = recheck()
    if may_detach(result["nested_worktrees"]):
        nested = result["nested_worktrees"]
        # Copies leave only a worktree that goes: one kept for other blockers keeps its dependencies.
        # Records --archive-scratch and --archive-path move next do not keep it.
        others = [blocker for blocker in result["blockers"]
                  if not blocker.startswith("worktrees of other repositories")]
        scratch_next = bool(archive_movers(Path(worktree), result, movable_now(result), besides_nested=True))
        removable = (nested and (scratch_next or (not others and not result["review"]))
                     and not any(item["blockers"] for item in nested))
        with restoration_lock() if removable else nullcontext():
            if removable and (approval_error := restore_approval_error()):
                result["blockers"].append(approval_error)
                result["ok"] = False
                removable = False
            if removable and restore:
                fresh_nested = [nested_check(Path(item["path"])) for item in nested]
                movement = moved_since(root, since, restored) if since else []
                if fresh_nested != nested or movement:
                    result["blockers"].extend(["子 worktree が削除直前に変わりました", *movement])
                    result["ok"] = False
                    removable = False
            detached = []
            detach_error = ""
            for item in nested if removable else ():
                copy = Path(item["path"])
                current = nested_check(copy)
                prefix = f"{copy.relative_to(root)}/"
                written = {name.removeprefix(prefix): stamp for name, stamp in restored.items() if name.startswith(prefix)}
                movement = moved_since(copy, since, written) if since and restore else []
                if current != item or movement:
                    detach_error = f"子 worktree {copy.relative_to(root)} が削除直前に変わりました"
                    if movement:
                        decided.extend(movement)
                    break
                if item["restore"]:
                    applied = subprocess.run(item["restore"], capture_output=True, text=True)
                    if applied.returncode or nested_check(copy)["blockers"]:
                        detach_error = f"子 worktree {copy.relative_to(root)} の生成ファイル復元後に削除を確認できません"
                        break
                removed = subprocess.run(item["detach"], capture_output=True, text=True)
                if removed.returncode:
                    detach_error = f"子 worktree {copy.relative_to(root)} を削除できません: {removed.stderr.strip()}"
                    break
                detached.append(item["path"])
        if detach_error:
            decided.append(detach_error)
            recheck = partial(recheck, decided=tuple(decided))
        if detached or detach_error:
            result = recheck()
        elif nested:
            notes.append("--detach-nested removed nothing: the worktree is kept while it has other blockers, "
                         "paths to review, or a copy with blockers")
        result["detached"] = detached
    if movable:
        day = datetime.now().strftime("%Y%m%d")
        archived = [archive(source, archive_target(args.archive_dir, Path(worktree), source, day), Path(worktree))
                    for source in archive_movers(root, result, movable_now(result), explicit=explicit)]
        if any(item["verified"] for item in archived):
            kept = {key: result[key] for key in ("detached",) if key in result}
            result = {**recheck(), **kept}
        elif any((Path(worktree) / name).exists() for name in movable):
            options = " and ".join(name for name, given in (("--archive-scratch", args.archive_scratch),
                                                            ("--archive-path", bool(named))) if given)
            obstacles = archive_obstacles(Path(worktree), result, movable_now(result))
            other_blockers = [item for item in result["blockers"] if not re.fullmatch(r"\d+ uncommitted changes", item)]
            note = "%s moved nothing: the worktree is kept for other blockers or paths to review" % options
            if obstacles:
                note += "; paths outside archive scope: " + ", ".join(obstacles)
            if other_blockers:
                note += "; " + "; ".join(other_blockers)
            scratch = (*movable, *SCRATCH_DIRS, *(item["path"].rstrip("/") for item in result["records"]),
                       *untracked_exec_plans(Path(worktree), base, pr))
            if not args.archive_scratch and archive_movers(Path(worktree), result, scratch):
                note += "。%s も止めている。--archive-scratch を足す" % "、".join(obstacles)
            notes.append(note)
        result["archived"] = archived
        if args.archive_scratch:
            result["copied_to_main"] = copied
    if named:
        verified = [Path(item["from"]) for item in result.get("archived", []) if item["verified"]]
        if any(not any(source.is_relative_to(parent) for parent in verified) for source in requested_archives):
            result["blockers"].append("指定されたアーカイブの照合を確認できません")
            result["ok"] = False
        result.setdefault("archived", [])
    notes[:0] = result.pop("notes")
    elsewhere = merged_pr_head_command(worktree, args.pr) if args.pr else None
    if pr_error:
        if not args.pr_head:
            notes.append("PR #%d lookup failed: %s; on a Mac where gh works, run `%s` and pass the arguments it "
                         "prints to rm_check.py" % (args.pr, pr_error, elsewhere) if elsewhere else
                         "PR #%d lookup failed: %s; confirm it is MERGED on a host with gh, then pass --pr-head <sha>"
                         % (args.pr, pr_error))
        else:
            notes.append("PR #%d lookup failed: %s; using the supplied merged PR head" % (args.pr, pr_error))
    if args.fetch_failed:
        if args.pr_head:
            notes.append("git fetch failed; remote refs may be stale. Using the supplied merged PR head")
        elif args.pr or pr:
            notes.append("git fetch failed; remote refs may be stale. Confirm the PR is MERGED on a host with gh and pass --pr-head <sha>")
        else:
            notes.append("git fetch failed; remote refs may be stale. Fetch again before checking the worktree")
    stale_note = stale_fetch_note(worktree, base, args.pr_head, bool(args.pr or pr))
    if stale_note:
        notes.append(stale_note)
    if (not stale_note and not args.pr_head and (args.pr or pr) and (not pr or pr.get("state") == "MERGED")
            and any(blocker.startswith("HEAD ") or blocker.startswith("no base ref")
                    for blocker in result["blockers"])):
        notes.append("For a merged PR, confirm its head on a host with gh and pass --pr-head <sha>")
    if any("cannot be compared" in blocker for blocker in result["blockers"]):
        notes.append("PR head object is absent locally; on a Mac where gh works, run `%s` and pass the arguments "
                     "it prints" % elsewhere if elsewhere else
                     "PR head object is absent locally; compare <pr-head>...<HEAD> with gh on another host and pass its ahead_by as --head-ahead-of-pr <number>")
    pr_base_name = (pr or {}).get("baseRefName")
    if not args.base and not by_pr and pr_base_name:
        notes.append("PR #%s base origin/%s is not fetched: fetch it or pass --base"
                     % ((pr or {}).get("number"), pr_base_name))
    if projects is None:
        notes.append("docker compose unavailable: containers not checked")
    if restored_nested:
        result["restored_nested"] = restored_nested
    if restored:
        result["restored_file_stamps"] = {rel: stamp for rel, stamp in restored.items() if (root / rel).exists()
                                         or (root / rel).is_symlink()}
    if notes:
        result["notes"] = notes
    if automation_blockers:
        result["automation_blocker"] = "; ".join(automation_blockers)
    print_check({"worktree": worktree, "main": main, "base": base, "ok": result["ok"], "reasons": reasons(result),
                 **result}, args.summary)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
