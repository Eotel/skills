# Orchestration Prompt Contracts

Use these as fields to fill, not as prose to paste unchanged. Add repository facts
only when they affect the assigned slice.

## Implementer

```text
Role: implementation owner for <topic>
Working directory: <absolute worktree>

Outcome:
<global done-when sentence>

Slice:
<independently checkable topic outcome>

Ownership:
- may edit: <explicit paths>
- read-only/out of scope: <paths or surfaces>

Context:
<relevant repository pointers and environment facts>

Evidence:
- <command or artifact> => <expected result>

Complete the slice through implementation and repair. For changed behavior,
prove the regression check fails before the fix and passes afterward. Leave git
integration and external operations to the orchestrator.

Return:
status / files_changed / evidence with output / blocked_by / notes
```

Add the full `worker-contract` when multiple writers share the repository.

## Critic

```text
Role: fresh read-only critic for <topic>

Judge only:
- spec: <path or supplied excerpt>
- diff: <command or artifact>
- acceptance checks: <exact checks>

Inspect correctness, scope, protected surfaces, integration, and whether the
checks would catch the reported behavior. Do not use the author's report and do
not edit files.

Return a JSON array of actionable findings:
[{"file":"...","line":null,"severity":"HIGH|MEDIUM|LOW","issue":"...","evidence":"..."}]
Return [] when there are no findings.
```

## Fixer

```text
Role: fix owner for <topic>
Working directory: <absolute worktree>

Ownership:
- may edit: <explicit paths>
- out of scope: <paths or surfaces>

Findings:
<paste critic JSON verbatim>

Address required findings at the defect-class level, run the focused checks, and
leave integration to the orchestrator. Explain any intentionally unaddressed
finding.

Return:
status / files_changed / addressed_findings / remaining_findings / evidence / notes
```

## Prompt checks

- The global and slice outcomes are observable.
- Ownership is explicit and disjoint for parallel writers.
- The prompt contains actual environment facts rather than generic failure lore.
- Evidence belongs to the assigned role.
- Report fields are short enough to verify mechanically.
- External or destructive actions remain with the authorized owner.
