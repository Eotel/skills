# Closing worktrees

A `finished` worktree whose `meaningful_tabs` is empty is done and left behind:
remove it without asking when `rm_check.py` reports `ok` with no `containers`,
and report it afterwards.

「agent がいなくて、手元の内容が PR と同じ（未 push・未変更が無い）作業場は、PR が Open でも聞かずに消す。」
The scan marks these rows `open_pr_cleanup: true`, except a row whose Orca
comment contains 「消さない」 or 「残す」: its reasons quote the comment instead.
A HEAD commit or review-pr log (`~/.cache/orca-briefs/review/out/<owner>-<repo>-<PR>.log`) under
thirty minutes old also keeps it, with the reason 「別の担当が使用中（時刻）」: another lane works there
without an agent in its tabs.
HEAD matching the PR head proves it was pushed to that PR even when local
remote-tracking refs are stale. Remove these checkouts after `rm_check.py` clears
them, even when their class is `waiting` or a readable shell tab remains. Relay
any pending question or result before removal; the removal itself needs no
question. For a remote row, check the current agent, HEAD, PR head, and local
changes on its host before applying the same rule; the scan leaves those git
facts unknown until that check. `~/.local/share/eotel-harness/current/bin/rm-check-batch --host
<host>` runs `rm_check.py` on that Mac under a python3 of 3.10 or later. gh cannot read GitHub there, so for a spec
naming a PR without its head it reads that Mac's HEAD and origin and runs `merged_pr_head.py` with this Mac's gh:
a merged PR's head and `ahead_by` reach `rm_check.py` as `"<path> <PR> <pr-head> <ahead_by>"`, and an open PR's spec
stays as given, so the `--host` line a scan prints, which names the row's PR, needs no head added by hand. The
process check below protects work running
in a tab. To check worktrees of several repositories in one call, give
`rm-check-batch` full paths (`"<path> <PR|->"`) and no base directory:
`scan_sessions.py --from SCAN_JSON --rm-specs --row NAME …` prints those specs
for the rows you name, finished ones whose tabs remain included, instead of
building them by hand. Specs remain on stdout; after them, stderr gives the complete
`rm-check-batch --from` command with `--remove-ok --summary` for the selected rows.
For a read-only check, pipe the specs into `rm-check-batch --from SCAN_JSON --no-lookup --summary`.
This pipe requires a harness release containing Eotel/harness#28. Use that release on both Macs for
batch checks over SSH. It accepts paths with spaces and evaluates removable nested worktrees in
saved open PR previews (`--preview --detach-nested`).
The scan's `--cards` and `--summary` views show one check command per finished or open PR cleanup row.
With the prior harness release, the views use `rm_check.py --summary` for local checks and show
a release requirement for SSH paths with spaces. The local check gives current blockers and Compose status.
`rm-check-batch --archive-scratch` passes that option to each `rm_check.py`, and
`--remove-ok` runs steps 3 and 4 below for each worktree whose check is `ok`
with no `containers`, putting the result in that worktree's line as `removal`
(it exits 4 when one did not finish). Give it only rows these rules remove
without asking. Its `--summary` prints one line per worktree instead of the
JSON (`<name> | ok=… | removal=… | reasons=…`, reasons naming only `.yomiyasu/`
left out, the blockers before the held files, and held files of one kind, reason, and decision under one top
directory on one line, such as `vendor/ の下の 48 件`): read those lines rather than parsing the JSON. After the
worktrees, either form lists every held file in one block (`確認が要るファイル`, one line
each with the worktree, reason, and decision, grouped the same way with `--summary`): ask the user about them
together.
With `--from SCAN_JSON`, batch reads the same saved verdicts as `--rm-specs`.
It counts a scanned meaningful tab only while `orca terminal list` still lists
it. With `--remove-ok`, it closes the row's scanned tabs through `close_tabs.py`
before checking; without it, a tab blocker explains that `--remove-ok` closes
the tabs before removal. Tabs whose worktree moved after the scan stay open.
For a row whose `removal_decision` allows removal, every process must sit in an idle tab. macOS hides a tab's login
shell's environment, so `rm_check.py` names no `tab` for an empty one: as many such empty tabs (counted by
`tab_root`, so two shells of one tab count once) as live tabs it did not name stand for those tabs, which close before
removal. A live tab left without one keeps the row.
Record a removal answer's condition and cleanup as values, not only in its words. `lead_decisions.py record --decision
remove:… --after pr-merged:<PR URL>` approves the row once the scan reads that PR as merged, whatever conditions the
words give (`removal_decision` shows `after` and `after_state`). With `--discard PATH`, `--archive-path PATH`, or
`--restore-nested PATH` (repeatable, relative to the worktree) or `--compose-down`, the scan approves the row whatever the
words give. `rm-check-batch --from SCAN_JSON --remove-ok` closes the scanned tabs, runs the recorded cleanup,
verifies any requested archives, and checks again before removal. A named path is archived even if the removal
check already passes without it. Tracked files and paths inside a symlinked directory are kept. Run that cleanup
on the worktree's Mac.
Use `--approve` for an explicit approval without a PR condition. It can accompany cleanup options.
Name each nested repository separately with `--restore-nested`, for example `--restore-nested local-deps/library`.
It restores index and working-tree changes to files in that repository's HEAD. Use `--discard PATH` for individual
untracked files the answer authorizes. A remaining untracked file, stash, or unpublished commit can still block cleanup.
If an answer has no structured values,
the scan reads its text. For a short closure instruction such as `閉じる。` or `close it`, it prints a command to
record an explicit approval. A live summary asks for a saved scan before displaying a removal command.

When the latest answers under different removal keys share a timestamp, keep the worktree
unless every answer approves removal and specifies the same archive, discard, Compose, and nested restoration cleanup.

Every other worktree waits for the user's choice. A row with `main: true` is a
repository's own checkout: its tabs close and the checkout stays (`rm_check.py`
blocks its removal).

Run these per worktree, in order.

1. `scripts/rm_check.py --worktree <path> [--pr <n>] --from SCAN_JSON` (or
   `rm_check.py <path> <PR|->`). With `--from`, a file outside `.git`,
   `node_modules`, and `.venv` or a move of HEAD after the scan's `scanned_at`
   (or `--since`) blocks: another lane may be using the worktree. Remote refs
   not fetched within the hour are fetched first; a failed fetch is in `notes`.
   Pass `--pr` when the worktree
   has a PR: a MERGED PR whose head equals HEAD counts as merged (squash merges),
   an OPEN PR whose head equals HEAD clears the unmerged-commit check, and the
   PR's fetched base branch becomes the base. Without it the base is the
   remote branch the worktree started from, else the remote's default branch,
   else (no `origin/HEAD`, `origin/main`, or `origin/master`, as in a
   repository with no remote) the local `main` or `master`, which `notes`
   name, and when HEAD is not in that base, gh is asked for a MERGED PR of the
   checked-out branch whose head is HEAD: one found counts as `--pr` (`notes`
   names it). With no PR given or found, HEAD outside that base, and HEAD in
   the branch's upstream (a pushed `release/…` branch with no PR), that upstream
   is the base and `notes` say so; such a row gets no `--pr-head` advice.
   Pass `--base` when the output's `base` is not where the work merges.
   - `reasons` lists everything that keeps the worktree in one place: each
     blocker, with the uncommitted paths after their count, then each work
     record, then each held file with its reason and the decision it needs.
     Read it when `ok` is false: `blockers` alone can be empty while a path to
     review keeps the worktree.
   - `blockers` (uncommitted changes, HEAD outside the base) stop the removal;
     `uncommitted` lists the paths of the changes. Report them and leave the
     worktree. Untracked Python bytecode (`__pycache__/` of `.pyc` files) is
     not a change: it is listed in `regenerable`. A commit whose patch is already in the
     base under another hash (a PR rebased before it merged) does not block; it
     is listed in `merged_by_patch`, so name it in the report. A merge commit
     outside the base still blocks, and a patch that the base later reverted
     still counts as in the base. One exception: when every commit past a
     merged PR's head is a merge and HEAD's tree equals the base's (the base
     merged in after the PR merged), nothing blocks and `notes` say so; an own
     commit or any tree difference still blocks as ahead of the PR head.
     An Orca comment containing 「消さない」 or 「残す」 also blocks, naming the
     key `remove:<path>`: hand the question to the lead, and once
     `lead_decisions.py` holds an answer for that key, the blocker clears and
     `notes` quote the answer. When the comment or the answers cannot be read,
     `notes` say so and nothing blocks on that alone.
   - `review` lists ignored paths that are neither caches nor copies in the
     main checkout: a directory is copied when each of its files is there with
     the same content, whatever else the main checkout holds there. Each one is
     also in `records` or `held`. A path is never removed for its name or for
     being untracked alone.
     - `records` are work records, by `kind`: `notes` (`.yomiyasu/`,
       `.investigate/`), `reviews` (`.claude/reviews/`, `.impeccable/`), and
       `images` (a folder of only screenshots). In a worktree the user chose to
       remove, they move aside and the removal goes on without another
       question: run `rm_check.py` again with `--archive-scratch`.
     - `held` are settings (`setting`: `.env`, `*.local.*`), data (`data`:
       SQLite, CSV, dumps), Git clones (`clone`), and anything else (`unknown`).
       Each has a `reason`, the `decision` the user makes, and a `content_summary`
       with its disk usage and up to three entry names. General held clones include
       the branch, commits absent from remote-tracking refs, uncommitted changes,
       and stash. The check reports unknown when Git configuration prevents inspecting
       changes and marks the live remote state unverified. The existing `local-deps`
       check queries origin directly before marking a clone disposable. Collect them and ask once. A `.env`
       names the keys whose values its `.env.example` lacks, never the values:
       they may be the only copy of an API key.

     When work records alone keep the worktree (nothing blocks and `held` is
     empty), `--archive-scratch` copies each to
     `~/.cache/orca-briefs/archive/<worktree>-<name>-<YYYYMMDD>/`, compares
     every file, appends the original path and the result to `archived.jsonl`
     there, deletes the original, and checks again (listed in `archived`, each
     with `verified`). A copy that differs is deleted and the original stays.
     It moves nothing while anything else blocks or is held. Without the option
     nothing moves. When the lead's answer
     to `remove:<path>` names another path to archive first (`.tmp/`,
     `.tmp/pr-descriptions`), pass it as `--archive-path <path>` (repeatable,
     relative to the worktree): it moves to the same place under the same
     rule, and an ignored directory holding nothing beyond a named path inside
     it counts as that path. Before that, when only
     paths to review, those folders, and nested worktrees keep it, the option
     copies each record under `.claude/reviews/` that the main checkout also
     ignores and does not have yet to the same place in main, confirmed with cmp
     (`copied_to_main`). Other ignored files, such as `.env` or
     `.claude/settings.local.json`, are settings main would run with and stay
     held. With `--detach-nested` as well, nested copies and these
     folders go together.
     For a MERGED PR, the option also archives untracked
     `docs/exec-plans/**/*.md` files absent from the base, using the same
     verified copy and manifest. Any tracked change or another untracked
     file keeps those plans in place.
   - `regenerable` lists what a tool writes again from tracked files or
     installed packages with no one's input: untracked Python bytecode, and,
     among ignored paths, generated sources (`__generated__/`, GraphQL codegen's
     `graphql/generated/`, `src/paraglide/`), e2e output (`e2e/screenshots/`, `e2e/.state/`,
     `e2e/.auth/`), and the hook scripts husky and vite-hooks write on install
     (`.husky/_/`, `.vite-hooks/_/`), MkDocs' `site/` beside `mkdocs.yml`,
     Django's test databases (`test_*.sqlite3`) and `var/test-storage/`,
     taihei-cbm's test uploads (`apps/api/media/files/datafile/storage/`), an
     ignored directory of only `*.log` files (its size and last write in
     `notes`), an ignored `.claude/scheduled_tasks.lock` whose holder (its
     `pid`, started at its `procStart`) no longer runs, an ignored output
     whose generator `GENERATED_BY` in `rm_check.py` names (only while that
     generator is tracked in the worktree and names the output's file name,
     such as `.claude/.venv-missing` from `.claude/hooks/worktree_create.sh`),
     an ignored tool output beside the tracked file it is built from (Cargo's
     `target/` beside `Cargo.toml`, Tauri's `gen/schemas/` beside
     `tauri.conf.json`, Composer's `vendor/` (or each path in it) beside a tracked
     `composer.json` that sets no other `vendor-dir` and a tracked `composer.lock`,
     the one `outDir` a tracked `vite.config.*` names on its `outDir:` line (comments
     outside string literals left out; a value of string literals (none with `${`,
     `%`, `?` or `#`) and the calls
     that join them, such as `fileURLToPath(new URL(…))`, read as path parts from
     the config's directory; only a directory below it; in a config that sets
     `root`, only an outDir of `fileURLToPath(new URL(…, import.meta.url))` or
     `path.resolve`/`path.join(__dirname, …)`), a directory whose every zip is one a Terraform
     `archive_file` block beside it (comments left out) declares with `output_path =
     "${path.module}/<dir>/…"`, an interpolation such as `${each.key}`
     standing for one path segment, and a
     `.terraform/` whose `terraform.tfstate` names a backend other than
     `local`; `notes` give the tool and the ignore file, line, and pattern
     that exclude it),
     and an ignored directory holding only these and empty files (`web/var/`).
     Add a generator there after reading the tracked file that writes the
     output. They do
     not hold up the removal. Neither
     does a `.env` that differs from its `.env.example` only in ports, which the
     example and fresh ports rebuild, nor the `.apm-local/` that APM 0.35 writes
     at the worktree root: its random installation identity binds only that
     checkout, and the next install writes a new one. Ignored caches and test
     reports (`.cache/`, `.coverage`, `playwright-report*/`, `blob-report/`, and the like) are
     skipped without a listing. An untracked path is never judged by its name,
     since a `build/` the repository does not ignore may be new source, and a
     `.gitignore` of `*` alone is no sign either: yomiyasu writes one over its
     records.
     Untracked or ignored standalone clones in `local-deps/` also count as disposable when each is
     clean, has no stash or ignored work to preserve, owns no linked worktree,
     and HEAD and every local branch/tag are on actual origin branches or tags.
     Clones with initialized submodules or stored `.git/modules` repositories
     stay for inspection: their child repositories may hold unpublished work.
     The remote refs are read once per clone. `notes` name each verified clone; a failed
     check names the clone in `reasons`. The check leaves the copies on disk.
   - `containers` lists docker compose projects defined inside the worktree.
     When docker does not answer in 20 seconds, a worktree with a compose file
     (`compose*.y*ml`, `docker-compose*.y*ml`) is blocked until it does; one
     without gets a note and its verdict.
   - `processes` lists commands or current directories using the path. Each
     says whether it runs in an Orca tab (`in_orca_tab`), the PID Orca's
     terminal host started for that tab (`tab_root`), the tab's `handle`
     when the system shows it (`tab`), and whether that tab runs nothing but
     its login shell or the shell's setup waits (`idle_tab`). An idle tab does
     not block: `orca worktree rm` stops its PTY. A busy tab blocks with the
     `close_tabs.py --from SCAN_JSON <handle>` that closes it: run it with the
     saved scan when that scan lists the handle in `closable`, then run
     `rm_check.py` again. A
     process outside Orca tabs is read again after two seconds: one that ended
     by then is left out, and one still running blocks with its PID, command,
     and current directory; a `node` server or another job is left running and
     reported. The live holder of an ignored `.claude/scheduled_tasks.lock` is
     listed here with `lock`, not as a path to review. The commands the check's
     output is piped into are not listed, including readers of a batch
     wrapper's output. Other processes in the same process group still count.
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
     `rm_check.py` on the remote Mac (`rm-check-batch --host` does this for each
     spec naming a PR).
2. Stop those projects with `docker compose -p <project> down` from the worktree.
   Keep named volumes unless the user approved deleting them, and say which
   volumes remain.
3. `orca worktree rm --worktree path:<path> --json`. Orca also deletes the
   checked-out local branch when it can prove it merged; a detached HEAD leaves
   the branch, so name it in the report.
4. `scripts/rm_check.py --worktree <path> --confirm-gone` confirms the path
   is gone from `orca worktree ps --json` and from disk, reading again for up
   to a minute: the removal answers `removing: true` and finishes in the
   background. Exit 1 (`gone: false`) is a stuck removal to report. Do not
   search the JSON for the name: a row's `preview` holds its tabs' output,
   which may still name a removed worktree (2026-10-29).
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
   Orca again, leaving out the calling session's own transcript, pane, and
   reports; a tab whose worktree moved after the scan stays, printed as
   `KEPT -> 走査の後に動いた（time what）`, a transcript named with its newest line's kind
   (`assistant の行`, `人の入力`, `ツールの結果`, …). A transcript whose new lines are
   all Claude Code compacting the idle session on its own (the boundary, the summary,
   its attachments and links, and the "Compacted while idle" notice) does not count,
   nor, when the worktree has one agent besides the caller, do its state changes from
   the summary's start to 5 seconds after its last line (a transcript names no pane,
   so beside another agent every change counts); a line after it counts as before. Scan again and read that row
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
its tab and the command that reads its screen (`orca terminal read --terminal
HANDLE`), the first 60 characters of Orca's draft (`-` when none, `unknown`
when the screen was not read), whether the screen's input line is empty
(`composer empty` / `has text`), Orca's copy of its last message, and the end
of its screen. Orca's draft can be Claude's suggestion rather than the user's
unsent text (on 2026-10-29 one came with `composer empty`): read its text. Read
the card's final message (the
row's `final` in `--row`: the transcript's, or the card's `last_message` when
Orca recorded it later or no transcript was read; its `screen_tail` when both
are empty), then:

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
