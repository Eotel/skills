---
name: orca-session-triage
description: Sweep Orca agent sessions, relay batched answers and next steps, clear the board's done cards, and close finished tabs and worktrees. Use when the user asks which Orca sessions are finished, stalled, or waiting on them, wants their pending questions answered in one pass, wants done cards, finished sessions, tabs, or worktrees closed, or wants the sweep scheduled.
hooks:
  Stop:
    - hooks:
        - type: command
          command: 'f="$HOME/.claude/skills/orca-session-triage/scripts/stop_check.py"; [ -f "$f" ] && python3 "$f" || true'
---

# Orca Session Triage

Find Orca worktrees whose agents finished, stalled, wait on a human, or never
got a task; relay the user's answers and next steps in one batch; clear the
board's done cards; close what the user approves.

## Where

- `scripts/scan_sessions.py`: classifies every worktree from Orca state plus
  Claude and Codex transcripts, lists the tabs it could close (`closable`), and
  gives each `stalled`, `unsure`, and `waiting` row its `git` and `pr` state. Jev
  (TypeSafe; key from `TYPESAFE_API_KEY` or `~/.config/typesafe/api_key`)
  reads each idle worktree's newest final message and returns `jev`: `asks`,
  the probability that it waits on the user (0.7 or more is `waiting`), and
  `status` (`in_progress`, `blocked_on_others`, or `done`) with its
  `confidence`. Only `done` can finish; the other two keep the row `stalled`
  with a reason, unless the row waited on someone else and its own PR merged
  after that message (`PR merged after the final message`). When `asks` is 0.3 to 0.7, `confidence` is below 0.6, or Jev
  gave no usable answer, the row is `unsure`. Jev's read is a fast first pass:
  the review queue below gets every row it judged a careful read. Orca also reports an agent as working while its background
  shell, monitor, or helper runs after its turn. Jev reads that row too: a
  question makes it `waiting` or `unsure`; otherwise it stays `working` with
  that reason, and never finishes while the job runs. A decision marker
  outlives its answer, so a `waiting` row's question is checked against its
  latest message before you ask it. Each row lists its `done_cards`, one per agent Orca shows
  as done: the `handle` that closes its tab (null while the tab sleeps),
  whether the user `interrupted` it, the `prompt` it last got, and Orca's copy
  of its last message. The board keeps a card in Done until its tab closes.
  `unread` says the user has not opened the worktree since it last spoke. A
  repository's main checkout is a row (`main`) while it hosts an agent. A
  Claude turn an API error cut off (`claude.api_error`), and a lead whose
  workers' reports, questions, or escalations sit unread since its last turn
  began (`unread_mail`, from Orca's orchestration mailbox), are `stalled` with
  that reason; the mail reason is also added to a `waiting` row.
- **Review queue**: run the scan with
  `--review-out ~/.cache/orca-session-triage/${CLAUDE_SESSION_ID}/review-queue.json`.
  It queues every row Jev judged from a final message (`unsure`, `stalled`,
  `waiting`) and every `stalled` row whose own PR merged, unless a careful read
  already classed that same final message. Then invoke the `orca-row-classify`
  skill with `~/.cache/orca-session-triage/${CLAUDE_SESSION_ID}`: it reads the
  queue in a fresh Sonnet context and writes `review-result.json` there. Act on
  its verdicts, not on Jev's class: `waiting` is a question (`question`,
  `needs_user`), `blocked` is reported with `waits_on`, `in_progress` wakes the
  lane with `next_step`, `done_unmerged` is a review-or-merge choice, and
  `finished` goes to `cleanup.md`. The next scan keeps each verdict in
  `~/.cache/orca-session-triage/verdicts.json` and shows it as the row's
  `verdict` until that row's final message changes; act on it the same way in
  every run. A Stop hook keeps the turn open while a queued row has no verdict.
  Codex ignores the fork, the model, and the hook: there, follow
  `orca-row-classify` in this session with that directory.
- `scripts/rm_check.py`: preflight before closing a worktree. Its verdict beats
  an agent's own "done" or "uncommitted".
- `references/delivery.md`: batching questions and sending answers.
- `references/next-step.md`: what each `stalled` worktree needs next.
- `references/answer-page.md`: one page for many answers.
- `references/cleanup.md`: closing worktrees, tabs, and done cards.
- `references/hourly.md`: a run started as `/orca-session-triage hourly`,
  setting up the Orca automation that starts it, or ending any turn of the
  session the automation runs in (a run it skipped while you worked is yours
  to sweep).

## Done

Every row in this run's review queue has a verdict, every agent worktree has
a class you checked (the verdict, where it has one) and none is left `unsure`, the
user saw every live question and every `stalled` row's proposed next step, each
sent answer or step reached `turn_started` or is reported as queued, every done
card is closed, asked about, or reported with the reason it stays, each
approved or left-behind worktree is gone from `orca worktree ps` and from disk,
and each approved tab is gone from `orca terminal list`.

## Boundary

- Scanning, reading transcripts, and `rm_check.py` are read-only: run them
  without asking. The scan sends each idle worktree's newest final message
  (its last 4,000 characters) to TypeSafe's API; without a key it sends
  nothing.
- Removing worktrees, branches, containers, or volumes, closing tabs, merging
  or requesting review, and giving an unstarted agent a task need the user's
  choice per item. Three exceptions act first and report after: a finished
  worktree with no meaningful tab that `rm_check.py` clears is removed, a
  done card that leaves the user nothing to do has its tab closed, and a row
  stalled by an API error or by unread worker mail gets one line that wakes it
  to continue its own work.
- Relay answers verbatim; the session answers the user's follow-up questions.
