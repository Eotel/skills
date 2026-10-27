# Observed triage facts

<!-- triage-retro-managed -->

- Bash の shell は zsh なので、set -- $spec は語に分かれない（$1 に「パス 番号」がまとめて入り、rm_check の出力が JSONDecodeError になる）。引数は直接書くか ${=spec} を使う <!-- triage-retro:de0ded24-abd4-5f67-992c-b08ebef4f2a5 -->
- scan.json と review-queue.json の Claude の行の最終発言は claude.final_text と claude.final_at にある（last_assistant は無く None になる）。Codex の行は codex[].last_assistant と last_assistant_at <!-- triage-retro:671627a3-8cae-55c9-8acc-4bd61791b08d -->
- `hourly_state.py show` の left・questions・handoffs・requests は、ID（left は作業場のパス）を鍵にした dict。list として回すと AttributeError: 'str' object has no attribute 'get' になる。`.items()` で読む <!-- triage-retro:0b05b5f1-c2ef-511e-a308-05838cb673b2 -->
- 定時の回の実行と飛ばしは orca automations runs --id <automation id> --limit N --json で見る（automation 単数形は無い）。skipped_precheck の理由は precheckResult.stdout、時刻は scheduledFor・startedAt（ミリ秒） <!-- triage-retro:d8947835-bda7-5033-90ba-adf192bf440a -->
- Orca の Claude 会話記録は ~/Library/Application Support/orca/claude-profiles/<id>/home/projects/ の下で、パスに空白がある。for f in $(ls …) では割れるので python の glob で列挙する <!-- triage-retro:f0a6b7ae-81fe-566a-b29e-249d0150b8be -->
- spawn-lane は指示書に書いた PR・issue 番号を、触らない・照合だけの記載でも重複と数えて duplicate_reference で止まる。why に出た作業場が別件と確かめてから --force で起動する <!-- triage-retro:f169fab9-5d13-52d5-82f8-58662dcacc90 -->
- launchd・env -i から claude を呼ぶとき、CLAUDE_CONFIG_DIR があっても USER が無いと claude auth status は loggedIn false。USER も渡すと authMethod claude.ai・subscriptionType max になる <!-- triage-retro:0345e24a-51b3-5302-934e-34fd02c7b4d2 -->
- retro の worker が「claude -p exited 1: 」だけで失敗したら、同じ引数を env -i で再現して stdout の JSON の result を読む。OAuth 切れなら CLAUDE_CONFIG_DIR に claude-profiles/selected-host の中身を渡すと通る <!-- triage-retro:e14aa8bb-bb72-58fa-95e0-39b338f65925 -->
- 遠隔の rm_check.py は gh が 401。手元の gh で PR が MERGED であることを確かめ、その PR head SHA と compare <pr-head>...<HEAD> の ahead_by を取って --pr-head <sha> --head-ahead-of-pr <n> を渡すと、commit 照合の blocker が消える。MERGED でない PR には使わない <!-- triage-retro:00091797-2a10-5813-a4be-fad7307de82a -->
- scan_sessions.py の出力 JSON で、行の `codex` は rollout の配列（複数可）、`claude` は dict。自前で読むときは codex に .items() を使わない（AttributeError になる）。値は json.dumps で出す。 <!-- triage-retro:aa46f093-490c-55b4-913d-5b59a4803737 -->
- scan_sessions.py の JSON の各行は、作業場を `path` に持つ（`worktree` は無い）。保存された判定の種類は `verdict.class` にある。`verdict.verdict` は無いので常に None になる <!-- triage-retro:80eb736a-2c24-5822-807e-7e94edb19aeb -->
- local-deps/ に共有 repo の worktree を持つ作業場を orca worktree rm しても、local-deps/* が共有 repo の worktree の記録（prunable）として orca worktree ps に残る。各共有 repo（~/ghq/github.com/<owner>/<repo>）で git worktree prune を実行して消す <!-- triage-retro:8e88678e-6973-525a-80d6-0edbb4f92a95 -->
- gh --jq の式では ' で文字列を書けない（jq の文字列は " だけで、unexpected token "'" になる）。シェルの二重引用とぶつかるときは、gh pr view --json の出力を Python で整形する <!-- triage-retro:a37390b5-2f98-5b4f-9086-b16f7b2b9fc1 -->
- rm_check の processes が子の無い Orca のログインシェル（-/bin/zsh -l、画面は ❯ だけ）だけなら、orca terminal close --worktree path:<p> --all --json で閉じて rm_check をやり直すと processes が空になる <!-- triage-retro:c3f46518-a7ae-575c-8da4-71da4fcaed56 -->
- 作業場の local-deps/ が共有 repo の入れ子 worktree のときは、各写しにも rm_check.py をかけて blockers が無く review を片付けたことと、HEAD がリモートにあること（git branch -r --contains・ls-remote）を確かめてから git -C <共有 repo> worktree remove で外すと、親の rm_check が ok になる。--force は写しの rm_check が ok のときだけ使う <!-- triage-retro:eb34ba15-0878-57f8-a361-1ffb20911dc3 -->
- 指揮が答えを受け取ったかは scan の pending_questions で決めない。引き継ぎの直前に `~/.cache/orca-briefs/bin/claude-tx <指揮の transcript> <since-ISO> 400` を実行し、ANSWER 行とその後の動きを見る。 <!-- triage-retro:bae13185-54d4-51be-8294-a6a41410c2c4 -->
- 定時の引き継ぎを triage-notify で送る直前に `inbox_read.py read --brief` を実行し直し、走査を始めた後に届いた分を引き継ぎに入れる。ack の前にもう一度読む。 <!-- triage-retro:d5866bed-ff8d-5d56-b525-76489944f866 -->
