# Review completion

1. Read the PR's reviews, top-level comments, line comments, unresolved threads,
   and configured automated review status for the current head.
2. Address valid findings. Respond with evidence when a finding does not apply.
3. Wait for configured automated reviews on the current head to finish.
4. Re-read every review and comment feed, unresolved threads, and CI. Resolve
   newly arrived findings, then repeat the completion check.

The PR is ready to merge when required CI is green, configured automated
reviews have finished on the current head, and no required feedback remains
unresolved. Merge only with explicit authorization.
