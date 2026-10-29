#!/usr/bin/env python3
"""Check that an Orca worktree is safe to remove."""

import argparse
from datetime import datetime, timedelta, timezone
import filecmp
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import TypedDict
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
REGENERABLE_PARTS = {"__generated__"}
REGENERABLE_PART_PAIRS = {("graphql", "generated"), ("var", "test-storage")}
REGENERABLE_PREFIXES = ("src/paraglide/", "e2e/screenshots/", "e2e/.state/", "e2e/.auth/", ".apm-local/",
                        ".husky/_/", ".vite-hooks/_/")
# Records agents keep in a worktree outside git; --archive-scratch moves them aside.
SCRATCH_DIRS = (".yomiyasu", ".investigate")
ARCHIVE_DIR = Path.home() / ".cache/orca-briefs/archive"
# Closes a busy tab only while its worktree has not moved since the scan that listed it.
CLOSE_TABS = Path(__file__).absolute().with_name("close_tabs.py")
# A per-worktree .env may move these away from its .env.example and still be written again.
ENV_LOCAL_KEY_SUFFIXES = ("_PORT", "_PORT_RANGE", "_BIND_IP")
ENV_LOCAL_KEYS = {"COMPOSE_PROJECT_NAME"}
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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


def ignored_paths(worktree: str | Path, main: str | Path) -> tuple[list[str], list[str], list[Path]]:
    """(review, regenerable, nested): ignored paths that hold something and are neither caches nor
    copies of the main checkout, split into those to open and those a setup or test run writes again,
    and the worktrees of other repositories found at them (such as `local-deps/<repo>`).

    `--ignored=matching` names each path an ignore pattern matched, so a generated
    directory deep inside an otherwise untracked tree is seen by its own path.
    """
    review, regenerable, nested = [], [], []
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
        where = "not in main checkout" if not copy.exists() else "differs from main checkout"
        if source.name == ".env" and source.is_file():
            keys = env_keys_beyond_example(source)
            if keys == []:
                regenerable.append(rel)
                continue
            if keys:
                where += "; values .env.example does not have: " + ", ".join(keys)
        review.append("%s (%s)" % (rel, where))
    return review, regenerable, nested


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
    review, _, deeper = ignored_paths(copy, repository)
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


def processes_in(worktree):
    """Processes using the worktree in their command or current directory, excluding ancestors.

    Each names whether it runs in an Orca tab (`in_orca_tab`), that tab's handle when the
    system shows it (`tab`), and whether the tab runs nothing but its shell (`idle_tab`).
    A process of the tab running this check counts as outside a tab: it must not close itself.
    """
    path = re.compile(r"(?<![\w./-])" + re.escape(worktree) + r"(?=$|[/\s'\";,):])")
    processes = process_table()
    if processes is None:
        return None
    ancestors = set()
    pid = os.getpid()
    while pid in processes and pid not in ancestors:
        ancestors.add(pid)
        pid = processes[pid][0]
    cwds = process_cwds(processes)
    if cwds is None:
        return None
    found = []
    for pid, (_, command) in processes.items():
        cwd = cwds.get(pid)
        inside = cwd == worktree or (cwd or "").startswith(worktree + os.sep)
        if pid not in ancestors and (path.search(command) or inside):
            found.append({"pid": pid, "command": command[:160], **({"cwd": cwd} if inside else {})})
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


def check(worktree, main, base, compose_projects, pr=None, pr_head=None, head_ahead_of_pr=None,
          *, decided: tuple[str, ...] = ()):
    """Return the removal verdict, including containers and processes using `worktree`.

    `decided` are blockers main found before the check: an Orca comment to keep it, an unanswered docker.
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
    review, regenerable, nested = ignored_paths(worktree, main)
    nested = [nested_check(copy) for copy in nested]
    ready = [item["path"] for item in nested if not item["blockers"]]
    held = ["%s (%s)" % (item["path"], "; ".join(item["blockers"])) for item in nested if item["blockers"]]
    if ready:
        blockers.append("worktrees of other repositories remain inside: %s. Pass --detach-nested to remove them "
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
    return {"ok": not blockers and not review, "blockers": blockers, "uncommitted": changes, "review": review,
            "regenerable": bytecode + regenerable, "merged_by_patch": merged_by_patch, "nested_worktrees": nested,
            "containers": compose_in(worktree, compose_projects), "processes": processes or []}


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


def stale_fetch_note(worktree: str | Path, base: str | None, pr_head: str | None, has_pr: bool) -> str | None:
    """Warn when no worktree has fetched the base remote in the last day."""
    ref = (base or "").removeprefix("refs/remotes/")
    remote, separator, _ = ref.partition("/")
    if not separator:
        return None
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
    if not fetched_at:
        return None
    latest = datetime.fromtimestamp(max(fetched_at), timezone.utc)
    if datetime.now(timezone.utc) - latest <= timedelta(days=1):
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


def scratch_to_archive(worktree: Path, result: dict) -> list[Path]:
    """The scratch directories that alone keep the worktree: every blocker is an uncommitted change
    inside one of them, and every path to review lies in one. Empty when anything else keeps it."""
    # A review entry is "<path> (<where>)", and <where> holds no " (".
    paths = [*result["uncommitted"], *(entry.rpartition(" (")[0] for entry in result["review"])]
    tops = {Path(rel).parts[0] for rel in paths}
    others = [blocker for blocker in result["blockers"] if not re.fullmatch(r"\d+ uncommitted changes", blocker)]
    if others or not tops or not tops <= set(SCRATCH_DIRS):
        return []
    return sorted(worktree / top for top in tops)


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


def main(argv: list[str] | None = None, worktree_notes: WorktreeNotes = OrcaNotes()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, help="worktree path to check")
    parser.add_argument("--main", help="main checkout to compare ignored files with (default: first `git worktree list` entry)")
    parser.add_argument("--base", help="ref that merged work must be in (default: the --pr base branch, else the remote branch the worktree started from, else origin/HEAD; without a PR, use the branch's upstream if only it holds HEAD)")
    parser.add_argument("--pr", type=int, help="PR number; a MERGED or OPEN PR whose head is HEAD clears the unmerged-commit "
                                               "check. Without it, a MERGED PR of the checked-out branch whose head is "
                                               "HEAD (asked of gh when HEAD is not in the base) counts the same")
    parser.add_argument("--pr-head", help="full SHA of a PR head confirmed MERGED on a host with gh; HEAD may be its ancestor")
    parser.add_argument("--head-ahead-of-pr", type=int, help="ahead_by from comparing <pr-head>...<HEAD> with gh; use when the PR head object is absent here")
    parser.add_argument("--fetch-failed", action="store_true", help="record that git fetch failed before this check; remote refs may be stale")
    parser.add_argument("--no-docker", action="store_true", help="skip the docker compose project lookup")
    parser.add_argument("--decisions-file", type=Path, default=lead_decisions.DEFAULT_LOG,
                        help="the lead's answers in an append-only JSONL log (default: %(default)s)")
    parser.add_argument("--detach-nested", action="store_true",
                        help="when nested worktrees are all that block the removal and each has no changes beyond "
                             "recompiled catalogs, nothing ignored to open, and HEAD on a remote, restore those "
                             "catalogs and remove the copies with git worktree remove --force, then check again")
    parser.add_argument("--archive-scratch", action="store_true",
                        help="when %s alone keep the worktree (nothing else blocks or needs review), move them to "
                             "<archive-dir>/<worktree>-<name>-<YYYYMMDD>/, then check again; off by default, so "
                             "move them only where the lead's rules say to" % " and ".join(SCRATCH_DIRS))
    parser.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR,
                        help="where --archive-scratch moves them (default: %(default)s)")
    args = parser.parse_args(argv)
    if args.pr_head and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", args.pr_head):
        parser.error("--pr-head must be a full commit SHA")
    if args.head_ahead_of_pr is not None and (not args.pr_head or args.head_ahead_of_pr < 0):
        parser.error("--head-ahead-of-pr needs --pr-head and a non-negative ahead_by")

    worktree = str(Path(args.worktree).resolve())
    if git(worktree, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        json.dump({"worktree": worktree, "ok": False, "blockers": ["not a git worktree"]}, sys.stdout)
        print()
        return 2
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
        upstream = git(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").stdout.strip()
        if head_in(worktree, upstream or None):
            base = upstream
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
    result = check(worktree, main, base, projects or [], pr, supplied_head, args.head_ahead_of_pr, decided=tuple(decided))
    if args.detach_nested:
        nested = result["nested_worktrees"]
        # Copies leave only a worktree that goes: one kept for other blockers keeps its dependencies.
        others = [blocker for blocker in result["blockers"]
                  if not blocker.startswith("worktrees of other repositories")]
        removable = nested and not others and not result["review"] and not any(item["blockers"] for item in nested)
        detached = [item["path"] for item in nested if removable
                    and (not item["restore"]
                         or subprocess.run(item["restore"], capture_output=True, text=True).returncode == 0)
                    and subprocess.run(item["detach"], capture_output=True, text=True).returncode == 0]
        if detached:
            result = check(worktree, main, base, projects or [], pr, supplied_head, args.head_ahead_of_pr,
                           decided=tuple(decided))
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
            result = {**check(worktree, main, base, projects or [], pr, supplied_head, args.head_ahead_of_pr,
                             decided=tuple(decided)), **kept}
        elif any((Path(worktree) / name).exists() for name in SCRATCH_DIRS):
            notes.append("--archive-scratch moved nothing: the worktree is kept for other blockers or paths to review")
        result["archived"] = archived
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
