# 台帳の整理

採用 triage の画面の「整理」ボタンから立ち上がった session が読む。届いている候補を、今の道具・skill・決まり・会話記録と照らし、重なる案をまとめ、古くなった案を取り下げる案にする。採用・却下・保留を決めるのは三浦さんで、ボードで押す。この session は `decide`・`record`・`collect` を使わない。

## 候補

`python3 ~/.agents/skills/adoption-triage/scripts/ledger.py list --json --candidates` が返す案を全部見る。確認待ち・保留と、採用済みで未実施の案のうち、前の整理でまとめた案・取り下げの案にまだ含まれていないものが返る。

## 1 件ずつ確かめること

次の 4 点を確かめ、それぞれに根拠を付ける。根拠は、ファイルと行（`path:line`）、commit または PR の URL、決定の原文と時刻のどれかとする。確かめられないことは「未確認」と書く。

1. すでに入っているか。案の `target` が指す skill・script・設定を読み、案の後に入った変更を `git log` で探す。
2. 道具で置き換わったか。文の案（メモ・手順）が、後から script の引数・既定値・検査・出力で守られるようになっていないか、`--help` と source で確かめる。
3. 新しい決定と食い違うか。`~/.agents/instructions/shared-rules.md`、[policy.md](policy.md)、ほかの案の decision に付いた `reason`（三浦さんのコメント）と照らす。
4. ほかの案と重なるか。同じ script、同じ失敗、同じ決まりを扱う案を束ねる。

読む場所は次のとおり。ホームや `~/ghq` 全体を find・grep しない。

- skill: 配られた写しは `~/.agents/skills/`、Eotel/skills の source は `~/ghq/github.com/Eotel/skills`
- 指揮の道具: `~/.cache/orca-briefs/bin`
- ボード: `~/ghq/github.com/Eotel/orca-board`
- 会話記録: 案の `source.session` と `evidence` が指すもの。Claude の記録は `~/.claude/projects` にある。

## 台帳に書くこと

`ledger.py propose` に JSON を標準入力で渡す。`source` には、この session の名前と作業場の名前を入れる。項目は [ledger.md](ledger.md) のとおり。

- まとめる: 重なる案を 1 つにした新しい案を出し、`merges` に元の案の `proposal_id` を並べる。`fact` には元の案の事実を、`evidence` には元の案の根拠と照合で得た根拠を入れる。
- 取り下げる: 古くなった案ごとに、または理由が同じ案をまとめて、取り下げの案を出し、`retires` に元の案を並べる。`proposal` は「〜を取り下げる」とし、`fact` と `evidence` に、すでに入っている・置き換わった・食い違うことの根拠を書く。
- 残す: 今も有効で、重なりもない案は書き換えない。新しい根拠があれば `support` で足す。

文の案は、[policy.md](policy.md)「案の形」に従い、道具の引数・既定値・検査・出力で守らせる案にまとめ直す。新しい案の `target` はその形に合わせる。Eotel/skills の script を直す案は `triage-retro:skill:<skill>:script`、直せない外の道具は `triage-retro:skill:<skill>:upstream`、skill の外（`~/.cache/orca-briefs/bin`・orca-board など）は repository とファイルを書いた文にする。`priority` は元の案のうち最も高いものにする。

三浦さんがまとめた案・取り下げの案を採用すると、台帳が元の案を同じ操作で閉じる。まとめた案には、元の案から残すべき事実・根拠・対象をすべて入れる。

台帳は、`merges`・`retires` の先が候補でないとき書き込みを断る。断られたら `list --json --candidates` で状態を読み直す。

## 終わり

候補を全部見たら、`~/.cache/orca-briefs/bin/triage-notify --kind done` で次を知らせる。この報告で、ボードの「整理中」の表示が消える。

1. 見た候補の件数と、変えずに残した件数
2. まとめた案の件数と、それに含めた元の案の件数
3. 取り下げの案の件数と、それに含めた元の案の件数
4. 新しく出した案の `proposal_id`
5. 三浦さんが http://127.0.0.1:7317/adoption で決めること
