# Distribute the merged Python skill fixes

## Goal

Install the merged changes from Eotel/skills#151 on all configured Macs and
verify the files each client loads.

## Context

- PR: https://github.com/Eotel/skills/pull/151
- The source has no Ruff ANN/PTH per-file or ty file exclusions.
- `yomiyasu` is the v2.5.0 snapshot from nanaism/yomiyasu#54 at
  `d6ba9e85c76d6ba570c4c465de63b29cccb671c2`. All 42 imported files match
  that revision. Codex plugin metadata is intentionally omitted.
- The chezmoi manifest pins `Eotel/skills/yomiyasu` to
  `c8413a5a2986a5da16af087820348eddfb3b3489`; distribution needs a new pin.
- Changed packages: adoption-triage, claude-cloud-dispatch, natural-japanese,
  orca-session-triage, review-response, and yomiyasu.
- The targets are the three Macs listed in the current distribution procedure.
  Remote connections work. Installed contents remain unverified.
- The local `apm-prune-guard.py receipt-check` reports that the receipt's
  installation path or identifier does not match. Recovery is required before
  any global APM install or update.

## Scope and Non-Goals

- Merge the reviewed PR only after explicit authorization, then update the
  maintained APM pin, distribute the packages, and verify client paths.
- No Nix changes, unrelated skill edits, or upstream PR merge.

## Constraints From Decisions

- Read the chezmoi source's AGENTS.md, CLAUDE.md, and
  `docs/skills-pipeline.md` before changing its files or applying state.
- Preserve existing changes and untracked files. Stage explicit paths only.
- Do not run package-specific user-scope APM install/update. Do not run a global
  operation with missing or incomplete receipts. Use the prescribed `recover`
  procedure to back up manifests, installed skills, and receipts, retire the
  generated lock, and regenerate the full installation.
- Compare canonical skill counts and broken bridge symlinks before and after.
- Updating the check scripts invalidates older check records by script SHA;
  rerun `run` before `verify` when it reports `tool_changed`.

## Remaining Work

1. Obtain explicit authorization to merge Eotel/skills#151. Re-read its current
   checks and review threads immediately before merging.
2. Inspect the chezmoi source checkout, change the yomiyasu pin and version
   comment to the merged source, run its required checks, and commit/push.
3. Inspect each Mac's source checkout and pull with `--ff-only` when safe.
   Apply the intended guard/manifest state before APM recovery and installation,
   following the Mac distribution section of `docs/skills-pipeline.md`.
4. Verify all six packages against the merged files through their canonical
   directories and each client's resolved path. Check counts, broken bridges,
   executable entrypoints, and the deployed yomiyasu version.

## Now And Next

Wait for explicit merge authorization. Then inspect PR #151's latest CI and
review threads, merge it, and use its actual merged SHA for the APM pin.
Before installation, read each host's live manifest and lockfile and inspect
the recovery command's current help. Do not use a remembered generated lock.

## Blockers And Human Tasks

- Merge is not yet authorized.
- Upstream Windows CI needs maintainer approval; local checks cover macOS
  Python 3.9/3.12/3.14 and simulated Windows branches. This limitation is recorded
  in the upstream PR and must remain explicit in the integration PR.

## Verification

- All targets resolve the intended merged source and the updated yomiyasu pin.
- Every changed deployed file matches that source through the actual client
  path, with no newly broken bridges or unexplained canonical skill losses.
- Any unavailable target is named with the exact required action.
