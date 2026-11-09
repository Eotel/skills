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
- Upstream checkout: `/tmp/skills78-yomiyasu-upstream`, branch
  `codex/python-annotations-pathlib`, based on upstream
  `8394e099486aaa2e28cf89992e46f5ebe5ed839d` (v2.5.0).
- Upstream `markdown_visibility.py` and `yomiyasu_hook.py` have local edits;
  their copies under `skills/yomiyasu/scripts/` must remain identical.

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

1. Finish and verify upstream yomiyasu changes under upstream conventions.
2. Refresh the upstream snapshot when the upstream revision is available.
3. Remove all file-specific exclusions; run repository checks and full tests.
4. Commit and push the change; follow the documented Mac distribution procedure
   when the merged revision is available for installation.

## Now And Next

Check `/tmp/skills78-yomiyasu-hook-py39.txt` and finish the hook migration.
Then repair `yomiyasu_check.py`, `yomiyasu_lint.py`, and `yomiyasu_diff.py`.
Keep Python 3.9 compatible annotations with postponed evaluation. Preserve
lexical absolute paths without resolving symlinks, raw readlink comparisons,
and the hook's unregistered fast path. Inspect each JSON boundary before
choosing its internal type representation.

## Blockers And Human Tasks

- Upstream integration requires a reviewed upstream revision; do not silently
  substitute a locally modified vendor snapshot.

## Verification

- `.githooks/pre-commit` checks every Python file with no per-file exclusions.
- `python3 -m unittest discover -s tests` passes.
- Existing script tests continue to exercise observable CLI behavior.
- Upstream checks pass and imported files exactly match the documented revision.
- Installed skill files match the merged source on each configured Mac host,
  or the exact unavailable host and required action are reported.
