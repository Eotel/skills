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
- Original-repository CI needs maintainer approval. Three findings have fix/evidence
  replies and are resolved. The export input-error finding is fixed in `d648f63`;
  reply to comment `4511480920`, then resolve thread `PRRT_kwDOU1BOZs61NK4g`.
- Native Windows 3.9 drive-relative run/verify/export and empty-home word-list
  selection pass at `4b34e3f`. Latest CI also exercises the export regression.
- Downstream branch: `codex/yomiyasu-drive-relative`, based on `1d7a23d`.
  Its 42 imported files match the new fix. The prior snapshot was checked before
  replacement. PR: https://github.com/Eotel/skills/pull/154 (assigned to Eotel).
  CI, code review, security review, and all 582 local tests passed at `2fea184`.
  The latest source's 21 relevant tests pass. The refreshed downstream CLI returns
  exit 2 without a traceback for missing and directory-valued export sources.
  Push this snapshot and obtain checks/reviews on its new head.

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

1. Finish fork CI and the new review, fix actual findings, and obtain original
   CI approval. Keep the PR evidence current.
2. Push the latest verified downstream snapshot, update PR evidence, and monitor
   CI and review before requesting merge authorization.
3. After explicit merge authorization, update the maintained APM pin to the
   merged revision and distribute through the current Mac procedure.
4. Compare changed deployed files and executable bits through actual client
   paths on each target; verify current receipt sets, counts, and bridges.
5. Delete the completed plan and record results in the PR or completing commit.

## Now And Next

Inspect all six fork CI jobs and the upstream review at `d648f63`. Do not treat
the original repository's `action_required` state as successful CI. Review the
existing word-list record-validator and native Windows regression evidence.
Push the downstream export fix and reconcile both PRs' descriptions and reviews.

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
