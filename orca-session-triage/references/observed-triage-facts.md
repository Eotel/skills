# Observed triage facts

<!-- triage-retro-managed -->

- Bash の shell は zsh なので、set -- $spec は語に分かれない（$1 に「パス 番号」がまとめて入り、rm_check の出力が JSONDecodeError になる）。引数は直接書くか ${=spec} を使う <!-- triage-retro:de0ded24-abd4-5f67-992c-b08ebef4f2a5 -->
- spawn-lane は指示書に書いた PR・issue 番号を、触らない・照合だけの記載でも重複と数えて duplicate_reference で止まる。why に出た作業場が別件と確かめてから --force で起動する <!-- triage-retro:f169fab9-5d13-52d5-82f8-58662dcacc90 -->
- launchd・env -i から claude を呼ぶとき、CLAUDE_CONFIG_DIR があっても USER が無いと claude auth status は loggedIn false。USER も渡すと authMethod claude.ai・subscriptionType max になる <!-- triage-retro:0345e24a-51b3-5302-934e-34fd02c7b4d2 -->
- retro の worker が「claude -p exited 1: 」だけで失敗したら、同じ引数を env -i で再現して stdout の JSON の result を読む。OAuth 切れなら CLAUDE_CONFIG_DIR に claude-profiles/selected-host の中身を渡すと通る <!-- triage-retro:e14aa8bb-bb72-58fa-95e0-39b338f65925 -->
- gh --jq の式では ' で文字列を書けない（jq の文字列は " だけで、unexpected token "'" になる）。シェルの二重引用とぶつかるときは、gh pr view --json の出力を Python で整形する <!-- triage-retro:a37390b5-2f98-5b4f-9086-b16f7b2b9fc1 -->
- Backlog のコメントは bee issue view --comments --json の JSON に入らない。bee api issues/<KEY>/comments -f order=desc -f count=N で取る（パスに ?order=… を付けると出力が空になり JSONDecodeError） <!-- triage-retro:6e3a812c-515f-59ba-9d2a-43386696dffe -->
- 帳票・画面の見た目を選ぶ問いは、文だけで伺わない。担当に、全体の中の位置を枠で示し案を同じ文字列で描いたページを作らせ、open を確かめてから AskUserQuestion で伺う。 <!-- triage-retro:278f9552-ece6-581b-9afe-e47aaf716582 -->
- AskUserQuestion の答えで、選んだ選択肢（投稿・merge など外へ出す操作）とメモが食い違うときは、許可として担当へ渡す前に読みを 1 問で確かめる。先にメモ付きのまま渡したら、確かめた答えで上書きを送る <!-- triage-retro:a39ef175-0051-5f45-a8c5-12ae7f2a7eb5 -->
- Actions の支払い上限の確認: org の billing API は 410 と admin:org 要求で読めない。最新 run の job を gh api repos/O/R/check-runs/JOB_ID/annotations で読むと課金で未起動かが分かる。 <!-- triage-retro:94336769-5eed-53ba-9af2-07f628ebed4c -->
- 組織の secret が repo に渡っているかは、gh secret list --org（組織の管理者以外は 403）ではなく、gh api repos/<o>/<r>/actions/organization-secrets で読む。repo の secret は gh secret list -R で読む。 <!-- triage-retro:4974278a-ada9-57d1-8614-a51d99d22fa1 -->
