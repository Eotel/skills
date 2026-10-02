---
name: orca-session-triage
description: Sweep Orca agent sessions, relay batched answers, and close finished tabs and worktrees. Use when the user asks which Orca sessions are finished, stuck, or waiting on them, wants their pending questions answered in one pass, or wants finished sessions, tabs, or worktrees closed.
---

# Orca Session Triage

Find Orca worktrees whose agents finished, wait on a human, or never got a task;
relay the user's answers in one batch; close what the user approves.

## Where

- `scripts/scan_sessions.py`: classifies every worktree from Orca state plus
  Claude and Codex transcripts, and lists the tabs it could close (`closable`).
  Orca's agent state misses prose questions and Codex prompts, so read each
  `waiting` and `idle` row's message yourself.
- `scripts/rm_check.py`: preflight before closing a worktree. Its verdict beats
  an agent's own "done" or "uncommitted".
- `references/delivery.md`: batching questions and sending answers.
- `references/answer-page.md`: one page for many answers.
- `references/cleanup.md`: closing approved worktrees and tabs.

## Done

Every agent worktree has a class you checked, the user saw every live question,
each sent answer reached `turn_started` or is reported as queued, each
approved worktree is gone from `orca worktree ps` and from disk, and each
approved tab is gone from `orca terminal list`.

## Boundary

- Scanning, reading transcripts, and `rm_check.py` are read-only: run them
  without asking.
- Removing worktrees, branches, containers, or volumes, closing tabs, and
  giving an unstarted agent a task need the user's choice per item.
- Relay answers verbatim; the session answers the user's follow-up questions.
