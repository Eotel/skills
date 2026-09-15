# Astra Cleanup Publishing

## Goal

Publish the completed GPT-6 Astra skill and prompt cleanup, persist the global
Codex instructions in chezmoi, and refresh the APM-managed installed skills so
the running environment no longer uses the previous GPT-5.x guidance.

## Constraints and Non-Goals

- Preserve unrelated work in both repositories.
- Keep `~/.apm/apm.lock.yaml` runtime-owned and out of chezmoi source control.
- Do not merge or close anything; direct pushes to the already-tracked `main`
  branches are the requested publication path.
- Preserve the existing `devenv-init` pinning policy by advancing its tag rather
  than silently changing it to an unpinned dependency.
- Do not copy APM-managed skill directories into chezmoi.

## Plan of Work

1. Re-run the skills repository validation and review the complete staged diff.
2. Commit and push the skills cleanup to `origin/main`.
3. Tag the published repository as `v0.2.0` for the pinned `devenv-init`
   dependency and push the tag.
4. In `chezmoi-dotfiles`, absorb `~/.codex/AGENTS.md` into its plain-file source,
   update stale APM comments, and advance `devenv-init` to `v0.2.0`.
5. Run `just test`, targeted chezmoi comparisons, and manifest checks.
6. Commit and push the chezmoi changes to `origin/main`.
7. Apply the managed manifest, run the global APM update, and verify deployed
   descriptions/files and the clean chezmoi relationship.
8. Record evidence and move this plan to `completed/` before the final skills
   commit amendment/push if required.

## Progress

- [x] Confirmed both repositories are on `main`, clean relative to their remotes,
  and have no pre-existing chezmoi worktree changes.
- [x] Published `Eotel/skills` and tag `v0.2.0`.
- [x] Persisted AGENTS and manifest changes in `chezmoi-dotfiles`.
- [x] Refreshed APM deployment and verified installed skills.
- [x] Completed and archived this plan.

## Verification

- OpenAI `quick_validate.py` for all 16 skills.
- Markdown link, JSON, inline JavaScript, shell syntax, catalog count, and
  `git diff --check` validations recorded in the parent cleanup plan.
- `just test` in `chezmoi-dotfiles` after pipeline/manifest changes.
- `cmp` between managed and destination `AGENTS.md`.
- `apm deps list -g`, lockfile resolved commit inspection, and deployed
  `codex-prompting` content checks after update.
- `git status`, remote ahead/behind, and pushed commit/tag checks in both repos.

## Decision Log

- 2026-09-15: Use `v0.2.0` because the existing manifest intentionally pins
  `devenv-init`; changing to branch tracking would alter its update policy.
- 2026-09-15: Keep the global APM lockfile untracked, matching the documented
  per-host runtime ownership in `run_after_apm-install.sh`.

## Surprises and Discoveries

- The installed `codex-prompting` still resolves commit `e1ce577` and contains
  the deleted GPT-5.x model guidance, so publishing alone is not deployment.
- The chezmoi manifest still mentions APM 0.14 and the deleted
  `changes-from-mizchi.md` file.
- APM 0.29 rewrote the live mattpocock SHA pins toward release tag `v1.2.3`
  during a global update, then exited because the rewritten manifest and lock
  SHA differed. The repository runbook already documents this defect. Restoring
  the managed manifest and running lockfile-faithful `apm install` completed
  deployment without accepting the pin downgrade.

## Outcomes and Retrospective

- Published skills commit `2bcca9f` to `origin/main` and annotated tag `v0.2.0`.
- Published chezmoi commit `1c8cb7a` to `origin/main`; its managed AGENTS and APM
  manifest match the live destinations.
- `just test` passed all 31 chezmoi Bats checks, including the APM/bridge
  disjointness invariants and secret scanning.
- The runtime lock contains all 16 Eotel skills at commit `2bcca9f`, with
  `devenv-init` resolved from `v0.2.0`.
- Deployed Codex and Claude bridge links point to the updated canonical
  `~/.agents/skills/codex-prompting`. Deleted GPT-5.x guidance, the duplicate
  APM Japanese file, and unsupported devenv placeholders are absent.
- Both published repositories matched `origin/main` after fetch. No merge,
  close, production write, or lockfile commit was performed.
