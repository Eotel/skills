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
  gives each `stalled`, `unsure`, and `waiting` row its `git` and `pr` state.
  `open_pr_cleanup` marks a checkout with no current agent, no local changes,
  and HEAD at its open PR head for removal without asking; a historical
  transcript's question still gets its usual class and must be relayed.
  It reads the paired Orca environments listed by this Mac. A remote row
  carries `environment` and uses Orca's last agent message when its transcript
  is unavailable locally. When Orca keeps no message of the worktree's agents
  there, the scan reads its tabs' screens: Claude Code's reply bullet (⏺), turn-end line
  (`✻ … for …`), or mode line (⏵⏵) names an agent the terminal list leaves
  out (it does on some reads), and the last ⏺ reply, or the indented lines
  above the turn-end line when the ⏺ scrolled off, is the row's
  `orca_message` (`from: screen`).
  Remote `git.dirty`, `git.unpushed`, and
  `pr.head_matches` are unknown until checked on that host. A failed remote
  connection is reported on stderr while the other hosts still scan.
  `--summary` prints class counts, review rows, finished worktree/tab counts,
  and open PR cleanup rows. `--rows` prints one line per row (name, the
  verdict's class over the scan's, GitHub repo and PR, tab count, newest final
  message time, and its question, `waits_on`, `next_step`, or message start).
  `--row NAME` prints one row as JSON with fixed keys: no null strings, the
  newest final message of Claude, Codex, or Orca in `final`, and `github` as
  owner/repo from origin for `gh -R`. `--from SCAN_JSON` reads a saved scan
  instead of scanning again. Read rows through these rather than parsing the
  scan's JSON by hand. Jev
  (TypeSafe; key from `TYPESAFE_API_KEY` or `~/.config/typesafe/api_key`)
  reads each idle worktree's newest final message and returns `jev`: `asks`,
  the probability that it waits on the user (0.7 or more is `waiting`), and
  `status` (`in_progress`, `blocked_on_others`, or `done`) with its
  `confidence`. Only `done` can finish from Jev's verdict; the other two keep the row `stalled`
  with a reason, unless the row waited on someone else and its own PR merged
  after that message (`PR merged after the final message`). When `asks` is 0.3 to 0.7, `confidence` is below 0.6, or Jev
  gave no usable answer, the row is `unsure`. Jev's read is a fast first pass:
  the review queue below gets every row it judged a careful read. Orca also reports an agent as working while its background
  shell, monitor, or helper runs after its turn. Jev reads that row too: a
  question makes it `waiting` or `unsure`; otherwise it stays `working` with
  that reason, and never finishes while the job runs. A decision marker
  outlives its answer, so a `waiting` row's question is checked against its
  latest message before you ask it; when that message settled it, rewrite the
  comment without the marker so other tabs stop listing it. Each row lists its
  `done_cards`, one per agent Orca shows
  as done: the `handle` that closes its tab (null while the tab sleeps or
  no live tab can be assigned to it uniquely),
  whether the user `interrupted` it, the `prompt` it last got, Orca's copy
  of its last message, the last lines of its tab's screen (`screen_tail`), and
  whether its composer holds unsent text (`draft`, the same check
  `cleanup_hourly_tabs.py` makes; null when the screen was not read).
  `--cards` prints each done card, `closable` tab, and `meaningful_tabs` entry
  on its own line, led by the worktree's name: read cards through it.
  The board keeps a card in Done until its tab closes.
  `unread` says the user has not opened the worktree since it last spoke. A
  repository's main checkout is a row (`main`) while it hosts an agent. A
  Claude or Codex turn an API error cut off, and a lead whose
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
  `verdict` until its final message, PR, latest review or comment from a person other than
  the PR author, tree, or newest `done` or `ask` in the triage inbox changes;
  act on it the same way in every run. Each queued row carries those inbox
  reports and which report came last, since the Orca comment has no time. A Stop
  hook keeps the turn open while a queued row has no verdict.
  Codex ignores the fork, the model, and the hook: there, follow
  `orca-row-classify` in this session with that directory.
- `scripts/rm_check.py`: preflight before closing a worktree. Its verdict beats
  an agent's own "done" or "uncommitted".
- `references/delivery.md`: batching questions, sending answers, and giving
  lanes work.
- `references/next-step.md`: what each `stalled` worktree needs next.
- `references/answer-page.md`: one page for many answers.
- `references/cleanup.md`: closing worktrees, tabs, and done cards.
- `references/hourly.md`: a run started as `/orca-session-triage hourly`,
  setting up the Orca automation that starts it, or ending a turn of the
  session the automation runs in.
- `references/retro.md`: the end-of-run retrospective in a forked Agent, the
  external service for finished sessions, its adoption ledger, and recovery
  when an hourly transcript has not been processed.

## Done

Every row in this run's review queue has a verdict, every agent worktree has
a class you checked (the verdict, where it has one) and none is left `unsure`, the
user saw every live question and every `stalled` row's proposed next step, each
sent answer or step reached `turn_started` or is reported as queued, every done
card is closed, asked about, or reported with the reason it stays, each
approved or left-behind worktree is gone from `orca worktree ps` and from disk,
and each approved tab is gone from `orca terminal list`.
When another tab has explicitly been appointed lead, completion is the scan,
permitted cleanup, and any handoff in `references/hourly.md`.

## Boundary

- Scanning, reading transcripts, and `rm_check.py` are read-only: run them
  without asking. The scan sends each idle worktree's newest final message
  (its last 4,000 characters) to TypeSafe's API; without a key it sends
  nothing.
- Removing worktrees, branches, containers, or volumes, closing tabs, merging
  or requesting review, and giving an unstarted agent a task need the user's
  choice per item. These exceptions act first and report after:
  - A finished worktree with no meaningful tab that `rm_check.py` clears is removed.
  - 「agent がいなくて、手元の内容が PR と同じ（未 push・未変更が無い）作業場は、PR が Open でも聞かずに消す。」
  - A done card that leaves the user nothing to do has its tab closed.
  - A previous scheduled-run tab that `references/hourly.md` verifies as
    finished, idle, and free of unanswered questions is closed at the next run.
  - A row stalled by an API error or by unread worker mail gets one line that
    wakes it to continue its own work.
  - A task the user approved as the follow-up of another lane's result ("when
    A settles it, give B the task") goes to B once A reports it.
  - A lane confirmation in the user's standing approvals is answered once it
    has waited for the lead, as `references/hourly.md` describes.
- Relay answers verbatim; the session answers the user's follow-up questions.
- In an hourly run, the automation's session directs the lanes unless another
  tab was explicitly appointed lead; `references/hourly.md` bounds the other
  tab's work and explains the external inbox service.

## Observed triage facts

Read [references/observed-triage-facts.md](references/observed-triage-facts.md) when troubleshooting a tool used in hourly triage.
