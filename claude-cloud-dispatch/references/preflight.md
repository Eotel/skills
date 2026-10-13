# Before the first dispatch for a repository

A session that cannot install the repository's dependencies stops at its first
command. Settle these from the repository's CI workflow and dev-environment
files before spending a session on real work.

## What the session can reach

- **GitHub**: API requests reach only the repositories attached to the session
  (docs). A git dependency on another private repository of the same
  organization failed with `could not read Username` (seen 2026-10-13). Read
  how CI fetches those (a token with a git `insteadOf` rewrite, a deploy key)
  and give the environment the same thing: the user enters the token as an
  environment variable, and the session runs the same `git config` before
  installing. A network secret is never attached to GitHub requests (docs), and
  everyone who uses an environment can read its variables, so the token goes on
  the user's own environment, never a shared one.
- **Environment variables** exist in the session but not while the setup script
  runs (seen 2026-10-13). Anything that needs one belongs in a command the
  session runs, written into the `~/.claude/CLAUDE.md` the setup script
  creates.
- **`gh`**: REST only. `gh pr` and `gh issue` use GraphQL and are refused
  (docs); the session uses `gh api repos/<owner>/<repo>/...`.
- **System libraries** the dev environment provides (database client headers,
  PDF and font libraries): install them in the setup script.
- **Docker**: installed, daemon not running (seen 2026-10-13). The session
  starts `dockerd`; the setup script can pull images under a daemon it starts
  and stops itself, and the cache keeps them.

## The environment

- Environments are created and edited in the environment selector at
  claude.ai/code or in the Desktop app's prompt box; no CLI edits them.
  `/remote-env` picks the default; its picker showed the IDs (seen 2026-10-13).
- Keep the setup script under about five minutes or its result is not cached.
- Rules every session in the environment needs go into the `~/.claude/CLAUDE.md`
  the setup script writes: how to install, the `gh` restriction, the user's
  conventions the repository does not state. The brief carries the rest.

## The checkout to launch from

- A clone you made for launching, on the branch the work starts from, with that
  branch pushed and nothing uncommitted. The session clones the GitHub remote
  at the checkout's current branch, so local commits are absent. Where the
  Claude GitHub App is not installed on the repository, the CLI uploads the
  checkout instead, uncommitted changes to tracked files included (docs).
- Point only this checkout at the environment, in its
  `.claude/settings.local.json`: `{"remote": {"defaultEnvironmentId": "env_..."}}`.
  The user-level default then stays as it was for other repositories.
- The repository's own `SessionStart` hooks run when `claude` starts here and
  may install git hooks into this clone (seen 2026-10-13). Launch from it;
  commit from a worktree the repository's own setup prepared.

## First dispatch

Make it a small task whose brief begins with "install the dependencies and run
one test; stop and report the full error if that fails". Fold what it reports
missing into the setup script before dispatching the rest.
