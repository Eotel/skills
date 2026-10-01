---
name: orca-session-triage
description: Sweep every Orca-managed agent session to find finished worktrees left open, sessions waiting on a human, and agents that never received a task, then batch the open questions into one answer pass and close what is done. Use for a fleet-wide Orca check, not for driving one session.
---

# Orca Session Triage

## When

- The user asks which Orca sessions are done, stuck, or waiting on them, wants to
  answer the waiting ones together, or wants finished worktrees closed.
- Operating one session or worktree belongs to `orca-cli`.

## Steps

1. **Inventory.** Run `scripts/scan_sessions.py > inventory.json`. It joins
   `orca worktree ps`, `orca terminal list`, Claude Code transcripts, and Codex
   rollouts, and gives each non-main worktree one class. Orca's agent state misses
   questions: a Claude turn that ended on a prose question reads `done`, and a
   Codex `request_user_input` hides behind shift+← in the TUI. Classify from the
   transcript fields, then read each `waiting` and `idle` row's `final_text` or
   `last_assistant` yourself; the classes are a first sort, not the verdict.
   Done when every row with an agent terminal is `waiting`, `unstarted`,
   `working`, `finished`, or `idle` with a one-line reason you checked.
2. **Verify finished claims.** An agent's "uncommitted", "not pushed", or "done"
   is a claim that goes stale. Run `scripts/rm_check.py --worktree <path>
   [--pr <n>]` for each close candidate and trust its verdict over the transcript.
3. **Batch the questions.** Quote each `waiting` session's question from its
   transcript (Codex request titles and options come from the rollout). A Codex
   request marked `answered_in_chat_later` was usually answered by a later user
   message: confirm before listing it. Put a recommended option first. For more
   than a handful of questions, build one answer page: `references/answer-page.md`.
   Offer `finished` and `unstarted` worktrees on the same page as close/keep rows.
4. **Re-read, then deliver.** Questions go stale within minutes: an agent answers
   itself, a PR merges, the user replies directly. Immediately before sending,
   re-read each target session's latest message and composer, and drop answers
   that no longer apply. Send each answer as one line:
   `orca terminal send --terminal <handle> --text "<answer>" --enter --wait-submit 15 --json`.
   Done when each send's `result.send.prompt.stages` contains `turn_started`,
   or the screen shows the input queued behind a busy Codex ("submitted after next
   tool call"), which you report instead of resending.
5. **Close what the user approved.** Follow `references/cleanup.md`. Done when the
   worktree is absent from `orca worktree ps` and from disk.

## Boundary

- Removing worktrees, remote branches, containers, and volumes needs the user's
  per-item approval; close/keep choices on the answer page count as approval for
  the worktree and the containers named in that row.
- Relay answers verbatim, including follow-up questions the user wrote in memos;
  the session answers those, not you.
- An `unstarted` agent gets a task only after the user picks one. Reuse the
  repository's established request form, found in that repository's earlier
  transcripts (for example a `$ship <issue URL>` prompt).
- When you stop to wait for the user, ask with AskUserQuestion and set the
  worktree comment to the decision marker, so this sweep finds you next time.

## Done

- Every agent session is classified with a checked reason, the user has seen
  every live question, each delivered answer reached `turn_started` or a reported
  queue, and every approved worktree is gone from Orca and disk.
