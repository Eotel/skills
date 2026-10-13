# 人に向けた日本語の推敲スキルと検出ツールの調査

**最も近い既存スキルは [yomiyasu](https://github.com/nanaism/yomiyasu/tree/c2ffae670994fec96daef92e0bc219f5c1923113)。文章全体の推敲には、その利用が選択肢になる。**
「正本」「静かに壊れる」「効く」を扱い、必要な専門用法を残し、意味を勝手に狭めない点まで今回の目的と重なる。
6語に絞って人に渡す直前の言葉を確認する用途には、現在の軽いスキルとスクリプトを維持し、既存の判断方法を参考にする案を推奨する。
調べた主要資料に、6語すべてを用途別に扱う規則は見当たらなかった。自作だけが持つ問題意識だとはいえない。

確認日: **2026-10-09**。公開ソースを読み、外部スキルのインストール、推敲の実行、投稿は行っていない。
以下では、作者が説明する効果、ソースに書かれた指示、実測結果を区別する。

## 比較した4件

| 候補・確認したコミット | 作者の説明と確認した資料 | 意味・保護・文体の指示 | 検出、修正範囲、再確認 | 今回の6語との関係・採否 |
| --- | --- | --- | --- | --- |
| [nanaism/yomiyasu](https://github.com/nanaism/yomiyasu/tree/c2ffae670994fec96daef92e0bc219f5c1923113) | [README](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/README.md) は不自然な比喩や主述関係を整えると説明。[SKILL](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/skills/yomiyasu/SKILL.md)、語彙カタログ、lint・diff、比較記録、集計スクリプト、関連テストを確認。 | 主張、比重、断定の強さ、文の働きを維持。専門用語・名称・比較用の引用を保護し、原文の文体を保つ。不明な仕組みを補わず、曖昧さの幅も維持する。 | lint は候補を示す。diff は語句・文末・構造の変化を示し、意味が同じかはモデルか人が確認する。自然な箇所は残す。lint 後の修正は最大2回、diff 後の修正は1回とし、警告を消すだけの反復を禁じる。 | 「正本」の専門用法を残す指示、「静かに壊れる」「効く」の意味を保った言い換えがある。確認した SKILL・カタログ・lint に「台帳」「版」「閉包」の専用ルールは見当たらない。一般推敲の既存候補として有力。全文の取り込みや、文長・書式の目安を6語の検査へ持ち込むことは必須でない。 |
| [geonwoo-jeong/japanese-humanizer](https://github.com/geonwoo-jeong/japanese-humanizer/tree/09bb6c69a2f671f6c3223fab093fd023c61d5c84) | [README](https://github.com/geonwoo-jeong/japanese-humanizer/blob/09bb6c69a2f671f6c3223fab093fd023c61d5c84/README.md) は翻訳調を局所的に直すと説明。[SKILL](https://github.com/geonwoo-jeong/japanese-humanizer/blob/09bb6c69a2f671f6c3223fab093fd023c61d5c84/plugins/japanese-humanizer/skills/japanese-humanizer/SKILL.md)、quick-rules、taxonomy、revision-playbook、evidence、診断・配布スクリプトを確認。 | 数値、専門語、引用、否定、条件、責任主体を維持。感情や体験を足さず、敬体・常体と読者との距離を保つ。専門語や受動態を一律に消さない。 | スクリプトの結果は補助的な候補。文脈判断と局所修正を優先し、構成変更は限定する。最後に事実・専門的意味を確認。反復回数や明確な終了基準までは記載していない。 | 汎用の「専門語を残す」判断は近い。確認したパターン資料には6語それぞれの用途別ルールを確認できず。参照文に「正本」「効く」が登場するが、これらを修正対象として判定する指示ではない。局所修正と意味の監査を参考にする。 |
| [BrambleXu/ja-ai-polish](https://github.com/BrambleXu/ja-ai-polish/tree/0473487ac056b8961e3e37d126f969f1f7f56af8) | [README.ja](https://github.com/BrambleXu/ja-ai-polish/blob/0473487ac056b8961e3e37d126f969f1f7f56af8/README.ja.md) は禁止語ではなく文脈上の手掛かりとして扱うと説明。[SKILL](https://github.com/BrambleXu/ja-ai-polish/blob/0473487ac056b8961e3e37d126f969f1f7f56af8/SKILL.md)、日本語手順、de-ai-patterns、意味保持・保護範囲、評価データ・スクリプト・テストを確認。 | 単語だけでなく、数値が指す対象、行為者、条件、約束、不確実性を維持。原文・明示要件・文体サンプルを一般ルールより優先する。 | write/edit/detect を分離。変更強度を選び、自然な文は残す。保護対象と意味を照合した後に再評価し、安全な残存修正だけを行う。未説明の高得点段落がないことなどを終了条件にする。 | 不自然な語法や場面不適合を扱う汎用ルールは近い。確認したパターン資料には6語の用途別対応例を確認できず。内部手順の「台帳」「版」は編集対象の説明ではない。意味保持の確認と誤修正の評価を参考にし、得点閾値はそのまま移さない。 |
| [blader/humanizer](https://github.com/blader/humanizer/tree/225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8) | [README](https://github.com/blader/humanizer/blob/225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8/README.md) は意味を変えず書き手に近づけると説明。[SKILL](https://github.com/blader/humanizer/blob/225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8/SKILL.md)、パッケージ検証スクリプト、評価報告を確認。 | 事実・引用等の追加を禁じ、ファイル編集時はコード・パス・メタデータ等を保持。文体サンプルを一般規則より優先する。ただし文体によって反応や感想の追加を認める。 | 検出、初稿、事実の比較、再確認、完成稿という流れ。弱い特徴は複数重なってから修正する。構成変更は広く認める。明確な終了基準はない。 | 英語中心の汎用パターン集で、確認した SKILL に日本語6語の用途別ルールはない。弱い手掛かりだけで断定しない点は参考になる。英語特有の句読点規則や感想の追加は今回の用途に移さない。 |

「正本」「台帳」「版」「静かに壊れる」「閉包」「効く」の文字列が資料にあることと、その語を文脈に応じて直す指示があることは別である。
内部の手順名に使われているだけなら、今回の目的への対応とは数えない。
yomiyasu の [語彙カタログ](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/skills/yomiyasu/references/slop-catalog.md) は3語について実際の意味と残す用法を扱う。
特に、曖昧な失敗表現を、根拠なしに通知の有無やデータ整合性の問題へ限定しない方針が参考になる。
[lint 実装](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/skills/yomiyasu/scripts/yomiyasu_lint.py) も、この3語に関係する検出パターンを持つ。

## textlint から参考にできる点

| ツール | 確認できた仕組み | 今回の設計への判断 |
| --- | --- | --- |
| @textlint-ja/preset-ai-writing | [README](https://github.com/textlint-ja/textlint-rule-preset-ai-writing/blob/45bb6485062b96a8578c5fa6ae37a41de01d9b80/README.md) は一律禁止より構造の改善を目的とし、許可パターンやカテゴリ別の無効化を提供する。[ai-tech-writing-guideline](https://github.com/textlint-ja/textlint-rule-preset-ai-writing/blob/45bb6485062b96a8578c5fa6ae37a41de01d9b80/src/rules/ai-tech-writing-guideline.ts) は段落を読み、コードを除外して位置付きの改善提案を報告し、自動修正は持たない。 | 候補提示と文脈に応じた編集を分ける参考になる。許可パターンに一つでも一致すると段落全体を除外するため、広い許可設定は見逃しにつながるとコードから判断できる。[テスト](https://github.com/textlint-ja/textlint-rule-preset-ai-writing/blob/45bb6485062b96a8578c5fa6ae37a41de01d9b80/test/rules/ai-tech-writing-guideline.test.ts) も確認した。 |
| textlint-rule-prh | [README](https://github.com/textlint-rule/textlint-rule-prh/blob/cd2ce0677d2ef57f76e990b56e0b00f3f647e1a4/README.md) は辞書の pattern と expected による修正、from/to による辞書検証を提供する。[実装](https://github.com/textlint-rule/textlint-rule-prh/blob/cd2ce0677d2ef57f76e990b56e0b00f3f647e1a4/src/textlint-rule-prh.js) は Str ノードと除外する親ノードを確認する。リンク・引用ブロックは既定で検査しない。[辞書テスト](https://github.com/textlint-rule/textlint-rule-prh/blob/cd2ce0677d2ef57f76e990b56e0b00f3f647e1a4/test/prh-rule-tester-test.js) を確認した。 | 表記揺れなど、正しい置換先を一つに決められる場合には適する。今回の6語を単一の expected に対応させると、正しい専門用法まで変えたり、曖昧な意味を勝手に特定したりするため採用しない。 |
| textlint のルール基盤 | [ルール作成](https://textlint.org/docs/rule/)と[自動修正ルール](https://textlint.org/docs/rule-fixable/)は AST の対象ノード、報告と修正の分離、最小単位の修正を説明する。 | Markdown の対象範囲を広げる場合には AST 基盤が選択肢になる。現在の標準ライブラリだけのスクリプトを直ちに移行する根拠にはならない。 |

ai-writing の README は `severity: "info"` を案内するが、確認した ai-tech-writing-guideline の Options には severity がない。
本調査ではその設定の実行結果を確認していないため、終了コードや非ブロック化の動作は断定しない。

国立国語研究所の [「外来語」言い換え提案・利用の手引き](https://www2.ninjal.ac.jp/gairaigo/Teian1_4/tebiki.html)（2006年更新）は、同じ語でも文脈に応じて意味が変わり、一つの置換先では対応できないと説明する。言い換えで専門概念が曖昧になる場合は、元の用語に説明を添える方法を勧めている。今回の6語を誤りとする根拠には使わず、読者と文脈に応じて言葉を選ぶ設計の参考とする。

## 今回への推奨

以下は資料の共通点から導いた設計案であり、効果を実測した結論ではない。

1. **スクリプトは候補抽出、スキルは文脈判断を担当する。** 検出を誤りの確定や禁止語判定にしない。適切な用法なら検出を残して完了してよい。現在の [SKILL](../../natural-japanese/SKILL.md) はこの点に対応している。
2. **用途に応じて既存スキルと6語の確認を選ぶ。** 文章全体の構造や文体まで推敲するなら yomiyasu を試す価値がある。6語に絞った軽い確認には現在のスキルを保ち、修正例・保持例・意味を特定できない例を組にする。汎用スキルを複数重ねる構成や、大きなパターン集の全文取り込みは必須でない。
3. **意味の保持を具体的に確認する。** 事実と意図に加え、否定、可能性、義務、条件、行為者、対象、約束、不確実性を修正前後で比べる。自然さを理由に原因・効果・数値・感情を補わない。「静かに壊れる」を通知なしの失敗へ限定するのは、その仕組みが原文から分かる場合だけにする。情報不足時に元の意味の幅を保つ例は、現在のスキルにも補う余地がある。[yomiyasu のカタログ](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/skills/yomiyasu/references/slop-catalog.md)、[ja-ai-polish の意味保持規則](https://github.com/BrambleXu/ja-ai-polish/blob/0473487ac056b8961e3e37d126f969f1f7f56af8/references/fidelity-contract.md)と[保護範囲](https://github.com/BrambleXu/ja-ai-polish/blob/0473487ac056b8961e3e37d126f969f1f7f56af8/references/protected-spans.md)が参考になる。
4. **必要な箇所だけ直し、最後に文章として読み直す。** 単語の置換で解決しなければ周囲の一文を直す。原文の敬体・常体、読者との距離、専門性を保つ。意味が保たれ、自然な代案がなくなれば終了し、検出ゼロを目的に再修正し続けない。[japanese-humanizer の手順](https://github.com/geonwoo-jeong/japanese-humanizer/blob/09bb6c69a2f671f6c3223fab093fd023c61d5c84/plugins/japanese-humanizer/skills/japanese-humanizer/references/revision-playbook.md)が近い。
5. **推敲の評価をスキャナーのテストと分ける。** 6語の組になった例を実際のモデルに編集させ、誤修正、意味の変化、情報不足時の判断、読みやすさを確認する。原文・完成稿・スキルのコミット・実行条件を保存し、母語話者による比較も行う。複数の自然な回答を許容し、文字列一致だけで自然さを評価しない。

## 評価の限界

**現行スキルで6語を直したときの自然さと意味保持を実証する評価は確認できなかった。**
各スキルの指示が詳しいことは、モデルが毎回そのとおりに編集する証拠ではない。

| 候補 | 評価について確認できたこと | 結論の限界 |
| --- | --- | --- |
| yomiyasu | [比較記録](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/evals/comparison_benchmark.md) と、160ファイルの [集計結果](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/tests/corpus/benchmark_results.json) が公開されている。[集計スクリプト](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/scripts/benchmark_corpus.py) は検出件数とスコアを計算し、[関連テスト](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/tests/test_benchmark_categories.py) は集計カテゴリを検証する。 | 比較記録は過去の結果で、当時の提供文脈が残っておらず、現行の意味保持ルールの合格例ではないと明記する。件数の減少やリンター得点は自然さ・意味同等性の測定ではない。[diff 実装](https://github.com/nanaism/yomiyasu/blob/c2ffae670994fec96daef92e0bc219f5c1923113/skills/yomiyasu/scripts/yomiyasu_diff.py) も意味同等性を判定せず、変化の確認候補を示す。 |
| ja-ai-polish | [評価データ](https://github.com/BrambleXu/ja-ai-polish/blob/0473487ac056b8961e3e37d126f969f1f7f56af8/evals/evals.json) は105件。誤修正防止や意味保持のケースを含み、派生 detect 評価は58件。[テスト](https://github.com/BrambleXu/ja-ai-polish/blob/0473487ac056b8961e3e37d126f969f1f7f56af8/tests/test_scripts.py) は評価準備とスクリプトの動作を検証する。README は一部の例を実モデルで検証したと説明するが、[検証状況](https://github.com/BrambleXu/ja-ai-polish/blob/0473487ac056b8961e3e37d126f969f1f7f56af8/docs/validation.md) はリリース版の実モデル評価・母語話者による盲検評価を未実施としている。 | 一部の例を検証したという作者の説明と、再確認できる評価結果を分ける。検出の適合率・再現率などは目標であり達成値ではない。[check_fidelity.py](https://github.com/BrambleXu/ja-ai-polish/blob/0473487ac056b8961e3e37d126f969f1f7f56af8/scripts/check_fidelity.py) は主に保護文字列の減少を検知し、条件や責任関係の維持まで自動で証明しない。 |
| blader/humanizer | README は修正稿が16回中16回選ばれたと説明。[一次報告の Issue #229](https://github.com/blader/humanizer/issues/229) は、評価者がすべて同一系列の言語モデルで、品質比較は予定144試行のうち77試行を実施したと明記する。 | 人による盲検評価ではなく、今回の日本語6語の効果にも直接当てはめられない。[検証スクリプト](https://github.com/blader/humanizer/blob/225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8/scripts/validate-package.py) が調べるのはパッケージの整合性である。 |
| japanese-humanizer | [evidence](https://github.com/geonwoo-jeong/japanese-humanizer/blob/09bb6c69a2f671f6c3223fab093fd023c61d5c84/plugins/japanese-humanizer/skills/japanese-humanizer/references/evidence.md) は研究・公的指針・実装参考を整理する。[配布検証](https://github.com/geonwoo-jeong/japanese-humanizer/blob/09bb6c69a2f671f6c3223fab093fd023c61d5c84/scripts/smoke-distribution.mjs) はインストール等の確認。 | 研究の引用や例文は、そのスキルによる推敲効果の測定結果ではない。確認した資料には、比較できる推敲成績の公開記録が見当たらない。引用される全研究の内容まで本調査で再検証したわけではない。 |
| 現在の natural-japanese | [17本のテスト](../../tests/test_natural_japanese_scripts.py) は検出位置、活用形、コード除外、終了コード、入力エラー等を検証する。 | スキャナーの動作確認であり、スキルによる推敲の自然さや意味の保持を実測する評価ではない。 |

今回は調査メモのみを追加した。スキルとスクリプトの改善案は未実装で、外部ツールの検証結果も再実行していない。
