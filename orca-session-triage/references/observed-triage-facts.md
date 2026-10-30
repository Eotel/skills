# Observed triage facts

<!-- triage-retro-managed -->

- Bash の shell は zsh なので、set -- $spec は語に分かれない（$1 に「パス 番号」がまとめて入り、rm_check の出力が JSONDecodeError になる）。引数は直接書くか ${=spec} を使う <!-- triage-retro:de0ded24-abd4-5f67-992c-b08ebef4f2a5 -->
- spawn-lane は指示書に書いた PR・issue 番号を、触らない・照合だけの記載でも重複と数えて duplicate_reference で止まる。why に出た作業場が別件と確かめてから --force で起動する <!-- triage-retro:f169fab9-5d13-52d5-82f8-58662dcacc90 -->
- launchd・env -i から claude を呼ぶとき、CLAUDE_CONFIG_DIR があっても USER が無いと claude auth status は loggedIn false。USER も渡すと authMethod claude.ai・subscriptionType max になる <!-- triage-retro:0345e24a-51b3-5302-934e-34fd02c7b4d2 -->
- retro の worker が「claude -p exited 1: 」だけで失敗したら、同じ引数を env -i で再現して stdout の JSON の result を読む。OAuth 切れなら CLAUDE_CONFIG_DIR に claude-profiles/selected-host の中身を渡すと通る <!-- triage-retro:e14aa8bb-bb72-58fa-95e0-39b338f65925 -->
- gh --jq の式では ' で文字列を書けない（jq の文字列は " だけで、unexpected token "'" になる）。シェルの二重引用とぶつかるときは、gh pr view --json の出力を Python で整形する <!-- triage-retro:a37390b5-2f98-5b4f-9086-b16f7b2b9fc1 -->
- Backlog のコメントは bee issue view --comments --json の JSON に入らない。bee api issues/<KEY>/comments -f order=desc -f count=N で取る（パスに ?order=… を付けると出力が空になり JSONDecodeError） <!-- triage-retro:6e3a812c-515f-59ba-9d2a-43386696dffe -->
