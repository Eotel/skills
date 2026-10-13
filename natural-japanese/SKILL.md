---
name: natural-japanese
description: Review and revise human-facing Japanese to replace awkward literal translations and vague jargon with wording that fits the reader and context. Use when drafting Japanese replies, UI copy, documentation, or shared messages, or checking existing prose for these expressions.
---

# Natural Japanese

人に向けた日本語を、読み手と用途に合う言葉で書く。対象は返信、画面の文言、
公開する説明、他の人が読む報告やコメント。内部の思考や作業メモには適用しない。

## 判断

言葉の珍しさではなく、その文で意味が伝わるか、もっと自然で正確な言い方が
あるかを判断する。専門用語、正式名称、読者に定着した呼び方は必要なら残す。
単語を別の単語に置き換えるだけで伝わりにくさが残る場合は、文を書き直す。

具体的な動作や結果を示す。ただし原文にない効果、原因、確実性を補わない。
意味を特定できない場合は断定を避け、修正に必要な情報だけを確認する。
引用、コード、識別子、パス、正式な項目名は正確に保持し、必要なら説明を添える。

## 資料と検査

- 日本語を作成・修正するときは
  [references/word-choice.json](references/word-choice.json) を読み、見直す場面と
  残す場面を参照する。登録語は例であり、一覧以外の直訳や曖昧な比喩も見直す。
- 既存の文章から候補を拾う、または投稿前の原稿を機械的に確認するときは
  [references/scanning.md](references/scanning.md) を読む。
  `scripts/check_japanese.py` は候補を示す。修正の判断は文脈を読んで行う。

## 完了と範囲

検出箇所を含め、読み手が意味を理解できる表現になり、原文の事実と意図が
保たれていれば完了。適切な用語を残した結果、検出が残ってもよい。
検出がゼロでも、文章全体の自然さを確認したことにはならない。

通常は修正後の文章を返す。説明を求められたときや、判断に必要な不明点が
あるときだけ理由を添える。原稿の修正は依頼の範囲で行い、投稿や送信は
その操作が依頼されている場合に行う。
