#!/usr/bin/env python3
"""Start one Claude Code cloud session from a checkout and print its URL.

`claude --cloud "<task>"` needs a terminal, and asks whether to trust a checkout
it has not seen before. This runs it under a pty, reads the task from a file,
and waits for the session URL. The trust prompt is answered only with
--trust-folder, and only after the selector shows "Yes" selected.

The session clones the checkout's GitHub remote at its current branch, so run
this from a checkout on the branch the work should start from.
"""

import argparse
import json
import os
import pty
import re
import select
import signal
import sys
import time

SESSION_URL = re.compile(r"https://claude\.ai/code/(session_[A-Za-z0-9]+)")
ESCAPES = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?")
TRUST_PROMPT = "trustthisfolder"
TRUST_SELECTED = "❯Yes,Itrust"
DOWN, ENTER = b"\x1b[B", b"\r"


class LaunchError(Exception):
    def __init__(self, reason, output=""):
        super().__init__(reason)
        self.output = output


def clean(raw):
    """Terminal output as text, without escape sequences."""
    return ESCAPES.sub(b"", raw).decode("utf-8", "replace")


def compact(text):
    """The TUI drops or moves spaces between redraws: compare without whitespace."""
    return re.sub(r"\s+", "", text)


def session_url(text):
    match = SESSION_URL.search(text)
    return match.group(0) if match else None


def read_available(fd, wait):
    ready, _, _ = select.select([fd], [], [], wait)
    if not ready:
        return b""
    try:
        return os.read(fd, 65536)
    except OSError:
        return None


def launch(checkout, brief, *, claude="claude", trust_folder=False, timeout=150.0):
    """Start the session and return {"session_id", "url"}; raise LaunchError otherwise."""
    pid, fd = pty.fork()
    if pid == 0:
        # The child must never return into the caller's code: leave on any failure.
        try:
            os.chdir(checkout)
            os.environ.setdefault("TERM", "xterm-256color")
            os.execvp(claude, [claude, "--cloud", brief])
        except OSError as error:
            os.write(2, f"cannot start {claude} in {checkout}: {error}\n".encode())
        os._exit(127)
    raw = b""
    answered = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            chunk = read_available(fd, 0.5)
            if chunk is None:
                break
            raw += chunk
            text = clean(raw)
            url = session_url(text)
            if url:
                return {"session_id": SESSION_URL.search(url).group(1), "url": url}
            if not answered and TRUST_PROMPT in compact(text):
                if not trust_folder:
                    raise LaunchError("claude asks whether to trust this checkout; check it is yours and pass "
                                      "--trust-folder", text)
                seen = len(raw)
                os.write(fd, DOWN)
                selected = False
                until = time.monotonic() + 5
                while time.monotonic() < until and not selected:
                    more = read_available(fd, 0.2)
                    if more is None:
                        break
                    raw += more
                    selected = TRUST_SELECTED in compact(clean(raw[seen:]))
                if not selected:
                    raise LaunchError("the trust selector did not show Yes selected; not answering it", clean(raw))
                os.write(fd, ENTER)
                answered = True
        text = clean(raw)
        url = session_url(text)
        if url:
            return {"session_id": SESSION_URL.search(url).group(1), "url": url}
        raise LaunchError("no session URL appeared", text)
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkout", required=True, help="checkout to start from; its current branch is cloned")
    parser.add_argument("--brief", required=True, help="file holding the task text")
    parser.add_argument("--trust-folder", action="store_true",
                        help="answer Yes when claude asks whether to trust the checkout")
    parser.add_argument("--timeout", type=float, default=150.0, help="seconds to wait for the session URL")
    parser.add_argument("--claude", default=os.environ.get("CLAUDE_CLI", "claude"), help="claude executable")
    parser.add_argument("--json", action="store_true", help="print {session_id, url} as JSON")
    args = parser.parse_args(argv)
    try:
        with open(args.brief, encoding="utf-8") as handle:
            brief = handle.read()
    except OSError as error:
        print(f"cannot read the brief: {error}", file=sys.stderr)
        return 2
    if not brief.strip():
        print("the brief is empty", file=sys.stderr)
        return 2
    try:
        result = launch(args.checkout, brief, claude=args.claude, trust_folder=args.trust_folder,
                        timeout=args.timeout)
    except LaunchError as error:
        print(f"no session started: {error}", file=sys.stderr)
        tail = "\n".join(line for line in error.output.splitlines() if line.strip())[-1500:]
        if tail:
            print(tail, file=sys.stderr)
        return 1
    print(json.dumps(result) if args.json else result["url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
