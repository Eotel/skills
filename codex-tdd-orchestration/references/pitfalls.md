# Worktree And Sandbox Pitfalls

Read this only when the selected workflow uses isolated worktrees or restricted
workers. Confirm each condition in the current environment before applying its
mitigation.

## Worktree is outside writable roots

Symptom: reads succeed but edits or patch application fail.

Mitigation: create the worktree under a path the worker's runtime can write,
often inside the repository, and launch the worker with that directory as its
cwd. Run a harmless write probe before implementation.

## Git metadata is outside the sandbox

Symptom: source edits work but `git add`, checkout, or commit fails on worktree
metadata or signing.

Mitigation: leave worker changes uncommitted and let the main agent perform git
operations. State this ownership in the worker contract.

## Dependencies or network are unavailable

Symptom: install, code generation, or remote documentation access fails inside a
worker.

Mitigation: provision required dependencies before dispatch, use repository
offline commands where supported, and give the worker an accepted reduced check.
Report the unrun check; do not describe the fallback as equivalent evidence.

## Wrong checkout or damaged worktree metadata

Symptom: the diff appears in another checkout, the worker reports success with an
empty target diff, or `.git` no longer resolves correctly.

Mitigation: make the first worker check print cwd, branch/base, and repository
root. After completion, inspect the target diff and `git status` from the main
agent. Repair or delete worktree metadata only with explicit authority.

## Local and remote gates differ

Symptom: local lint/test is green but CI uses another command, version, generated
check, or environment.

Mitigation: inspect CI before dispatch and include the relevant command in the
acceptance contract. When a remote-only failure appears, add an equivalent local
gate if the repository can run it reliably.

## Environment pins

Add cache directories, dynamic-library paths, service dependencies, or known
flaky tests only after observing them in the target repository. Keep them in the
topic's environment contract rather than in a global prompt copied everywhere.
