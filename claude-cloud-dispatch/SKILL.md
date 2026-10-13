---
name: claude-cloud-dispatch
description: Sends a self-contained coding task to a Claude Code cloud session (`claude --cloud`) and brings its PR back reviewed. Use when dispatching an issue or PR task to the cloud, preparing a repository's cloud environment, or steering a cloud session that is already running.
---

# Claude Cloud Dispatch

A cloud session works on a fresh VM that holds one repository and the text you
send. Nothing on this machine reaches it: no `~/.claude` rules, skills, or
memory, no sibling checkouts, no local lane's context. The brief and the
environment's setup carry everything it must know.

## Where

- `scripts/cloud_launch.py`: starts one session from a checkout and prints its
  URL. `claude --cloud` needs a terminal and asks to trust a checkout it has
  not seen; the script gives it a pty and answers that prompt only with
  `--trust-folder`.
- `references/preflight.md`: read before the first dispatch for a repository,
  and when a session stops during setup (a dependency it cannot fetch, a
  missing library, no Docker daemon).
- `references/brief.md`: read when writing the task text.
- `references/follow-up.md`: read when steering a running session, and when
  its PR appears.
- Flags, limits, and what the VM provides change: check
  <https://code.claude.com/docs/en/claude-code-on-the-web.md> and
  <https://code.claude.com/docs/en/cloud-environments.md> rather than this
  skill.

## Done

Every dispatched task has its session ID written where the next session will
look. Every PR a session opened was reviewed outside that session, includes the
current base branch, has green CI, and is merged or with its reviewer as the
repository's practice says. Trackers the session cannot reach are updated.

## Boundary

- A session spends the user's cloud usage and acts on GitHub as the user:
  dispatch the tasks they named, and say in the brief whether it may merge.
- Typing a token or secret into the environment settings, and approving a
  permission prompt a session raises (adding a repository, for one), are the
  user's. Prepare the rest and name the one thing they must do.
- Dispatch only work no local lane holds: check the open PRs and each local
  worktree's branch, unpushed ones included.
