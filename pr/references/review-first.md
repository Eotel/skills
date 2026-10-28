# Review-first PR body

For repositories owned by the signed-in account or one of its organizations, when the repository
has no pull request template of its own. Start from [the template](../assets/review-first.md);
keep its five headings and their order. Other headings (対象外, 残件) may sit between them. A PR
that changes a screen adds 画面の before/after after the second; see [screenshots.md](screenshots.md).

The reader is a reviewer who has not seen the issue or the session. They should learn from the
first section what went wrong, and from the third where to spend their time.

## Sections

1. **何が問題だったか**: two or three sentences on what a user did and what happened to them or
   to the data, before any function name, column, or arrow diagram. Link the issue or ticket.
   For new work, say what users could not do.
2. **この PR で変わること**: one changed behavior per item. Name changes to migrations, APIs,
   screens, and values sent to other systems. A diagram from
   [show-me.md](show-me.md) may support an item; it does not replace the sentence.
3. **reviewer に見てほしいこと**: a numbered list. Each item names a file and line range at the
   head commit, and the decision the reviewer has to make or the risk to check. Known gaps the
   reviewer should judge (an edge left as before, a raw SQL writer that skips a new column) go
   here, not only under the next heading.
4. **確かめたこと・確かめていないこと**: results only, with who ran them and on which commit:
   test files and counts, CI, real-environment checks. List what was not verified, and why, as a
   separate list. Do not state an unverified behavior as a fact.
5. **戻すときの注意**: what a revert does not undo (migrations, rewritten data, values already
   sent to other systems) and what to check before reverting. Write 「revert だけで戻せる」 when
   nothing remains.

## Words

Say what a user or a record experiences, then the identifier in backticks. Do not lead with the
session's own shorthand for its data and steps (a column name, a nickname for a record, an arrow
chain of calls); a reviewer who has not read the session cannot decode it.

## When someone else wrote the body

When rewriting another session's body, keep its facts, numbers, and conditions, reread the latest
comments and reviews first, and say at the end who wrote the body and who rewrote it.
