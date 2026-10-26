#!/usr/bin/env python3
"""Check that an Orca worktree is safe to remove."""

import argparse
from datetime import datetime, timedelta, timezone
import filecmp
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

CACHE_PARTS = {
    "node_modules", ".venv", ".devenv", ".direnv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".svelte-kit", "apm_modules", ".import_linter_cache", ".moon", "dist", "build",
    "coverage", "test-results", "playwright-report",
}
CACHE_SUFFIXES = (".pyc", ".mo", ".tsbuildinfo")
# Generated sources and e2e output a setup or a test run writes again. Matched by
# path, not by any part, so a hand-made docs/screenshots/ still goes to review.
REGENERABLE_PARTS = {"__generated__"}
REGENERABLE_PREFIXES = ("src/paraglide/", "e2e/screenshots/", "e2e/.state/", "e2e/.auth/")
# A per-worktree .env may move these away from its .env.example and still be written again.
ENV_LOCAL_KEY_SUFFIXES = ("_PORT", "_PORT_RANGE", "_BIND_IP")
ENV_LOCAL_KEYS = {"COMPOSE_PROJECT_NAME"}
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def is_cache(relpath):
    parts = Path(relpath.rstrip("/")).parts
    return bool(CACHE_PARTS.intersection(parts)) or relpath.endswith(CACHE_SUFFIXES)


def is_regenerable(relpath):
    rel = relpath if relpath.endswith("/") else relpath + "/"
    return bool(REGENERABLE_PARTS.intersection(Path(relpath.rstrip("/")).parts)) or rel.startswith(REGENERABLE_PREFIXES)


def same_tree(left, right):
    if left.is_symlink() or right.is_symlink():
        return left.is_symlink() and right.is_symlink() and os.readlink(left) == os.readlink(right)
    if left.is_file():
        return right.is_file() and filecmp.cmp(left, right, shallow=False)
    if not (left.is_dir() and right.is_dir()):
        return False
    names = {p.name for p in left.iterdir()} | {p.name for p in right.iterdir()}
    return all(same_tree(left / n, right / n) for n in names)


def holds_nothing(path):
    """An empty file, or a directory whose files are all empty."""
    if path.is_symlink():
        return False
    if path.is_file():
        return path.stat().st_size == 0
    return path.is_dir() and all(holds_nothing(child) for child in path.iterdir())


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


def ignored_paths(worktree, main):
    """(review, regenerable): ignored paths that hold something and are neither caches nor
    copies of the main checkout, split into those to open and those a setup or test run writes again.

    `--ignored=matching` names each path an ignore pattern matched, so a generated
    directory deep inside an otherwise untracked tree is seen by its own path.
    """
    review, regenerable = [], []
    # -z gives each path unquoted, so a Japanese or spaced name is read as written.
    for entry in git(worktree, "status", "--porcelain", "-z", "--ignored=matching").stdout.split("\0"):
        if not entry.startswith("!! ") or is_cache(entry[3:]):
            continue
        rel = entry[3:]
        source, copy = Path(worktree) / rel, Path(main) / rel
        if holds_nothing(source) or (copy.exists() and same_tree(source, copy)):
            continue
        if is_regenerable(rel):
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
    return review, regenerable


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
        files = [f for f in (project.get("ConfigFiles") or "").split(",") if os.path.isabs(f)]
        if any(str(Path(f).resolve()).startswith(prefix) for f in files):
            found.append({"project": project.get("Name"), "status": project.get("Status")})
    return found


def processes_in(worktree):
    """Processes whose command contains the worktree path, excluding this check's ancestors."""
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

    ancestors = set()
    pid = os.getpid()
    while pid in processes and pid not in ancestors:
        ancestors.add(pid)
        pid = processes[pid][0]
    return [{"pid": pid, "command": command[:160]}
            for pid, (_, command) in processes.items()
            if pid not in ancestors and worktree in command]


def check(worktree, main, base, compose_projects, pr=None, pr_head=None, head_ahead_of_pr=None):
    """Return the removal verdict, including containers and processes using `worktree`."""
    blockers = []
    if Path(worktree).resolve() == Path(main).resolve():
        blockers.append("this is the repository's main checkout: close its tabs and keep it")
    changes = [line for line in git(worktree, "status", "--porcelain").stdout.splitlines() if line]
    if changes:
        blockers.append("%d uncommitted changes" % len(changes))
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    merged_by_patch = []
    if base is None or git(worktree, "merge-base", "--is-ancestor", head, base).returncode != 0:
        merged_head = pr_head or ((pr or {}).get("headRefOid") if (pr or {}).get("state") == "MERGED" else None)
        relation = pr_head_relation(worktree, head, merged_head, head_ahead_of_pr) if merged_head else None
        # Do not let an equivalent patch hide work committed after the PR head.
        if relation == "ahead":
            blockers.append("HEAD %s is ahead of merged PR head %s" % (head[:8], merged_head[:8]))
        elif relation != "contained":
            # A commit rebased before its PR merged has its patch in base under another hash.
            merged_by_patch = (patches_in_base(worktree, head, base) or []) if base else []
            if merged_by_patch:
                pass
            elif relation == "diverged":
                blockers.append("HEAD %s diverged from merged PR head %s" % (head[:8], merged_head[:8]))
            elif relation == "unknown":
                blockers.append("HEAD %s cannot be compared with merged PR head %s" % (head[:8], merged_head[:8]))
            elif base is None:
                blockers.append("no base ref found (%s); pass --base" % ", ".join(BASE_CANDIDATES))
            elif not pr:
                blockers.append("HEAD %s is not in %s" % (head[:8], base))
            elif pr.get("headRefOid") != head or pr.get("state") != "MERGED":
                blockers.append("HEAD %s is not in %s and PR #%s is %s"
                                % (head[:8], base, pr.get("number"), pr.get("state")))
    review, regenerable = ignored_paths(worktree, main)
    processes = processes_in(str(Path(worktree).resolve()))
    if processes is None:
        blockers.append("processes could not be checked; check running processes before removing the worktree")
    elif processes:
        blockers.append("作業場を使うプロセスがあります。その PID を止めてから消してください。起動に使った zsh のラッパーではなく、node などの本体の PID を止めてください。")
    return {"ok": not blockers and not review, "blockers": blockers, "review": review,
            "regenerable": regenerable, "merged_by_patch": merged_by_patch,
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


def stale_fetch_note(worktree, base, pr_head):
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
    return note + "merge 済み PR の head を別の Mac で確認し、--pr-head <sha> を渡してください。"


def start_base(worktree):
    """The remote branch the checked-out branch was created from, read from its reflog, or None.

    Orca and `git worktree add -b` record "branch: Created from origin/<base>", which names
    where the work merges when that is not the remote's default branch. A branch started
    from its own remote branch (origin/<same name>) holds pushed work, not merged work.
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


def compose_projects():
    try:
        out = subprocess.run(["docker", "compose", "ls", "--all", "--format", "json"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return json.loads(out or "[]")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, help="worktree path to check")
    parser.add_argument("--main", help="main checkout to compare ignored files with (default: first `git worktree list` entry)")
    parser.add_argument("--base", help="ref that merged work must be in (default: the --pr base branch, else the remote branch the worktree started from, else origin/HEAD)")
    parser.add_argument("--pr", type=int, help="PR number; a MERGED PR whose head is HEAD counts as merged (squash merges)")
    parser.add_argument("--pr-head", help="full SHA of a PR head confirmed MERGED on a host with gh; HEAD may be its ancestor")
    parser.add_argument("--head-ahead-of-pr", type=int, help="ahead_by from comparing <pr-head>...<HEAD> with gh; use when the PR head object is absent here")
    parser.add_argument("--fetch-failed", action="store_true", help="record that git fetch failed before this check; remote refs may be stale")
    parser.add_argument("--no-docker", action="store_true", help="skip the docker compose project lookup")
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
    by_pr = pr_base(worktree, pr)
    base = args.base or by_pr or start_base(worktree) or default_base(worktree)
    projects = [] if args.no_docker else compose_projects()
    supplied_head = args.pr_head if not pr or pr.get("state") == "MERGED" else None
    result = check(worktree, main, base, projects or [], pr, supplied_head, args.head_ahead_of_pr)
    notes = []
    if pr_error:
        if not args.pr_head:
            result["blockers"].append("PR #%d lookup failed: %s" % (args.pr, pr_error))
            result["ok"] = False
            notes.append("PR #%d lookup failed: %s; confirm it is MERGED on a host with gh, then pass --pr-head <sha>" % (args.pr, pr_error))
        else:
            notes.append("PR #%d lookup failed: %s; using the supplied merged PR head" % (args.pr, pr_error))
    if pr and args.pr_head and pr.get("headRefOid") != args.pr_head:
        result["blockers"].append("--pr-head does not match PR #%d head" % args.pr)
        result["ok"] = False
    if args.fetch_failed:
        if args.pr_head:
            notes.append("git fetch failed; remote refs may be stale. Using the supplied merged PR head")
        else:
            notes.append("git fetch failed; remote refs may be stale. Confirm the PR is MERGED on a host with gh and pass --pr-head <sha>")
    stale_note = stale_fetch_note(worktree, base, args.pr_head)
    if stale_note:
        notes.append(stale_note)
    if (not stale_note and not args.pr_head and (not pr or pr.get("state") == "MERGED")
            and any(blocker.startswith("HEAD ") or blocker.startswith("no base ref")
                    for blocker in result["blockers"])):
        notes.append("For a merged PR, confirm its head on a host with gh and pass --pr-head <sha>")
    if any("cannot be compared" in blocker for blocker in result["blockers"]):
        notes.append("PR head object is absent locally; compare <pr-head>...<HEAD> with gh on another host and pass its ahead_by as --head-ahead-of-pr <number>")
    if not args.base and not by_pr and (pr or {}).get("baseRefName"):
        notes.append("PR #%d base origin/%s is not fetched: fetch it or pass --base" % (args.pr, pr["baseRefName"]))
    if projects is None:
        notes.append("docker compose unavailable: containers not checked")
    if notes:
        result["notes"] = notes
    json.dump({"worktree": worktree, "main": main, "base": base, **result}, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
