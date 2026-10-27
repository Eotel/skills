#!/usr/bin/env python3
"""Start one Claude Code cloud session from a checkout and print its URL.

`claude --cloud "<task>"` needs a terminal, and asks whether to trust a checkout
it has not seen before. This runs it under a pty, reads the task from a file,
and waits for the session URL. The trust prompt is answered only with
--trust-folder: trusting a checkout lets its hooks and settings run here.

The session starts from the checkout's GitHub remote at its current branch, or
from an upload of the checkout where the Claude GitHub App is not installed on
the repository. Run this from a clean checkout on a pushed branch.
"""

import argparse
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import sys
import time

SESSION_URL = re.compile(r"https://claude\.ai/code/(session_[A-Za-z0-9]+)")
SESSION_ID = re.compile(r"session_[A-Za-z0-9]+")
ESCAPES = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]")
TRUST_PROMPT = "trustthisfolder"
POINTER = "❯"
TRUST_YES, TRUST_NO = "Yes,Itrust", "No,exit"
DOWN, ENTER = b"\x1b[B", b"\r"
# The trust dialog drops keys for about 150 ms after it opens, then redraws with "No" selected
# (claude 2.1.293, seen 2026-10-13). Read this long before a key, and after one before judging it.
KEY_WAIT = 0.5
# The URL can arrive in pieces: read this long after its first sight before taking the ID.
URL_WAIT = 0.5
STOP_WAIT = 3.0


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


def new_session(text, quoted=()):
    """The first session URL in the output, other than those the brief itself quotes."""
    for match in SESSION_URL.finditer(text):
        if not any(known.startswith(match.group(1)) for known in quoted):
            return match
    return None


def selected(text):
    """What follows the pointer in the last frame drawn."""
    _, pointer, rest = compact(text).rpartition(POINTER)
    return rest if pointer else ""


class Screen:
    """What claude has written to the pty so far."""

    def __init__(self, fd):
        self.fd = fd
        self.raw = b""
        self.closed = False

    def read(self, wait):
        """Take what arrives within `wait` seconds. A closed pty means claude has exited."""
        if self.closed:
            return
        ready, _, _ = select.select([self.fd], [], [], wait)
        if not ready:
            return
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:
            chunk = b""
        if chunk:
            self.raw += chunk
        else:
            self.closed = True

    def read_for(self, seconds):
        end = time.monotonic() + seconds
        while not self.closed and time.monotonic() < end:
            self.read(max(0.0, end - time.monotonic()))

    def send(self, keys):
        try:
            os.write(self.fd, keys)
        except OSError as error:
            raise LaunchError(f"claude stopped reading before the trust prompt was answered ({error})",
                              self.text()) from error

    def text(self, start=0):
        return clean(self.raw[start:])


def become_claude(checkout, claude, brief):
    """The forked child: turn into claude, or leave. It must never return into the caller's code."""
    try:
        os.chdir(checkout)
        os.environ.setdefault("TERM", "xterm-256color")
        os.execvp(claude, [claude, "--cloud", brief])
    except BaseException as error:  # a NUL in the brief raises ValueError, not OSError
        os.write(2, f"cannot start {claude} in {checkout}: {error}\n".encode())
    finally:
        os._exit(127)


def answer_trust(screen):
    """Move the selector to Yes and confirm. Return where the output after Enter starts."""
    screen.read_for(KEY_WAIT)
    moved = len(screen.raw)
    screen.send(DOWN)
    screen.read_for(KEY_WAIT)
    if not selected(screen.text(moved)).startswith(TRUST_YES):
        raise LaunchError("the trust selector did not show Yes selected; not answering it", screen.text())
    entered = len(screen.raw)
    screen.send(ENTER)
    return entered


def watch(screen, quoted, trust_folder, timeout):
    deadline = time.monotonic() + timeout
    entered = None
    while not screen.closed and time.monotonic() < deadline:
        screen.read(0.5)
        if new_session(screen.text(), quoted):
            screen.read_for(URL_WAIT)
            break
        if entered is None:
            if TRUST_PROMPT in compact(screen.text()):
                if not trust_folder:
                    raise LaunchError("claude asks whether to trust this checkout; check it is one you cloned from "
                                      "the user's own repository and pass --trust-folder", screen.text())
                entered = answer_trust(screen)
        elif selected(screen.text(entered)).startswith((TRUST_YES, TRUST_NO)):
            raise LaunchError("the trust prompt was drawn again after Enter; the checkout was not trusted",
                              screen.text())
    found = new_session(screen.text(), quoted)
    if found:
        return {"session_id": found.group(1), "url": found.group(0)}
    if screen.closed:
        raise LaunchError("claude exited before a session URL appeared", screen.text())
    raise LaunchError(f"no session URL appeared in {timeout:g} s; a session may still have been created, so look at "
                      "claude.ai/code before starting it again", screen.text())


def stop(pid, screen):
    """End claude and reap it. It may ignore SIGTERM, or block writing to a pty nobody reads."""
    for sent in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sent)
        except OSError:
            pass
        end = time.monotonic() + STOP_WAIT
        while time.monotonic() < end:
            if screen.closed:
                time.sleep(0.02)
            else:
                screen.read(0.05)
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                reaped = pid
            if reaped:
                return


def launch(checkout, brief, *, claude="claude", trust_folder=False, timeout=150.0, log=None):
    """Start the session and return {"session_id", "url"}; raise LaunchError otherwise."""
    pid, fd = pty.fork()
    if pid == 0:
        become_claude(checkout, claude, brief)
    screen = Screen(fd)
    try:
        return watch(screen, SESSION_ID.findall(brief), trust_folder, timeout)
    finally:
        stop(pid, screen)
        os.close(fd)
        if log:
            with Path(log).open("w", encoding="utf-8") as handle:
                handle.write(screen.text())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkout", required=True, help="checkout to start from; the session starts at its branch")
    parser.add_argument("--brief", required=True, help="file holding the task text")
    parser.add_argument("--trust-folder", action="store_true",
                        help="answer Yes when claude asks whether to trust the checkout")
    parser.add_argument("--timeout", type=float, default=150.0, help="seconds to wait for the session URL")
    parser.add_argument("--claude", default=os.environ.get("CLAUDE_CLI", "claude"), help="claude executable")
    parser.add_argument("--log", help="file to write everything claude printed to, as text")
    parser.add_argument("--json", action="store_true", help="print {session_id, url} as JSON")
    args = parser.parse_args(argv)
    try:
        with Path(args.brief).open(encoding="utf-8") as handle:
            brief = handle.read()
    except OSError as error:
        print(f"cannot read the brief: {error}", file=sys.stderr)
        return 2
    if not brief.strip():
        print("the brief is empty", file=sys.stderr)
        return 2
    if brief.startswith("-"):
        print("the brief starts with '-', which claude would read as an option: start it with text", file=sys.stderr)
        return 2
    try:
        result = launch(args.checkout, brief, claude=args.claude, trust_folder=args.trust_folder,
                        timeout=args.timeout, log=args.log)
    except LaunchError as error:
        print(f"cloud_launch: {error}", file=sys.stderr)
        tail = "\n".join(line for line in error.output.splitlines() if line.strip())[-1500:]
        if tail:
            print(tail, file=sys.stderr)
        return 1
    print(json.dumps(result) if args.json else result["url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
