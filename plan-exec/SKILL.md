---
name: plan-exec
description: Use this skill when the user asks for an implementation plan, a "plan first", or starts work that crosses subsystems, changes API/DB contracts, touches auth/security/reliability, or will span multiple turns. Mirrors Claude Code's plan-file behavior by writing a repo-local plan to docs/exec-plans/active/{yyyy-mm-dd}-{slug}.md, getting user approval, then executing against it. Skip for trivial fixes, single-file copy edits, and obvious bug fixes - just do those directly.
---

# plan-exec — write a plan before large work

Some repos have Claude Code plan mode available, but Codex sessions need an
explicit file-backed workflow. This skill uses the current repo's
`docs/exec-plans/` system so a plan can survive context compaction, handoff,
and later review.

When invoked, write a plan file in the target repo, get user approval, then
execute against that plan.

Reference reading:

- Repo policy, when present: `docs/PLANS.md`
- Plan template, when present: `docs/exec-plans/plan-template.md`
- Background pattern: [OpenAI cookbook — Codex exec plans](https://developers.openai.com/cookbook/articles/codex_exec_plans)

## When to use this skill

Trigger when any of the following is true:

- The user says "plan first", "make a plan", "let's plan this", "実装計画", "exec plan", "/plan".
- The change crosses two or more of: backend, frontend (admin / principal-web), agent-worker, infra, docs.
- The change touches auth, an API contract, a DB migration, a public type, or a billing/security boundary.
- The work is expected to span multiple turns or multiple commits.
- The user wants a paper trail of the decision (e.g., "後から判断理由を追跡したい").

## When NOT to use it

Skip and just do the work when:

- Single-file edit, typo, lint fix, formatter run.
- Obvious bug fix where the diff is the explanation.
- Renaming one local variable.
- Updating one copy string.
- The user explicitly says "don't bother planning, just fix it".

A plan that wraps a one-line fix is friction, not value.

## Lifecycle (matches `docs/PLANS.md`)

Follow `docs/PLANS.md` and start from
`docs/exec-plans/plan-template.md`. The Codex-specific rule is approval:
after writing the plan file, stop and ask the user to approve before executing
unless the user has explicitly said to proceed in auto mode.

## File format

Use the current repo's `docs/exec-plans/plan-template.md` if it exists. If the
repo has no template, create a compact Markdown plan with these sections:

- Goal
- Constraints and Non-Goals
- Plan of Work
- Progress
- Verification
- Decision Log
- Surprises and Discoveries
- Outcomes and Retrospective

For small but still plan-worthy changes, sections may be short. For multi-hour,
cross-subsystem, or multi-agent work, keep the plan self-contained enough to
resume without chat history.

## Filename convention

`{yyyy-mm-dd}-{slug}.md`

- Date: today, in the local repo timezone (Asia/Tokyo here).
- If the session provides an explicit timezone, use that timezone for the date.
- Slug: 2–6 kebab-case words, ASCII only, no trailing date suffixes.
- Examples:
  - `2026-04-28-managementscreens-split.md`
  - `2026-05-02-livekit-token-rotation.md`
  - `2026-05-15-billing-webhook-idempotency.md`

If a plan is a sub-track of an existing one, prefix the slug with the parent (`2026-05-04-managementscreens-split-phase-b.md`).

## Approval ritual

After writing the plan, surface it in chat with:

> "Plan written to `docs/exec-plans/active/{file}`. Approve to start, or push back on any section."

Then **stop**. Wait for the user's explicit approval. If the user pushes back on the plan, edit the plan file (not the chat) — the file is the source of truth.

If the user is in `auto` mode and tells you to proceed without explicit approval per turn, you may execute directly. State that you're proceeding under auto-mode and link the plan file in your first execution turn so the user can interrupt.

## Updating during execution

While executing:

- Keep `Progress` current at every stopping point. Split partially completed work into done and remaining pieces.
- If a step turns out wrong, edit the plan's Plan of Work / Concrete Steps to reflect what you actually did. Don't leave the plan as a fiction.
- If a new risk surfaces, add it to Constraints with a one-line note.
- If an unexpected behavior or repo convention shapes the implementation, record it in `Surprises & Discoveries` with concise evidence.
- If a meaningful choice is made, add it to `Decision Log` with rationale.
- If scope expands, add the new scope as a new section or a new sub-plan — don't quietly fold it in.
- At completion or major milestones, update `Outcomes & Retrospective` with what changed, what remains, and what was verified.

A plan that disagrees with the diff is worse than no plan.

## Multi-agent / parallel work

When this plan dispatches subagents (e.g., a Codex worker spawned via tool, or a Claude Code Agent call):

- The plan stays the source of truth. Subagents read it, don't replace it.
- Verbatim-extraction work needs an independent reviewer pass — workers can hallucinate. Compare extracted code against `git show HEAD:` of the original; don't trust the worker's self-report.
- Mark each `Progress` item `done` / `in-progress` / `blocked` as work proceeds so the next agent picks up cleanly.

## Promotion checklist (at completion)

Before moving the plan to `completed/`, ask:

- Did this work change a durable convention? Update `AGENTS.md`, `CLAUDE.md`,
  or `docs/design-docs/`.
- Did it surface a rule worth enforcing? Add an ast-grep rule in `rules/`, a
  lint rule, a type check, or a test.
- Did it surface a regression worth catching? Add a test.
- Did it leave follow-ups? File a new exec plan under `active/` referencing
  this one.

The completed plan is the audit trail. The promoted artifacts are the institutional memory.

## Anti-patterns

- Writing a plan in chat instead of in the file. The file IS the plan; chat is the conversation about it.
- Treating the plan as immutable. Plans drift; that's fine, just keep them honest.
- Wrapping a typo fix in a plan. Friction without value.
- Folding a parallel feature into an unrelated plan to avoid creating a second file. Two unrelated plans = two files.
- Leaving a plan in `active/` after the work shipped. Move it to `completed/` and update `Outcomes & Retrospective`.
- Naming a plan `plan.md` or `refactor.md`. Use the dated kebab slug.
