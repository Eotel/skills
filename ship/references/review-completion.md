# Review completion

Wait for CI, reviews, and the merge with these tools, never with
`gh pr checks --watch`, `gh run watch`, or a loop of gh calls. A hook blocks
those, and every agent on the three Macs shares GitHub's 5,000 requests an hour.

- Claude Code: run `review-response`'s `scripts/watch_prs.py` under the Monitor
  tool, with the PR as one `owner/repo#N` line in its list file.
- Other agents: run `~/.local/bin/pr-wait <owner/repo> <N> --until
  checks|review|merged` in the background and read its JSON when it exits. On a
  Mac without pr-wait, run `watch_prs.py --once` at least 60 seconds apart.

1. Read the PR's reviews, top-level comments, line comments, unresolved threads,
   and configured automated review status for the current head.
2. Address valid findings. Respond with evidence when a finding does not apply.
3. Wait for configured automated reviews on the current head to finish. When
   `watch_prs.py` prints `CODEX NONE` for the head
   of a PR in a repository where Codex reviews PRs, post a PR comment
   `@codex review` whose last line is this session's name, and keep waiting.
   Do not ask Codex in a repository it does not review, such as an outside
   author's project.
4. Re-read every review and comment feed, unresolved threads, and CI. Resolve
   newly arrived findings, then repeat the completion check.

The PR is ready to merge when required CI is green, configured automated
reviews have finished on the current head, and no required feedback remains
unresolved. Merge only with explicit authorization, through
`~/.local/share/eotel-harness/current/bin/merge-pr <owner/repo> <N>`, which runs
`merge-check` first. On a Mac without merge-pr, use `gh pr merge`.
