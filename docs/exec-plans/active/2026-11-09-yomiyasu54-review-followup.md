# Complete the yomiyasu PR review fix

## Goal

Address nanaism/yomiyasu#54 review findings, verify CI and review results, and
distribute the exact verified snapshot to the configured Macs.

## Context

- PR: https://github.com/nanaism/yomiyasu/pull/54
- Review finding: https://github.com/nanaism/yomiyasu/pull/54#discussion_r4509108009
- Upstream fix: `d648f6339f30842ff095fc0f6d1304a9ad37dd2b`, on
  `codex/python-annotations-pathlib` in `/tmp/skills78-yomiyasu-publish`.
- Python 3.9 WindowsPath reproduced a relative word-list record rejected by
  the real record validator. Native Windows 3.9 run/verify/export now passes.
- Identical-commit fork CI: https://github.com/Eotel/yomiyasu/actions/runs/42598674574
- Original-repository CI needs maintainer approval:
  https://github.com/nanaism/yomiyasu/actions/runs/42598648263
- Review at `d648f63` is complete with no findings. All four earlier findings
  have fixes/evidence replies and resolved conversations.
- Native Windows 3.9 drive-relative run/verify/export and empty-home word-list
  selection pass at `d648f63`. Ruff/ty and distribution equality also pass.
  All six fork CI jobs pass. Full suites: Python 3.9 has 2437 passed/5 skipped;
  Python 3.14 has 2434 passed/8 skipped; Windows 3.12 has 2416 passed/26 skipped.
- Downstream branch: `codex/yomiyasu-drive-relative`, based on `1d7a23d`.
  Its 42 imported files match the new fix. The prior snapshot was checked before
  replacement. PR: https://github.com/Eotel/skills/pull/154 (assigned to Eotel).
  CI, code review, and security review pass at `0973902`; its code incorporates
  main `7bbc568`. All 585 local tests and 93 relevant upstream tests pass.
  The downstream CLI returns exit 2 without a traceback for missing and
  directory-valued export sources. Only this plan changes after verified code `581a861`.
- Read-only deployment preflight found valid receipts and no broken client links
  on all three Macs. Managed content matches receipts, with only the documented
  CMD line-ending representation difference. Inspect fresh state before updating.

## Scope and Non-Goals

- Preserve drive-relative paths for word lists, state, and hooks without
  resolving symlinks; keep root/distributed upstream scripts identical.
- Preserve literal tilde paths when Windows home configuration is empty, for
  both USERPROFILE and the HOMEDRIVE/HOMEPATH fallback.
- Preserve verification input-error exit codes through export. The dictionary API
  reports rejected arguments as ValueError instead of an assertion or indexing error.
- Keep per-file Ruff and ty exclusions absent. Two documented PTH100 line
  exceptions retain native lexical path semantics where pathlib is incomplete.
- No upstream PR merge or unrelated feature changes.

## Remaining Work

1. Obtain original CI approval. Its `action_required` state is an external blocker;
   equivalent-commit fork CI is green and the maintainer has been asked to approve.
2. Keep both PR descriptions current and request merge authorization for #154
   once its final head's CI and review finish.
3. After explicit merge authorization, update the maintained APM pin to the
   merged revision and distribute through the current Mac procedure.
4. Compare changed deployed files and executable bits through actual client
   paths on each target; verify current receipt sets, counts, and bridges.
5. Delete the completed plan and record results in the PR or completing commit.

## Now And Next

Verify the final downstream head's checks, post both PR descriptions with the
completed full-suite results, and obtain #154 merge approval. Do not treat the
original repository's `action_required` state as successful CI. After approval,
read fresh deployment state and perform the documented Mac rollout.

## Constraints and Human Tasks

- Upstream CI execution requires a maintainer's approval.
- PR merge requires explicit authorization; previous authorization was for #151.
- Follow the chezmoi source's current instructions before changing its pin.
  Inspect fresh manifests, locks, receipts, and deployed files; do not reuse the
  prior deployment's receipt counts as an assumed baseline.
- Never run package-specific user-scope APM install/update. Preserve local
  changes and use backed-up full recovery when required by the pipeline.

## Verification

- Native Windows 3.9 run/verify/export succeeds with another drive's word list.
- Supported full suites, Ruff/ty, distribution equality, and review checks pass.
- Downstream files match the documented revision byte for byte and by executable
  bit, with no per-file exclusions.
- Every requested target loads that source, or the exact remaining external
  approval is stated without claiming completion.
