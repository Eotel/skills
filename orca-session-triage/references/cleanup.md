# Closing worktrees

A `finished` worktree whose `meaningful_tabs` is empty is done and left behind:
remove it without asking when `rm_check.py` reports `ok` with no `containers`,
and report it afterwards.

「agent がいなくて、手元の内容が PR と同じ（未 push・未変更が無い）作業場は、PR が Open でも聞かずに消す。」
The scan marks these rows `open_pr_cleanup: true`, except a row whose Orca
comment contains 「消さない」 or 「残す」: its reasons quote the comment instead.
HEAD matching the PR head proves it was pushed to that PR; `git.unpushed` may
still be positive when the branch tracks the PR's base. Remove these checkouts after `rm_check.py` clears
them, even when their class is `waiting` or a readable shell tab remains. Relay
any pending question or result before removal; the removal itself needs no
question. For a remote row, check the current agent, HEAD, PR head, and local
changes on its host before applying the same rule; the scan leaves those git
facts unknown until that check. `~/.cache/orca-briefs/bin/rm-check-batch --host
<host>` runs `rm_check.py` on that Mac under a python3 of 3.10 or later. The process check below protects work running
in a tab. To check worktrees of several repositories in one call, give
`rm-check-batch` full paths (`"<path> <PR|->"`) and no base directory.

Every other worktree waits for the user's choice. A row with `main: true` is a
repository's own checkout: its tabs close and the checkout stays (`rm_check.py`
blocks its removal).

Run these per worktree, in order.

1. `scripts/rm_check.py --worktree <path> [--pr <n>]`. Pass `--pr` when the worktree
   has a PR: a MERGED PR whose head equals HEAD counts as merged (squash merges),
   an OPEN PR whose head equals HEAD clears the unmerged-commit check, and the
   PR's fetched base branch becomes the base. Without it the base is the
   remote branch the worktree started from, else the remote's default branch,
   and when HEAD is not in that base, gh is asked for a MERGED PR of the
   checked-out branch whose head is HEAD: one found counts as `--pr` (`notes`
   names it). Pass `--base` when the output's `base` is not where the work merges.
   - `blockers` (uncommitted changes, HEAD outside the base) stop the removal;
     `uncommitted` lists the paths of the changes. Report them and leave the
     worktree. Untracked Python bytecode (`__pycache__/` of `.pyc` files) is
     not a change: it is listed in `regenerable`. A commit whose patch is already in the
     base under another hash (a PR rebased before it merged) does not block; it
     is listed in `merged_by_patch`, so name it in the report. A merge commit
     outside the base still blocks, and a patch that the base later reverted
     still counts as in the base.
     An Orca comment containing 「消さない」 or 「残す」 also blocks, naming the
     key `remove:<path>`: hand the question to the lead, and once
     `lead_decisions.py` holds an answer for that key, the blocker clears and
     `notes` quote the answer. When the comment or the answers cannot be read,
     `notes` say so and nothing blocks on that alone.
   - `review` lists ignored paths that are neither caches nor identical copies of
     the main checkout. Open each one: generated sources and test output
     (screenshots, uploaded fixtures) are regenerable; anything else (exports,
     reports, hand-written notes) goes back to the user before removal. A `.env`
     entry names the keys whose values its `.env.example` lacks, never the
     values: they may be the only copy of an API key. In a worktree the user
     chose to remove, a scratch record folder (`.yomiyasu/`, `.investigate/`)
     moves next to the lane's brief and report, and the removal goes on without
     another question: run `rm_check.py` again with `--archive-scratch`. When
     those folders alone keep the worktree, it moves them to
     `~/.cache/orca-briefs/archive/<worktree>-<name>-<YYYYMMDD>/` (listed in
     `archived`) and checks again; it moves nothing while anything else blocks
     or needs review. Without the option nothing moves.
   - `regenerable` lists what a tool writes again from tracked files or
     installed packages with no one's input: untracked Python bytecode, and,
     among ignored paths, generated sources (`__generated__/`, GraphQL codegen's
     `graphql/generated/`, `src/paraglide/`), e2e output (`e2e/screenshots/`, `e2e/.state/`,
     `e2e/.auth/`), and the hook scripts husky and vite-hooks write on install
     (`.husky/_/`, `.vite-hooks/_/`). They do not hold up the removal. Neither
     does a `.env` that differs from its `.env.example` only in ports, which the
     example and fresh ports rebuild, nor the `.apm-local/` that APM 0.35 writes
     at the worktree root: its random installation identity binds only that
     checkout, and the next install writes a new one. Ignored caches and test
     reports (`.cache/`, `.coverage`, `playwright-report*/`, and the like) are
     skipped without a listing. An untracked path is never judged by its name,
     since a `build/` the repository does not ignore may be new source, and a
     `.gitignore` of `*` alone is no sign either: yomiyasu writes one over its
     records.
   - `containers` lists docker compose projects defined inside the worktree.
   - `processes` lists commands or current directories using the path. Each
     says whether it runs in an Orca tab (`in_orca_tab`), the tab's `handle`
     when the system shows it (`tab`), and whether that tab runs nothing but
     its login shell or the shell's setup waits (`idle_tab`). An idle tab does
     not block: `orca worktree rm` stops its PTY. A busy tab blocks with the
     `close_tabs.py --from SCAN_JSON <handle>` that closes it: run it with the
     saved scan when that scan lists the handle in `closable`, then run
     `rm_check.py` again. A
     process outside Orca tabs blocks with its PID; a `node` server or another
     job is left running and reported.
   - `nested_worktrees` lists worktrees of other repositories inside the
     checkout (such as `local-deps/<repo>`), each with its own `blockers`
     (uncommitted changes, ignored paths to open, HEAD on no remote). A tracked
     `.mo` changed beside an unchanged `.po` is a recompiled catalog, not a
     change: it is listed in the item's `regenerable`. When they are the only
     thing left, run `rm_check.py` again with `--detach-nested`: it restores
     those catalogs (`git checkout HEAD --`) and removes every copy from its
     repository before `orca worktree rm`, so no prunable record is left. It removes nothing
     while the worktree has other blockers, paths to review, or a copy with
     blockers, since a kept worktree still needs its copies.
   - On a remote Mac whose gh cannot read the PR, `notes` give a
     `merged_pr_head.py` command for a Mac where gh works. It confirms the PR
     is MERGED and prints `--pr-head` and `--head-ahead-of-pr`; pass them to
     `rm_check.py` on the remote Mac.
2. Stop those projects with `docker compose -p <project> down` from the worktree.
   Keep named volumes unless the user approved deleting them, and say which
   volumes remain.
3. `orca worktree rm --worktree path:<path> --json`. Orca also deletes the
   checked-out local branch when it can prove it merged; a detached HEAD leaves
   the branch, so name it in the report.
4. Confirm the path is gone from `orca worktree ps --json` and from disk. The
   removal answers `removing: true` and finishes in the background, so poll for
   up to a minute before reporting it stuck.
   `orca terminal show` keeps answering for the removed terminals with
   `orphaned: true, connected: false`: that is a stale record, not a live PTY.
5. When the lane reported to a lead in another tab, send the lead one line
   naming the removed worktree and where its results remain, so the lead does
   not go looking for it. A send refused with `agent_prompt_blocked` (seen while
   the lead's tab shows a selector) typed nothing: send it on a later pass.

A merged remote branch the user approved deleting: check
`gh api repos/<owner>/<repo>/compare/<default>...<branch>` reports `ahead_by: 0`,
then `gh api -X DELETE repos/<owner>/<repo>/git/refs/heads/<branch>`.

## Closing tabs in a kept worktree

A worktree's `closable` list names the tabs it can lose while the checkout stays:
shells whose last line is a bare prompt (`❯`, `➜`, `✗`, `$`, `%`) with no
output for `--shell-idle-minutes` (a finished `Setup` tab, a spare shell; on
another Mac, where Orca lists most shells without output or its time, a
rendered screen ending at a bare prompt, whatever direnv printed above it),
and, once the worktree is `finished` or `stalled`, its agent sessions, unless the final
message says work continues: a background job may still run in that tab. A
helper agent in a `working` or `waiting` worktree stays: its lead may send it
the next round. An `unsure` worktree keeps its agents until you have read the
final message and classed it. Shells close on the user's choice; agent sessions
follow "Clearing done cards".

1. `scripts/close_tabs.py --from SCAN_JSON <handle> ...` with the approved
   handles and the scan that listed them (`--cards` with `--from` prints this
   command for each row's `closable` tabs). Just before closing, it reads the
   worktree's transcripts, its triage inbox reports, and its agents' state in
   Orca again; a tab whose worktree moved after the scan stays, printed as
   `KEPT -> 走査の後に動いた（time what）`. Scan again and read that row
   before deciding again. Never close a listed tab with a bare
   `orca terminal close`: it closed a lane that deployed after the scan
   (2026-10-28). Closing stops that pane's process and removes a single-pane
   tab.
2. Confirm the handles are gone from `orca terminal list --json`.

Closing drops Orca's resume record, not the transcript: `claude --resume
<session-id>` or `codex resume <session-id>` in the worktree reopens the session.

## Clearing done cards

A row's `done_cards` are the agents whose turn ended; the board keeps each one
in Done until its tab closes. `scan_sessions.py --cards` prints each card with
its tab, the first 60 characters of Orca's draft (`-` when none, `unknown`
when the screen was not read), whether the screen's input line is empty
(`composer empty` / `has text`), Orca's copy of its last message, and the end
of its screen. Orca's draft can be Claude's suggestion rather than the user's
unsent text (on 2026-10-29 one came with `composer empty`): read its text. Read
the card's final message (the
row's transcript; the card's `last_message` when none was read; its
`screen_tail` when both are empty), then:

- Something is left for the user: it is a question (`delivery.md`). An
  `interrupted` card is a request they cut short: ask whether its `prompt`
  still stands. The card closes once the answer is delivered and the lane ends
  its turn with nothing more for them.
- The last message is itself what they asked for (an explanation, a report)
  and the row is `unread`: report it as ready to read and keep the card.
- Nothing is left for the user (finished, or waiting only on a reviewer, a
  release, a deploy, or another lane): write what remains and who it waits on
  with `orca worktree set --worktree path:<path> --comment "<text>" --json`,
  close the tab with `scripts/close_tabs.py --from SCAN_JSON <handle>`, then
  report it; a card it keeps moved after the scan, so read it again. The
  worktree, its files, and the transcript stay; when the review
  or deploy arrives, a later sweep reopens the session there (`claude --resume
  <session-id>` or `codex resume <session-id>`) or carries the step itself.
- A lead whose helper still works, and a session holding the user's unsent
  text, stay: report each with that reason.
  A card whose `draft` is null was not read: read its screen before closing it.

A null `handle` means the tab is asleep or no live tab can be assigned to the
card uniquely. Check the worktree's agent terminals and transcript before
closing.
If ownership remains ambiguous, keep the card and report why. If no tab of
that agent type is live, `orca terminal close --worktree path:<path> --all --json`
retires every tab of that worktree, shells included: use it when the worktree's
live tabs are all in `closable` and `close_tabs.py` closed them first (it keeps
them when the worktree moved after the scan), and otherwise report the card as
asleep. A
card whose worktree is removed goes with it; what it left for the user is still
a question. Confirm each closed card is gone from its worktree's `agents` in
`orca worktree ps --json`.
