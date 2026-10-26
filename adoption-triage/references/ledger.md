# 採用台帳

各 Mac の `~/.local/share/adoption-triage/events.jsonl` に追記する。`scripts/ledger.py collect` は SSH で別の Mac の記録を読み、`imports/` に Mac 別の写しを原子的に保存する。元の記録は変更しない。接続先は `~/.config/adoption-triage/hosts.json` の `hosts` 配列（`name`, `ssh_target`, `remote_path`）で指定する。`name` は写しのファイル名に使う固定の識別名で、OS のホスト名と一致させる必要はない。Mac の改名後も同じ `name` を使う。`remote_path` はその Mac の記録の絶対パスとする。

画面を動かす Mac の設定だけに、最上位の `"decision_writer": true` と固定の `"name": "board"` を置き、他の Mac を `hosts` に列挙する。提案元の Mac の設定は `{"trusted_writer":"board","hosts":[]}` とし、逆向きの SSH 接続は使わない。画面側の `collect` は他の Mac の記録を取り込んだ後、統合した記録を SSH で各 Mac の `receive` に送って採用判断を配る。採用後に画面側で `collect` を実行すれば、提案元は判断を読み、`record` で実施結果を追記できる。画面側がもう一度 `collect` すると結果が戻る。`receive` は設定済みの判断元だけを受け入れ、既存行の削除と書き換えを拒否する。Mac 間の新着行で並び順が変わっても、内容が同じ行は保持する。その他の Mac に判断の行があれば、収集を拒否する。

`proposal` 行は `id`, `ts`, `host`, `type`, `proposal_id`, `source`（`session`, `worktree`）, `fact`, `proposal`, `reason`, `recommendation`（`adopt` / `reject`）, `priority`（`high` / `normal` / `low`）, `evidence`（配列）, 任意の `target` を持つ。同じ案の根拠を足す `evidence` 行は共通項目、`source`、`evidence`（配列）を持つ。`decision` 行は共通項目、`proposal_id`, `decision`（`adopt` / `reject` / `hold` / `revoke`）, `actor`, 任意の `reason`, `result`, `undo` を持つ。実施担当は `execution` 行（共通項目、参照した判断の `decision_id`、`outcome` は `applied` / `reverted` / `failed`, `actor`, `result`, 任意の `undo`）に変更の結果と取り消し方を追記する。別の Mac から古い判断に基づく実施結果が遅れて届いても、その判断の履歴として残す。`id` は各行の UUID、`proposal_id` は案件の UUID。時刻はオフセット付き ISO 8601。採用日・取り消し日は該当する decision の `ts`、実施日・復旧日は execution の `ts`。

提案は `python3 scripts/ledger.py propose` に JSON を標準入力で渡す。同じ案の別の会話記録から得た根拠は `support` に渡す。採否は `decide`、実施の結果は `record` に JSON を渡す。これらの行には直前に確認した案件のイベントを `after_id` として記録し、Mac 間の時計がずれても採否と実施を順に読む。`list --json` は現状を返す。`collect` は読めた Mac の写しだけを更新し、配布または収集に失敗した Mac と終了コードを返す。記録がまだない Mac は空の写しにする。`--events` と `--imports` で一時領域を指定できる。詳しい引数は `--help` で確かめる。

同じ `id` の行は各写しで重複してよいが、内容が違えば読み取りを拒否する。収集済みの履歴は行の順序も含めて変更を拒否し、並行した収集は Mac ごとにロックして更新する。採否の操作は履歴を上書きしない。`revoke` は取り消しの判断を記録する。実際の変更の取り消しは変更元の担当処理が行い、`record` で結果を残す。
