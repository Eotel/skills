# The brief

The session reads the repository (its `AGENTS.md`, `CLAUDE.md`, committed
skills) and this text. Rules that live only on this machine, or that you give
local lanes by habit, do not exist for it unless written here.

## What it carries

- **The task and its contract**: the issue number, and where the acceptance
  criteria are (the issue body, or a named comment on it).
- **How to read GitHub**: `gh api repos/<owner>/<repo>/issues/<n>` and
  `.../comments`.
- **Rules the repository does not state but the user holds**, for example:
  tests check behavior, none that only pin wording or comments; no AI
  attribution lines in commits or the PR body when the repository forbids them
  (a cloud session adds them by default); the language of the PR; the assignee.
- **The boundary**: whether it may merge; that it requests no reviews and
  writes to no tracker it cannot reach; that findings outside the task are
  reported, not fixed; that an open decision is written in the PR as a point to
  confirm, not settled by its own recommendation.
- **How it ends**: the PR URL, the result per acceptance criterion, the tests
  it ran, and what it could not verify.

## Shape

```text
<repo> の issue #<n> を実装して PR を出してください。

## 読むもの
- issue の本文とコメント（gh api ...）。「Agent Brief」のコメントが受け入れ条件の正本。
- AGENTS.md と、issue が挙げる設計書。

## 進め方
- 直す前に今の動きを確かめる。足すテストは振る舞いを確かめるものだけ。
- PR を出し（assignee ...）、CI が green になるまで見る。

## しないこと
- merge、レビューの依頼、<tracker> への書き込み。
- 範囲を超える直し、決まっていないことの決定。

## 終わり方
PR の URL、受け入れ条件ごとの結果、流したテスト、確かめられなかったこと。
```

Add a section for what is particular to the task: a dependency on another PR,
a file another open PR touches, a tool it must build before it can verify.
