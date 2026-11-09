# Exec plan migration lifecycle

Keep `exec-plan-migration` available while a target still needs the legacy
migration workflow. Completed-plan archives and history-style active plans are
confirmed retirement blockers.

## Inventory scope and records

Discover targets from the distribution manifest, repository-level skill settings,
and the checkout catalog on every host served by that distribution. Search
tracked rules, templates, hooks, CI, and plan directories as well as literal
`exec-plan-migration` references. A checkout catalog alone does not establish
that the target set is complete.

Identify a repository by its normalized remote identity; retain separate clone
and worktree observations. Use the repository's authoritative default branch and
record its full commit SHA. A cached remote-tracking ref is provisional until
compared with the remote. Preserve local-only and inaccessible entries as
unresolved, with the evidence needed to revisit them.

Each target's record contains:

| Field | Required evidence |
| --- | --- |
| Identity and scope | Repository identity, why it is included, clones/worktrees, and the checked source revision |
| Plan state | Completed archives, history-style active plans, other plan-check failures, and old lifecycle enforcement |
| Callers | Operational settings or instructions that need the migration skill, distinguished from packaging, examples, and tests |
| Unfinished work | Open PRs and their changed plan paths; unmerged local refs; worktree changes reported without reading session logs |
| Gaps and next action | Inaccessible or stale evidence, the unresolved decision, and what must be checked next |

An unmerged local ref is evidence of a differing tree, not proof of an open PR.
Check the tracker before assigning it to live work. Open-PR file lists and local
refs cover different surfaces; record incomplete pagination and unavailable refs.
Untracked plans belong to in-flight work and stay untouched.

## Confirmed public evidence

The public default-branch snapshot below provides concrete blockers and controls.
Refresh it before making a retirement decision. The inventory scope must also be
closed using the records above.

Evidence snapshot: 2026-11-09T03:05:16Z. Repository visibility is verified as public.

| Repository and checked commit | Completed | Active / with history headings | Plan check | Unfinished plan work |
| --- | ---: | ---: | --- | --- |
| [Eotel/blogs](https://github.com/Eotel/blogs/tree/5f7f47c331495b5a0ed0592767ce71bf3786a4b4) | 0 | 1 / 0 | 1 finding | No open PR changes plans |
| [Eotel/dual-start-button](https://github.com/Eotel/dual-start-button/tree/33300a47fa598e1d187b0a534bdc93ba2ce08102) | 1 | 0 / 0 | 1 finding | No open PR changes plans |
| [Eotel/skills](https://github.com/Eotel/skills/tree/08d2dd301ef8f0003b7901f9c117b6ecd1553b38) | 0 | 0 / 0 | pass | [PR #151](https://github.com/Eotel/skills/pull/151) |
| [shiftone-ai/caption-relay](https://github.com/shiftone-ai/caption-relay/tree/331b5da0be0b75e287fc7d9f3a8dbc5736e84cc7) | 0 | 2 / 0 | pass | No open PR changes plans; four plan paths differ in cached unmerged refs |
| [spin-dd/django-api-base-2025](https://github.com/spin-dd/django-api-base-2025/tree/c3edaa2998648dade136bd7970329926c6b23c64) | 1 | 0 / 0 | 1 finding | No open PR changes plans |
| [Eotel/qr-relay](https://github.com/Eotel/qr-relay/tree/caf2e21393328938dc341111ff23b1cd17fc91f5) | 5 | 14 / 14 | 71 findings | No open PR changes plans |

`Eotel/blogs` has one 395-line active plan and no detected history heading.
`Eotel/qr-relay` also retains a template that requires moving finished plans
into `completed/`, and instructions reference a completed plan.
`Eotel/dual-start-button`, `Eotel/qr-relay`, and
`spin-dd/django-api-base-2025` therefore remain migration targets.

The checker is `agentic-docs/scripts/check_plans.py`, run against the plan trees
at the recorded commits. A zero result covers that tree's plan shape; it does not
establish target coverage, absence of callers, or the status of other branches.
A line-limit failure is recorded separately from a legacy archive or history
heading.

The existing per-repository inventory command remains useful in a dedicated
checkout of the default branch:

```sh
exec-plan-migration/scripts/migrate_plans.py --root . --base origin/main
```

Use the actual default-branch ref in place of `origin/main`. Leave `--apply`
off for inventory. Inspect old enforcement in rules, templates, hooks, and CI in
addition to the command's report. Read only referenced completed plans when
resolving their remaining knowledge.

## Retirement gates

Retirement is ready for review when all five conditions hold:

1. **Coverage is closed.** Every declared host and repository is accounted for;
   stale, inaccessible, local-only, and otherwise unresolved entries have been
   resolved. An empty search result with incomplete coverage cannot mean unused.
2. **Defaults use the current lifecycle.** Targets have no completed-plan archive,
   no history-style active plans, and no old enforcement that recreates them.
   Plan checks pass against the recorded default commits. Durable knowledge and
   links from removed plans have valid destinations.
3. **Unfinished work is reconciled.** Plan changes in open PRs, unmerged refs, and
   worktrees have an explicit disposition. Deferred legacy plans are converted
   when resumed; while that conversion is still needed, retirement stays blocked.
4. **Operational dependence has ended.** Settings and live instructions no longer
   require this skill. Its own catalog entry, tests, and APM declaration are
   removal work, rather than evidence of ongoing operational use. Keep
   `plan-exec`, `agentic-docs`, and the current-lifecycle checker while their
   other consumers remain.
5. **Evidence is current and removal is authorized.** Recheck changed defaults
   and PRs, review the exact withdrawal diff and expected installed-set change,
   and obtain explicit authorization for deletion and distribution changes.

Inventory and documentation can be complete while retirement remains blocked.
Keep the skill and its deployment declaration in that case.

## Distribution withdrawal

Use the distribution's maintained installation runbook, reached from
[Install](../../README.md#install), for host enumeration and the current commands.
The sequence below adds the retirement conditions; it does not replace that
runbook or authorize an installation change.

1. **Prepare the coordinated changes.** Identify the skill definition, its owned
   migration script and tests, catalog entries, callers, and distribution
   declaration. Keep the shared current-lifecycle checker and its tests. Review
   both the source and distribution changes against fixed revisions, including
   any check that requires every source skill to be declared.
2. **Keep every declared source resolvable.** Do not delete a path still followed
   by a live distribution declaration. If source and distribution publication
   cannot be atomic, use an explicitly reviewed pin to the last existing source
   during the transition. Verify the installed package manager supports that
   declaration, and remove the interim declaration in the final change.
3. **Prove support for the intended removal.** Back up manifests, generated locks,
   deployment receipts, the installed skill set, and client bridges. The guard
   must validate the exact authorized removal while preserving other owners.
   A guard that rejects reductions blocks withdrawal until a reviewed removal
   path is verified with offline fixtures. Do not bypass it, erase its baseline,
   or assume that a successful ordinary refresh supports retirement.
4. **Publish and apply the approved changes.** Remove only this skill's owned
   resources and declaration in the agreed order. Regenerate locks and receipts
   using the supported distribution tooling; do not hand-edit generated state.
   Use the guarded full user-scope workflow rather than a package-specific
   install/update. Stop on a rejected receipt, unexpected set change, unresolved
   source, or host that cannot be reached.
5. **Verify every declared host and client.** Check the live manifest, lock,
   receipt ownership, canonical skill directory, and both Claude Code and Codex
   discovery slots. The retired skill and its bridges are absent, remaining
   dependency content is intact, and the installed set differs only by the
   approved removals. Treat an independent client copy as its own owned resource;
   do not overwrite or delete it without authorization. Record unavailable hosts
   as unfinished. Restoring saved state also requires authorization.

Check the actual installed content and links, not only update messages or
resolved-revision metadata. A source-only deletion, one updated host, or an APM
success message does not complete withdrawal.
