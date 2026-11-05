#!/usr/bin/env python3
"""Check that an Orca worktree is safe to remove."""

import argparse
from datetime import datetime, timedelta, timezone
import filecmp
from functools import partial
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import NamedTuple, TypedDict
from urllib.parse import urlsplit

import lead_decisions
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
# Playwright's HTML reporter writes to playwright-report/ or the outputFolder a config names after it.
CACHE_PREFIXES = ("playwright-report",)
CACHE_SUFFIXES = (".pyc", ".mo", ".tsbuildinfo")
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
# An ignored directory of nothing but logs (django-environ's DEBUG app.log) holds no one's work.
LOG_SUFFIX = ".log"
# The blocker for nested worktrees --detach-nested can remove.
NESTED_READY = "worktrees of other repositories remain inside: "
# Records agents keep in a worktree outside git; --archive-scratch moves them aside.
SCRATCH_DIRS = (".yomiyasu", ".investigate")
# Records --archive-scratch copies to main. Other ignored files, such as .claude/settings.local.json or .env,
# are settings tools in main would run with, so they stay for review.
RECORD_DIRS = (".claude/reviews/",)
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


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def is_cache(relpath):
    parts = Path(relpath.rstrip("/")).parts
    # pip writes <name>.egg-info beside an editable install's sources again on each install.
    return (bool(CACHE_PARTS.intersection(parts)) or relpath.endswith(CACHE_SUFFIXES)
            or any(part.endswith(".egg-info") or part.startswith(CACHE_PREFIXES) for part in parts))


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


def working_changes(worktree) -> tuple[list[str], list[str]]:
    """(uncommitted, regenerable): paths `git status` reports, split off the untracked bytecode in them."""
    uncommitted, regenerable = [], []
    # -z gives each path unquoted; a rename's second entry names its source and is skipped.
    entries = iter(git(worktree, "status", "--porcelain", "-z").stdout.split("\0"))
    for entry in entries:
        if not entry:
            continue
        if set(entry[:2]) & set("RC"):
            next(entries, None)
        rel = entry[3:]
        if entry.startswith("?? ") and holds_only_bytecode(Path(worktree) / rel):
            regenerable.append(rel)
        else:
            uncommitted.append(rel)
    return uncommitted, regenerable


def is_regenerable(relpath):
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


def holds_nothing(path):
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


def env_values(path):
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


def env_keys_beyond_example(env):
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


def nested_worktrees(source: Path) -> list[Path]:
    """Git worktrees of other repositories at an ignored path or directly inside it (a `.git` file names them)."""
    if (source / ".git").is_file():
        return [source]
    if not source.is_dir() or source.is_symlink():
        return []
    return sorted(child for child in source.iterdir() if (child / ".git").is_file())


class IgnoredPaths(NamedTuple):
    review: list[str]
    regenerable: list[str]
    nested: list[Path]
    # The size and last write of each directory of logs counted as regenerable.
    logs: list[str]


def ignored_paths(worktree: str | Path, main: str | Path) -> IgnoredPaths:
    """(review, regenerable, nested, logs): ignored paths that hold something and are neither caches nor
    copies of the main checkout, split into those to open and those a setup or test run writes again,
    and the worktrees of other repositories found at them (such as `local-deps/<repo>`).

    `--ignored=matching` names each path an ignore pattern matched, so a generated
    directory deep inside an otherwise untracked tree is seen by its own path.
    """
    review, regenerable, nested, logs = [], [], [], []
    # -z gives each path unquoted, so a Japanese or spaced name is read as written.
    for entry in git(worktree, "status", "--porcelain", "-z", "--ignored=matching").stdout.split("\0"):
        if not entry.startswith("!! ") or is_cache(entry[3:]):
            continue
        rel = entry[3:]
        source, copy = Path(worktree) / rel, Path(main) / rel
        if holds_nothing(source) or same_tree(source, copy):
            continue
        found = nested_worktrees(source)
        if found and (found == [source] or {child.name for child in source.iterdir()} == {f.name for f in found}):
            nested.extend(found)
            continue
        if holds_only_regenerable(source, Path(worktree)):
            regenerable.append(rel)
            continue
        if (files := log_files(source)) is not None:
            regenerable.append(rel)
            logs.append(log_note(rel, files))
            continue
        where = "not in main checkout" if not copy.exists() else "differs from main checkout"
        if source.name == ".env" and source.is_file():
            keys = env_keys_beyond_example(source)
            if keys == []:
                regenerable.append(rel)
                continue
            if keys:
                where += "; values .env.example does not have: " + ", ".join(keys)
        review.append("%s (%s)" % (rel, where))
    return IgnoredPaths(review, regenerable, nested, logs)


def on_remote(worktree: Path, head: str) -> bool:
    """Whether a remote branch holds HEAD, or (asked over the network) a ref of origin points at it."""
    if git(worktree, "branch", "-r", "--contains", head).stdout.strip():
        return True
    try:
        refs = subprocess.run(["git", "-C", str(worktree), "ls-remote", "origin"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return refs.returncode == 0 and any(line.split("\t")[0] == head for line in refs.stdout.splitlines())


def nested_check(copy: Path) -> dict:
    """Whether a nested worktree can be detached from its repository: clean, nothing ignored to open,
    and HEAD on a remote. `restore` resets recompiled catalogs before `detach` removes it."""
    repository = main_checkout(copy) or ""
    blockers = []
    changes, _ = working_changes(copy)
    modified = set(git(copy, "diff", "HEAD", "--name-only", "--diff-filter=M", "-z").stdout.split("\0"))
    regenerable = [rel for rel in changes if rel.endswith(".mo") and rel in modified
                   and (copy / rel).with_suffix(".po").is_file()
                   and str(Path(rel).with_suffix(".po")) not in changes]
    changes = [rel for rel in changes if rel not in regenerable]
    if changes:
        blockers.append("%d uncommitted changes" % len(changes))
    review, _, deeper, _ = ignored_paths(copy, repository)
    if review or deeper:
        blockers.append("ignored paths to open: " + ", ".join([*review, *(str(d) for d in deeper)]))
    head = git(copy, "rev-parse", "HEAD").stdout.strip()
    if not head or not on_remote(copy, head):
        blockers.append("HEAD %s is on no remote branch" % head[:8])
    return {"path": str(copy), "repository": repository, "head": head[:12], "blockers": blockers,
            "regenerable": regenerable,
            "restore": ["git", "-C", str(copy), "checkout", "HEAD", "--", *regenerable] if regenerable else [],
            "detach": ["git", "-C", repository, "worktree", "remove", "--force", str(copy)]}


def patches_in_base(worktree, head, base):
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


def pr_head_relation(worktree, head, pr_head, head_ahead_of_pr):
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


def compose_in(worktree, compose_projects):
    """Compose projects (from `docker compose ls --all --format json`) defined inside the worktree."""
    prefix = str(Path(worktree).resolve()) + "/"
    found = []
    for project in compose_projects:
        files = [f for f in (project.get("ConfigFiles") or "").split(",") if Path(f).is_absolute()]
        if any(str(Path(f).resolve()).startswith(prefix) for f in files):
            found.append({"project": project.get("Name"), "status": project.get("Status")})
    return found


def process_cwds(pids):
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
        elif pid in pids and line.startswith("n"):
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


def process_table():
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


def tab_root(pid, table):
    """The process Orca's terminal host started for the tab that runs `pid`, or None outside Orca tabs."""
    seen = set()
    while pid in table and pid not in seen:
        seen.add(pid)
        parent = table[pid][0]
        if parent in table and any(host in table[parent][1] for host in ORCA_TERMINAL_HOSTS):
            return pid
        pid = parent
    return None


def descendants(root, table):
    children = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    found, stack = [], [root]
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, []))
    return found


def runs_nothing(root, members, table):
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


def output_readers() -> set[int]:
    """The commands this check's output is piped through (`rm_check.py | jq | ...`), each reading the one before."""
    if not stat.S_ISFIFO(os.fstat(1).st_mode):
        return set()
    ends = pipe_ends()
    readers: set[int] = set()
    # A subshell and the command it runs both read the pipe they inherit.
    pipes = {ends.get(os.getpid(), (None, None))[1]} - {None}
    while pipes:
        found = {pid for pid, (stdin, _) in ends.items() if stdin in pipes} - readers
        readers |= found
        pipes = {ends[pid][1] for pid in found} - {None}
    return readers


def processes_using(worktree: str, processes: dict[int, tuple[int, str]], cwds: dict[int, str],
                    readers: set[int]) -> list[dict]:
    """The processes of one table whose command names the worktree or whose current directory is inside it,
    without this process's ancestors and `readers`; several worktrees can share the table and directories."""
    path = re.compile(r"(?<![\w./-])" + re.escape(worktree) + r"(?=$|[/\s'\";,):])")
    ancestors = set()
    pid = os.getpid()
    while pid in processes and pid not in ancestors:
        ancestors.add(pid)
        pid = processes[pid][0]
    found = []
    for pid, (_, command) in processes.items():
        cwd = cwds.get(pid)
        inside = cwd == worktree or (cwd or "").startswith(worktree + os.sep)
        if pid not in ancestors and pid not in readers and (path.search(command) or inside):
            found.append({"pid": pid, "command": command[:160], **({"cwd": cwd} if inside else {})})
    return found


def processes_in(worktree):
    """Processes using the worktree in their command or current directory, excluding ancestors and
    the commands reading this check's output.

    Each names whether it runs in an Orca tab (`in_orca_tab`), that tab's handle when the
    system shows it (`tab`), and whether the tab runs nothing but its shell (`idle_tab`).
    A process of the tab running this check counts as outside a tab: it must not close itself.
    """
    processes = process_table()
    if processes is None:
        return None
    cwds = process_cwds(processes)
    if cwds is None:
        return None
    found = processes_using(worktree, processes, cwds, output_readers())
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
             "tab": tabs[roots[item["pid"]]][1] if item["pid"] in roots else None} for item in found]


# Written again by an install or a test run; their churn says nothing about who uses the worktree.
UNWATCHED_DIRS = {".git", "node_modules", ".venv"}


def moved_since(worktree: Path, since: datetime) -> list[str]:
    """Blockers for a worktree whose files (outside UNWATCHED_DIRS) or HEAD moved after `since`."""
    after = since.timestamp()
    moved = []
    for top, dirs, files in os.walk(worktree):
        dirs[:] = [name for name in dirs if name not in UNWATCHED_DIRS]
        for name in files:
            try:
                if (Path(top) / name).lstat().st_mtime > after:
                    moved.append(Path(top) / name)
            except OSError:
                continue
    blockers = []
    if moved:
        latest = max(moved, key=lambda path: path.lstat().st_mtime)
        blockers.append("走査（%s）の後に作業場のファイルが %d 個変わりました（最新 %s の %s）。別の担当が使っているかもしれません"
                        % (since.isoformat(timespec="seconds"), len(moved),
                           datetime.fromtimestamp(latest.lstat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
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


def check(worktree, main, base, compose_projects, pr=None, pr_head=None, head_ahead_of_pr=None,
          *, decided: tuple[str, ...] = (), since: datetime | None = None):
    """Return the removal verdict, including containers and processes using `worktree`.

    `decided` are blockers main found before the check: an Orca comment to keep it, an unanswered docker.
    `since` is when the scan that listed the worktree ran: work after it means someone uses the worktree.
    """
    blockers = list(decided)
    if Path(worktree).resolve() == Path(main).resolve():
        blockers.append("this is the repository's main checkout: close its tabs and keep it")
    changes, bytecode = working_changes(worktree)
    if changes:
        blockers.append("%d uncommitted changes" % len(changes))
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    merged_by_patch = []
    open_pr_at_head = (pr or {}).get("state") == "OPEN" and (pr or {}).get("headRefOid") == head
    if not open_pr_at_head and (base is None or git(worktree, "merge-base", "--is-ancestor", head, base).returncode != 0):
        merged_head = pr_head or ((pr or {}).get("headRefOid") if (pr or {}).get("state") == "MERGED" else None)
        relation = pr_head_relation(worktree, head, merged_head, head_ahead_of_pr) if merged_head else None
        # Do not let an equivalent patch hide work committed after the PR head.
        if relation == "ahead":
            assert merged_head is not None
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
                blockers.append("no base ref found (%s); pass --base" % ", ".join(BASE_CANDIDATES))
            elif not pr:
                blockers.append("HEAD %s is not in %s" % (head[:8], base))
            elif pr.get("headRefOid") != head or pr.get("state") != "MERGED":
                blockers.append("HEAD %s is not in %s and PR #%s is %s"
                                % (head[:8], base, pr.get("number"), pr.get("state")))
    review, regenerable, nested, logs = ignored_paths(worktree, main)
    nested = [nested_check(copy) for copy in nested]
    ready = [item["path"] for item in nested if not item["blockers"]]
    held = ["%s (%s)" % (item["path"], "; ".join(item["blockers"])) for item in nested if item["blockers"]]
    if ready:
        blockers.append(NESTED_READY + "%s. Pass --detach-nested to remove them "
                        "from their repositories first, so none is left as a prunable record" % ", ".join(ready))
    if held:
        blockers.append("worktrees of other repositories inside cannot be detached: " + ", ".join(held))
    processes = processes_in(str(Path(worktree).resolve()))
    if processes is None:
        blockers.append("processes could not be checked; check running processes before removing the worktree")
    else:
        outside = [str(item["pid"]) for item in processes if not item["in_orca_tab"]]
        busy = [item for item in processes if item["in_orca_tab"] and not item["idle_tab"]]
        if outside:
            blockers.append("作業場を使うプロセスがあります（Orca のタブの外）。PID %s を止めてから消してください。"
                            "起動に使った zsh のラッパーではなく、node などの本体の PID を止めてください。"
                            % ", ".join(outside))
        if busy:
            closes = sorted({"python3 %s --from SCAN_JSON %s" % (CLOSE_TABS, item["tab"])
                             for item in busy if item["tab"]})
            unknown = [str(item["pid"]) for item in busy if not item["tab"]]
            blockers.append("Orca のタブで作業場を使うプロセスがあります。scan の closable にあるタブなら、保存した走査の "
                            "JSON を SCAN_JSON に渡して閉じてから（走査の後に動いたタブは閉じません）、"
                            "rm_check をやり直してください: " + "; ".join(closes)
                            + ("。handle を読めないタブの PID %s は orca terminal list --worktree path:%s --json "
                               "で探してください" % (", ".join(unknown), worktree) if unknown else ""))
    if since is not None:
        blockers.extend(moved_since(Path(worktree), since))
    return {"ok": not blockers and not review, "blockers": blockers, "uncommitted": changes, "review": review,
            "regenerable": bytecode + regenerable, "merged_by_patch": merged_by_patch, "nested_worktrees": nested,
            "containers": compose_in(worktree, compose_projects), "processes": processes or [], "notes": logs}


def main_checkout(worktree):
    for line in git(worktree, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):]
    return None


BASE_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master")


def ref_exists(worktree, ref):
    return git(worktree, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def default_base(worktree):
    """The first existing remote default branch, or None when the repository has none."""
    for ref in BASE_CANDIDATES:
        if ref_exists(worktree, ref):
            return ref
    return None


def remote_identity(url):
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


def merged_pr_head_command(worktree, number):
    """The merged_pr_head.py command that prints --pr-head and --head-ahead-of-pr on a Mac whose gh works."""
    origin = git(worktree, "remote", "get-url", "origin")
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    host, slug = remote_identity(origin.stdout.strip()) if origin.returncode == 0 else ("", "")
    if host != "github.com" or slug.count("/") != 1 or not head:
        return None
    return ("python3 ~/.claude/skills/orca-session-triage/scripts/merged_pr_head.py --repo %s --pr %d --head %s"
            % (slug, number, head))


def pr_base(worktree, pr):
    """origin/<the PR's base branch> when that ref exists: where the work merges."""
    name = (pr or {}).get("baseRefName")
    ref = "origin/%s" % name if name else None
    return ref if ref and ref_exists(worktree, ref) else None


def pull_request(worktree, number):
    """Return (pr, error): the PR's number/state/head, or why `gh` could not tell."""
    try:
        out = subprocess.run(["gh", "pr", "view", str(number), "--json", "number,state,headRefOid,baseRefName"],
                             cwd=worktree, capture_output=True, text=True, check=True).stdout
        return json.loads(out), None
    except subprocess.CalledProcessError as error:
        return None, (error.stderr or "").strip() or str(error)
    except (OSError, json.JSONDecodeError) as error:
        return None, str(error)


def head_in(worktree, base) -> bool:
    return base is not None and git(worktree, "merge-base", "--is-ancestor", "HEAD", base).returncode == 0


def merged_pr_at_head(worktree) -> tuple[dict | None, str | None]:
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


def scratch_to_archive(worktree: Path, result: dict, *, besides_nested: bool = False) -> list[Path]:
    """The scratch directories that alone keep the worktree: every blocker is an uncommitted change
    inside one of them, and every path to review lies in one. Empty when anything else keeps it.
    `besides_nested` also lets nested worktrees ready to detach stay, for --detach-nested to remove."""
    # A review entry is "<path> (<where>)", and <where> holds no " (".
    paths = [*result["uncommitted"], *(entry.rpartition(" (")[0] for entry in result["review"])]
    tops = {Path(rel).parts[0] for rel in paths}
    others = [blocker for blocker in result["blockers"] if not re.fullmatch(r"\d+ uncommitted changes", blocker)
              and not (besides_nested and blocker.startswith(NESTED_READY))]
    if others or not tops or not tops <= set(SCRATCH_DIRS):
        return []
    return sorted(worktree / top for top in tops)


def holds_only_records(result: dict) -> bool:
    """Whether only paths to review, changes inside scratch directories, and nested worktrees ready to detach
    keep the worktree, so copying its records to main is a step toward removing it."""
    others = [blocker for blocker in result["blockers"] if not re.fullmatch(r"\d+ uncommitted changes", blocker)
              and not blocker.startswith(NESTED_READY)]
    return not others and all(Path(rel).parts[0] in SCRATCH_DIRS for rel in result["uncommitted"])


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


def main(argv: list[str] | None = None, worktree_notes: WorktreeNotes = OrcaNotes()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", metavar="PATH", help="worktree path to check, the same as --worktree")
    parser.add_argument("pr_number", nargs="?", metavar="PR",
                        help='PR number, the same as --pr; "-" for none, as rm-check-batch specs write it')
    parser.add_argument("--worktree", help="worktree path to check")
    parser.add_argument("--main", help="main checkout to compare ignored files with (default: first `git worktree list` entry)")
    parser.add_argument("--base", help="ref that merged work must be in (default: the --pr base branch, else the remote branch the worktree started from, else origin/HEAD; without a PR, use the branch's upstream if only it holds HEAD)")
    parser.add_argument("--pr", type=int, help="PR number; a MERGED or OPEN PR whose head is HEAD clears the unmerged-commit "
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
    parser.add_argument("--no-docker", action="store_true", help="skip the docker compose project lookup")
    parser.add_argument("--decisions-file", type=Path, default=lead_decisions.DEFAULT_LOG,
                        help="the lead's answers in an append-only JSONL log (default: %(default)s)")
    parser.add_argument("--detach-nested", action="store_true",
                        help="when nested worktrees are all that block the removal and each has no changes beyond "
                             "recompiled catalogs, nothing ignored to open, and HEAD on a remote, restore those "
                             "catalogs and remove the copies with git worktree remove --force, then check again")
    parser.add_argument("--archive-scratch", action="store_true",
                        help="first copy each file to review that the main checkout also ignores and lacks (new "
                             "records under .claude/reviews/) to the same place in main, confirmed with cmp; then "
                             "when %s alone keep the worktree (nothing else blocks or needs review), move them to "
                             "<archive-dir>/<worktree>-<name>-<YYYYMMDD>/, then check again; off by default, so "
                             "move them only where the lead's rules say to. With --detach-nested, nested worktrees "
                             "and these records together do not hold each other back" % " and ".join(SCRATCH_DIRS))
    parser.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR,
                        help="where --archive-scratch moves them (default: %(default)s)")
    parser.add_argument("--confirm-gone", type=float, nargs="?", const=GONE_WAIT_SECONDS, metavar="SECONDS",
                        help="instead of checking, confirm after `orca worktree rm` that no row of this Mac's "
                             "`orca worktree ps` has the path and the disk lacks it, reading again for up to SECONDS "
                             "(default: %(const)s); print {worktree, gone, in_orca (null when Orca could not be "
                             "read or cut its list short), on_disk, waited_seconds} and exit 1 while it stays")
    parser.add_argument("--orca", default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
                        help="Orca CLI executable for --confirm-gone (default: $ORCA_CLI_COMMAND or orca)")
    args = parser.parse_args(argv)
    if args.confirm_gone is not None:
        gone = confirm_gone(Path(args.worktree).resolve(), args.orca, args.confirm_gone)
        json.dump(gone, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0 if gone["gone"] else 1
    if args.pr_head and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", args.pr_head):
        parser.error("--pr-head must be a full commit SHA")
    if args.head_ahead_of_pr is not None and (not args.pr_head or args.head_ahead_of_pr < 0):
        parser.error("--head-ahead-of-pr needs --pr-head and a non-negative ahead_by")
    if args.path and args.worktree and Path(args.path).resolve() != Path(args.worktree).resolve():
        parser.error("PATH and --worktree name different worktrees")
    if not (args.path or args.worktree):
        parser.error("give the worktree as PATH or --worktree")
    if args.pr_number not in (None, "-"):
        if not args.pr_number.isdigit() or (args.pr and args.pr != int(args.pr_number)):
            parser.error("PR must be a number or -, and agree with --pr")
        args.pr = int(args.pr_number)

    worktree = str(Path(args.worktree or args.path).resolve())
    inside = git(worktree, "rev-parse", "--is-inside-work-tree")
    gitdir = Path(worktree) / ".git"
    if inside.stdout.strip() != "true" and gitdir.is_file() and Path(
            gitdir.read_text(errors="replace").removeprefix("gitdir:").strip()).is_dir():
        # A gitdir that is there answers on a second try once git's lock or index refresh has passed.
        inside = git(worktree, "rev-parse", "--is-inside-work-tree")
    if inside.stdout.strip() != "true":
        json.dump({"worktree": worktree, "ok": False,
                   "blockers": ["not a git worktree: git rev-parse exited %d: %s"
                                % (inside.returncode, inside.stderr.strip() or "(no stderr)")]},
                  sys.stdout, ensure_ascii=False)
        print()
        return 2
    since = args.since
    if since is None and args.from_file:
        rows = json.loads(args.from_file.read_text(encoding="utf-8"))
        scanned = [row["scanned_at"] for row in rows if row.get("scanned_at")]
        since = datetime.fromisoformat(scanned[0]) if scanned else None
    if since is not None and since.tzinfo is None:
        since = since.astimezone()
    main = str(Path(args.main or main_checkout(worktree)).resolve())
    pr, pr_error = pull_request(worktree, args.pr) if args.pr else (None, None)
    notes = []
    keep_blocker = None
    try:
        comment = keep_note(worktree_notes.comment(Path(worktree)))
    except NotesUnavailable as error:
        comment = None
        notes.append("worktree note could not be read: %s" % error)
    if comment:
        key = "remove:" + worktree
        answer = None
        try:
            answer = lead_decisions.latest(lead_decisions.records(args.decisions_file.expanduser())).get(key)
        except (OSError, ValueError) as error:
            notes.append("lead decisions could not be read: %s" % error)
        if answer is None:
            keep_blocker = ("Orca comment asks to keep it: %s. Remove it only after the lead answers %s"
                            % (comment, key))
        else:
            notes.append("Lead answered %s: %r" % (key, answer.answer))
    fetch_note = fetch_when_stale(worktree, args.base or start_base(worktree) or default_base(worktree))
    if fetch_note:
        notes.append(fetch_note)
    if not args.pr and not args.pr_head and not head_in(worktree, args.base or start_base(worktree)
                                                         or default_base(worktree)):
        pr, lookup_error = merged_pr_at_head(worktree)
        branch = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        if pr:
            notes.append("--pr was not given: PR #%s of branch %s is MERGED at HEAD, so it counts as --pr %s"
                         % (pr.get("number"), branch, pr.get("number")))
        elif lookup_error:
            notes.append("looking up a merged PR of branch %s failed: %s; pass --pr" % (branch, lookup_error))
    by_pr = pr_base(worktree, pr)
    base = args.base or by_pr or start_base(worktree) or default_base(worktree)
    if not (args.base or args.pr or args.pr_head or pr or head_in(worktree, base)):
        # A branch can track a local branch (branch.<name>.remote=.), which proves nothing was pushed.
        upstream = git(worktree, "rev-parse", "--symbolic-full-name", "@{upstream}").stdout.strip()
        if upstream.startswith("refs/remotes/") and head_in(worktree, upstream):
            base = upstream.removeprefix("refs/remotes/")
            notes.append("Using %s as base: HEAD is pushed to the branch it tracks, and no PR was given or found"
                         % base)
    decided = [keep_blocker] if keep_blocker else []
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
                      decided=tuple(decided), since=since)
    result = recheck()
    copied = []
    # Nothing is written to main while anything else keeps the worktree.
    if args.archive_scratch and holds_only_records(result):
        copied = copy_to_main(Path(worktree), Path(main), result["review"])
        if copied:
            result = recheck()
    if args.detach_nested:
        nested = result["nested_worktrees"]
        # Copies leave only a worktree that goes: one kept for other blockers keeps its dependencies.
        # Records --archive-scratch moves next do not keep it.
        others = [blocker for blocker in result["blockers"]
                  if not blocker.startswith("worktrees of other repositories")]
        scratch_next = args.archive_scratch and bool(scratch_to_archive(Path(worktree), result, besides_nested=True))
        removable = (nested and (scratch_next or (not others and not result["review"]))
                     and not any(item["blockers"] for item in nested))
        detached = [item["path"] for item in nested if removable
                    and (not item["restore"]
                         or subprocess.run(item["restore"], capture_output=True, text=True).returncode == 0)
                    and subprocess.run(item["detach"], capture_output=True, text=True).returncode == 0]
        if detached:
            result = recheck()
        elif nested:
            notes.append("--detach-nested removed nothing: the worktree is kept while it has other blockers, "
                         "paths to review, or a copy with blockers")
        result["detached"] = detached
    if args.archive_scratch:
        day = datetime.now().strftime("%Y%m%d")
        archived = []
        for scratch in scratch_to_archive(Path(worktree), result):
            target = archive_target(args.archive_dir, Path(worktree), scratch, day)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(scratch, target)
            archived.append({"from": str(scratch), "to": str(target)})
        if archived:
            kept = {key: result[key] for key in ("detached",) if key in result}
            result = {**recheck(), **kept}
        elif any((Path(worktree) / name).exists() for name in SCRATCH_DIRS):
            notes.append("--archive-scratch moved nothing: the worktree is kept for other blockers or paths to review")
        result["archived"] = archived
        result["copied_to_main"] = copied
    notes[:0] = result.pop("notes")
    elsewhere = merged_pr_head_command(worktree, args.pr) if args.pr else None
    if pr_error:
        if not args.pr_head:
            result["blockers"].append("PR #%d lookup failed: %s" % (args.pr, pr_error))
            result["ok"] = False
            notes.append("PR #%d lookup failed: %s; on a Mac where gh works, run `%s` and pass the arguments it "
                         "prints to rm_check.py" % (args.pr, pr_error, elsewhere) if elsewhere else
                         "PR #%d lookup failed: %s; confirm it is MERGED on a host with gh, then pass --pr-head <sha>"
                         % (args.pr, pr_error))
        else:
            notes.append("PR #%d lookup failed: %s; using the supplied merged PR head" % (args.pr, pr_error))
    if pr and args.pr_head and pr.get("headRefOid") != args.pr_head:
        result["blockers"].append("--pr-head does not match PR #%d head" % args.pr)
        result["ok"] = False
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
    if notes:
        result["notes"] = notes
    json.dump({"worktree": worktree, "main": main, "base": base, **result}, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
