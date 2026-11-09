# Remove Python lint and type exclusions (#78)

## Goal

Remove file-specific ANN/PTH and ty exclusions while preserving script behavior.

## Context

- Issue: https://github.com/Eotel/skills/issues/78
- `yomiyasu/` is an unmodified upstream v2.4.0 snapshot. Its Python changes
  must be made and verified upstream before refreshing the complete snapshot.
- The upstream main branch is v2.5.0 and still lacks annotations.
- First-party scripts and tests have annotations; only the five imported
  `yomiyasu` files retain lint/type exclusions. JSONL events and projected
  proposals have distinct schemas, and invalid JSONL field types are rejected.
- Upstream base: `8394e099486aaa2e28cf89992e46f5ebe5ed839d` (v2.5.0).
- Upstream implementation is published in nanaism/yomiyasu#54 (draft),
  commit `453b6135c8cca506ddb9b97d268cc598637d3dc9`.
- Integration checkout: `/tmp/skills78-yomiyasu-publish`, branch
  `codex/python-annotations-pathlib`. All five distributed scripts pass Ruff
  ANN/PTH and ty for Python 3.9. Their root copies are byte-identical.
- Its local follow-up commit is `d6ba9e85c76d6ba570c4c465de63b29cccb671c2`;
  publish it after both full suites finish successfully.
- The six repaired test files pass independently under Python 3.9 and 3.14
  (113 tests each). First-party cold review returned no findings; its focused
  independent checks passed 187 tests.
- Filesystem predicates preserve the prior false-on-OSError behavior without
  hiding ordinary read/stat/write errors. Real permission regressions pass on
  Python 3.9, 3.12, and 3.14. Fresh cold review returned no findings; full
  Python 3.9 and 3.14 suites are running on the repaired result.
- The lead independently passed the four permission-related files on Python
  3.12 (`130 passed, 158 subtests passed`) and the final three adapter/source-rule
  files (`71 passed, 77 subtests passed`).

## Scope and Non-Goals

- Annotate excluded scripts and tests, validate external JSON at its boundary,
  migrate obsolete path calls, and remove the corresponding exclusions.
- Preserve CLI output and workflow decisions. Keep the documented raw-readlink
  exceptions, which preserve the literal target string.
- No upstream merge or issue closure without explicit authorization.

## Constraints From Decisions

- Standard library only for shipped Python scripts; Python 3.12 entrypoints.
- Do not replace errors with Any, casts, or suppression comments.
- Preserve upstream snapshot equality and record the exact imported revision.

## Remaining Work

1. Finish both full upstream suites and publish the verified follow-up commit.
2. After the version choice, refresh the complete snapshot from its published
   revision.
3. Remove all file-specific exclusions; run repository checks and full tests.
4. Commit and push the change; follow the documented Mac distribution procedure
   when the merged revision is available for installation. The chezmoi manifest
   pins `Eotel/skills/yomiyasu` to `c8413a5a2986a5da16af087820348eddfb3b3489`;
   bump that pin to the merged revision before distribution.

## Now And Next

Source and tests are frozen while both full upstream suites run against
`d6ba9e8`. Git and public writes belong to the lead.

1. Inspect both complete suite results.
2. Publish the follow-up commit and update the upstream PR with final evidence.
3. After the version choice, verify exact snapshot equality, repository hooks,
   and all repository unittests.

## Blockers And Human Tasks

- Upstream CI awaits maintainer approval. The fork has no registered Actions
  workflows; direct workflow enable/dispatch returns 404.
- The user is choosing whether to import upstream v2.5.0, which also changes
  writing checks, or preserve v2.4.0 behavior with a separate backport.
- Do not merge or close either PR without explicit authorization.

## Verification

- `.githooks/pre-commit` checks every Python file with no per-file exclusions.
- `python3 -m unittest discover -s tests` passes.
- Existing script tests continue to exercise observable CLI behavior.
- Upstream checks pass and imported files exactly match the documented revision.
- Installed skill files match the merged source on each configured Mac host,
  or the exact unavailable host and required action are reported.
