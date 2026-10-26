# 採用台帳

各 Mac の `~/.local/share/adoption-triage/events.jsonl` に追記する。画面を動かす Mac では `scripts/ledger.py collect` が SSH で他の Mac の記録を読み、`imports/` に host 別の写しを原子的に保存する。元の記録は変更しない。接続先は `~/.config/adoption-triage/hosts.json` の `hosts` 配列（`name`, `ssh_target`, `remote_path`）で指定する。`remote_path` はその Mac の記録の絶対パスとする。

`proposal` 行は `id`, `ts`, `host`, `type`, `proposal_id`, `source`（`session`, `worktree`）, `fact`, `proposal`, `reason`, `recommendation`（`adopt` / `reject`）, `priority`（`high` / `normal` / `low`）, `evidence`（配列）, 任意の `target` を持つ。`decision` 行は共通項目、`proposal_id`, `decision`（`adopt` / `reject` / `hold` / `revoke`）, `actor`, 任意の `reason`, `result`, `undo` を持つ。実施担当は `execution` 行（共通項目、`outcome` は `applied` / `reverted` / `failed`, `actor`, `result`, 任意の `undo`）に変更の結果と取り消し方を追記する。`id` は各行の UUID、`proposal_id` は案件の UUID。時刻はオフセット付き ISO 8601。採用日・取り消し日は該当する decision の `ts`、実施日・復旧日は execution の `ts`。

提案は `python3 scripts/ledger.py propose` に JSON を標準入力で渡す。採否は `decide`、実施の結果は `record` に JSON を渡す。`list --json` は現状を返す。`collect` は読めた host の写しだけを更新し、失敗した host と終了コードを返す。記録がまだない host は空の写しにする。`--events` と `--imports` で一時領域を指定できる。詳しい引数は `--help` で確かめる。

同じ `id` の行は各写しで重複してよいが、内容が違えば読み取りを拒否する。採否の操作は履歴を上書きしない。`revoke` は取り消しの判断を記録する。実際の変更の取り消しは変更元の担当処理が行い、`record` で結果を残す。
