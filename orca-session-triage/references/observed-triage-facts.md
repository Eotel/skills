# Observed triage facts

<!-- triage-retro-managed -->

- Bash の shell は zsh なので、set -- $spec は語に分かれない（$1 に「パス 番号」がまとめて入り、rm_check の出力が JSONDecodeError になる）。引数は直接書くか ${=spec} を使う <!-- triage-retro:de0ded24-abd4-5f67-992c-b08ebef4f2a5 -->
- scan.json と review-queue.json の Claude の行の最終発言は claude.final_text と claude.final_at にある（last_assistant は無く None になる）。Codex の行は codex[].last_assistant と last_assistant_at <!-- triage-retro:671627a3-8cae-55c9-8acc-4bd61791b08d -->
- `hourly_state.py show` の left・questions・handoffs・requests は、ID（left は作業場のパス）を鍵にした dict。list として回すと AttributeError: 'str' object has no attribute 'get' になる。`.items()` で読む <!-- triage-retro:0b05b5f1-c2ef-511e-a308-05838cb673b2 -->
