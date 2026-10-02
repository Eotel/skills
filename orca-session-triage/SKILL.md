---
name: orca-session-triage
description: Sweep Orca agent sessions, relay batched answers and next steps, and close finished tabs and worktrees. Use when the user asks which Orca sessions are finished, stalled, or waiting on them, wants their pending questions answered in one pass, or wants finished sessions, tabs, or worktrees closed.
---

# Orca Session Triage

Find Orca worktrees whose agents finished, stalled, wait on a human, or never
got a task; relay the user's answers and next steps in one batch; close what the
user approves.

## Where

- `scripts/scan_sessions.py`: classifies every worktree from Orca state plus
  Claude and Codex transcripts, lists the tabs it could close (`closable`), and
  gives each `stalled` row its `git` and `pr` state. Jev (TypeSafe) judges
  whether each idle agent's final message waits on the user (`asks`, a
  probability); its key comes from `TYPESAFE_API_KEY` or
  `~/.config/typesafe/api_key`. A message Jev could not judge comes back
  `waiting` with "final message not judged", and one that may ask (0.3 up to
  the threshold) keeps merged work `stalled`. A decision marker outlives its
  answer, so read each `waiting` and `stalled` row's latest message yourself.
- `scripts/rm_check.py`: preflight before closing a worktree. Its verdict beats
  an agent's own "done" or "uncommitted".
- `references/delivery.md`: batching questions and sending answers.
- `references/next-step.md`: what each `stalled` worktree needs next.
- `references/answer-page.md`: one page for many answers.
- `references/cleanup.md`: closing worktrees and tabs.

## Done

Every agent worktree has a class you checked, the user saw every live question
and every `stalled` row's proposed next step, each sent answer or step reached
`turn_started` or is reported as queued, each
approved or left-behind worktree is gone from `orca worktree ps` and from disk,
and each approved tab is gone from `orca terminal list`.

## Boundary

- Scanning, reading transcripts, and `rm_check.py` are read-only: run them
  without asking. The scan sends each idle agent's final message (its last
  4,000 characters) to TypeSafe's API; without a key it sends nothing.
- Removing worktrees, branches, containers, or volumes, closing tabs, merging
  or requesting review, and giving an unstarted agent a task need the user's
  choice per item. The exception is a finished worktree with no meaningful tab
  that `rm_check.py` clears: remove it, then report it.
- Relay answers verbatim; the session answers the user's follow-up questions.
