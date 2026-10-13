# Before the first dispatch for a repository

A session that cannot install the repository's dependencies stops at its first
command. Settle these from the repository's CI workflow and dev-environment
files before spending a session on real work.

## What the session can reach

- **GitHub**: only the repository it was started on. A git dependency on
  another private repository fails with `could not read Username`, whatever
  the user's own access. Read how CI fetches those (a token with a git
  `insteadOf` rewrite, a deploy key) and give the environment the same thing:
  the user enters the token as an environment variable, and the session runs
  the same `git config` before installing.
- **Environment variables** exist in the session but not while the setup script
  runs (seen 2026-10-13). Anything that needs one belongs in a command the
  session runs, written into the `~/.claude/CLAUDE.md` the setup script
  creates.
- **`gh`**: REST only. `gh pr` and `gh issue` use GraphQL and are refused; the
  brief says `gh api repos/<owner>/<repo>/...`.
- **System libraries** the dev environment provides (database client headers,
  PDF and font libraries): install them in the setup script.
- **Docker**: installed, daemon not running. The session starts `dockerd`;
  the setup script can pull images under a daemon it starts and stops itself.

## The environment

- Environments are created and edited only in the claude.ai/code environment
  selector. `/remote-env` in the CLI lists their IDs and picks one.
- Keep the setup script under about five minutes or its result is not cached.
- Write the rules every session needs into `~/.claude/CLAUDE.md` from the
  setup script: how to install, the `gh` restriction, the user's conventions
  the repository does not state.

## The checkout to launch from

- A plain clone on the branch the work starts from: the session clones the
  remote at the checkout's current branch.
- Point only this checkout at the environment, in its
  `.claude/settings.local.json`: `{"remote": {"defaultEnvironmentId": "env_..."}}`.
  The user-level default then stays as it was for other repositories.
- The repository's own `SessionStart` hooks run when `claude` starts here and
  may install git hooks into this clone. Launch from it; commit elsewhere.

## First dispatch

Make it a small task whose brief begins with "install the dependencies and run
one test; stop and report the full error if that fails". Fold what it reports
missing into the setup script before dispatching the rest.
