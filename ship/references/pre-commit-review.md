# Pre-commit review inputs

Pass these inputs to `code-review` and then to `simplify`. They replace each
skill's default diff (`code-review`'s HEAD-only range, `simplify`'s
`@{upstream}...HEAD` range, neither of which sees untracked files) and
`code-review`'s spec discovery, because shipping reviews happen before committing.

- **Fixed point:** the change's starting commit or the merge-base of `HEAD` and
  the intended PR base branch. Resolve it to a commit before invoking the review.
- **Shipping diff:** pass `git diff <fixed-point> -- <shipping-paths>` for tracked
  files. This compares the fixed point with the working tree, covering committed,
  staged, and unstaged edits. Also supply the full contents of intended untracked
  files. Scope the review to the change being shipped, preserving unrelated work.
  Check this complete input for changes rather than the skill's default
  `git diff <fixed-point>...HEAD`, which can be empty before committing.
- **Spec** (`code-review` only): supply the originating issue's contents, the
  user's request, or the approved exec plan directly. Tracker configuration is needed only when an issue
  must still be fetched; a supplied spec requires no tracker setup.

Keep `code-review`'s independent Standards and Spec reviews and `simplify`'s
parallel cleanup angles.
