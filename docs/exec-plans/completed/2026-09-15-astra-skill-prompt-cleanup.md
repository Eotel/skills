# GPT-6 Astra Skill And Prompt Cleanup

## Goal

OpenAI の「Rethinking skills and prompts for GPT-6 Astra」に沿って、この
リポジトリの skill、Codex 向け prompt、グローバル `AGENTS.md` を整理する。
完了時には、自動選択に使われる description が短く識別的で、root skill は
必要な分岐だけを案内し、モデル固有の古い手順や重複した常時指示が除かれている。

## Constraints and Non-Goals

- 破壊操作、外部書き込み、本番操作、他人の変更を守る安全境界は弱めない。
- skill の利用目的や、実障害を防いでいる非自明な制約は保持する。
- GPT-6 Astra 用のガイドを各 skill に複製しない。
- 実測根拠のないモデル別マイクロ最適化は増やさない。
- skill 名、配布単位、利用者向けの主要機能は、明確な理由なしに変更しない。
- リポジトリ外の `/Users/eotel/.codex/AGENTS.md` も対象とするが、git 管理対象とは
  分けて変更・報告する。
- subagent は使わない。独立した経験的評価は、本文整理後に別途明示依頼があった
  場合だけ行う。

## Plan of Work

1. Selection layer
   - 16件の `SKILL.md` description を1〜2文へ圧縮する。
   - `codex-prompting`、`codex-orchestrator-brief`、
     `codex-tdd-orchestration` の発火境界を成果物ベースで分離する。
   - `django` と `business-logic-extraction` の重複を、Django 固有処理か
     汎用ドメイン抽出かで分ける。
   - 存在しない skill 参照や過剰な trigger phrase 列挙を除く。

2. Skill entrypoints
   - `apm-usage`、`markdown-lint-setup`、`codex-orchestration-core`、
     `codex-tdd-orchestration` を中心に、常に必要な判断だけ root に残す。
   - 条件付きの設定例、詳細手順、長い template は既存または新しい
     `references/`、`scripts/`、`assets/` へ移す。
   - root と reference の重複をなくし、各 reference の読取条件を明記する。

3. Model guidance
   - 36件のモデル別ガイドを棚卸しし、skill 固有の意思決定を変えないものを削除する。
   - 現行モデルの選択や prompt 差分は `codex-prompting` に集約する。
   - GPT-5.x 固有の断言を GPT-6 Astra の公式記事・現行 OpenAI Docs と照合し、
     現行性を確認できない主張は削除または一次情報への参照に置き換える。
   - Claude 向け差分も、実際に共通 skill の動作を変えるものだけ残す。

4. Prompt templates
   - `codex-instruction.md` を Outcome / Done / Boundaries / Context pointers
     中心の小さな task prompt にする。
   - `codex-system-prompt.md` から実行設定や Codex 既定動作の再説明を外す。
   - `agents-stanza.md` を、常時必要な不変条件と条件付き document pointer の
     template に変える。
   - orchestration prompt は、役割分離・所有権・検証証拠などの安全上必要な契約を
     保ちつつ、手順の重複と harness 固有の古い語彙を減らす。

5. Global AGENTS.md
   - 安全・権限・事実確認の不変条件を残す。
   - full suite、事前調査、class sweep、実ブラウザ確認を変更リスクに応じた
     decision rule として書き直す。
   - Asana、Oracle、UI 表現、exec-plan など条件付きの規則を短い pointer または
    該当 skill 側へ移す。
   - 安全なローカル操作は完了条件まで自律的に続けることを明記する。

6. Catalog and consistency
   - `README.md` と生成済み skill catalog/workflow documentation を現行構造に合わせる。
   - description と本文、本文と reference、skill 間の誘導に矛盾がないか確認する。

7. Verification and completion
   - 全 skill を OpenAI の `quick_validate.py` で検証する。
   - Markdown の参照先、存在しない skill 名、古いモデル名、重複した routing を
     `rg` で検査する。
   - description の総文字数と上位件数を再計測し、削減結果を記録する。
   - リポジトリ固有の lint/test が存在すれば実行する。存在しなければ未実施理由を
     明記する。
   - diff を全件レビューし、計画を `docs/exec-plans/completed/` へ移して結果を記録する。

## Progress

- [x] 公式記事と現行 `skill-creator` の方針を確認。
- [x] 16件の description、root 行数、36件のモデル別ガイドを棚卸し。
- [x] リポジトリとグローバル `AGENTS.md` の初期構造監査。
- [x] Selection layer を整理。
- [x] Skill entrypoints を整理。
- [x] Model guidance を整理。
- [x] Prompt templates を整理。
- [x] Global `AGENTS.md` を整理。
- [x] Catalog と参照整合性を更新。
- [x] 検証と最終レビューを完了。
- [x] この計画を `completed/` へ移動。

## Verification

```text
uv run --with pyyaml python /Users/eotel/.codex/skills/.system/skill-creator/scripts/quick_validate.py <skill-dir>
rg -n 'prompt-optimizer|GPT-5\\.x|model-gpt-5\\.[56]|Claude itself|TaskCreate|Monitor' .
rg -n '\[[^]]+\]\([^)]*\.md\)' --glob '*.md'
git diff --check
git status --short
```

全 skill の validator を個別に実行する。リンク検査は候補抽出後に存在確認まで行う。
prompt の品質は文字列一致ではなく、発火境界、完了条件、権限境界、progressive
disclosure が実際に明確かをレビューする。

## Decision Log

- 2026-09-15: 既存の安全境界を「古い prompt」と一括して弱めず、権限・破壊操作と
  workflow 上の好みを分離する。
- 2026-09-15: GPT-6 Astra 用ファイルを各 skill に横展開せず、モデル共通で成立する
  outcome と decision rule を skill 本文に置く。
- 2026-09-15: 経験的 prompt tuning は、独立 evaluator の明示依頼がないため今回の
  完了条件には含めない。

## Surprises and Discoveries

- description は16件で9,854文字、上位6件だけで5,199文字だった。
- モデル別ガイドは36件、合計1,250行だった。
- `codex-prompting` と `codex-orchestrator-brief` が、存在しない
  `prompt-optimizer` へ誘導していた。
- 直近コミットで GPT-5.6 / Opus 5 のモデル別ガイドが503行追加されており、今回の
  公式方針と逆向きの保守負荷になっていた。
- ローカル APM は 0.29.0 で、0.30.0 の更新通知が出ていた。今回の変更では CLI
  更新を行わず、0.29.0 の `--help` を実装根拠にした。
- OpenAI の validator は system Python では `PyYAML` 不足で起動しなかったため、
  repository を変更しない `uv run --with pyyaml` 環境で実行した。
- `devenv-init` に未実装言語の placeholder reference が3件残っていたため削除し、
  現在の Python 対応だけを案内する形にした。

## Outcomes and Retrospective

- 16件の description を9,854文字から2,273文字へ削減した（7,581文字、76.9%減）。
  近接 skill は成果物と実行形態で発火境界を分けた。
- 36件・1,250行のモデル別ガイドを削除し、現行モデル向けの共通判断を
  `codex-prompting/references/prompt-design.md` に集約した。古いモデル表、GPT-5向け
  prompt、重複日本語版、履歴メモ、廃止した起動 script も削除した。
- APM、Markdown lint、orchestration、brief、prompting、plan、ship の entrypoint を
  outcome、判断条件、完了条件中心に再構成した。安全・権限・ownership・cold review・
  外部操作の境界は維持した。
- `/Users/eotel/.codex/AGENTS.md` は、変更リスクに比例する検証と、安全なローカル作業を
  完了まで進める自律性が両立する形へ整理した。
- README と静的 catalog/workflow を更新し、catalog は全42件、Eotel 由来16件を表示する。
- 全16 skill が OpenAI `quick_validate.py` を通過。Markdown 相対リンク切れ0件、JSON
  parse 成功、4件の shell script が `bash -n` 成功、agentic-docs の template 生成と
  日付置換も成功した。`git diff --check` も成功した。
- 実ブラウザで catalog 到達後に `codex` 検索を操作し、4件へ絞り込まれることを確認。
  workflow は「依頼済みの範囲を実行」と更新日 2026-09-15 の表示を確認した。
- repository root に package manifest や共通 test runner はないため、単一の full suite
  は存在しない。変更対象に対応する validator、link、script syntax、template generation、
  JSON、実ブラウザの各チェックを実行した。
