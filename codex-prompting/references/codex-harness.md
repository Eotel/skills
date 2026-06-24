# Codex harness quirks

> Snapshot: 2026-06. Source: OpenAI Codex prompting guide
> (<https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide>).
> These are priors baked into Codex by post-training for the Codex CLI / cloud
> harness. Write prompts that align with them; don't re-litigate them.

Codex is a senior-engineer agent, not a Q&A bot. Once given a direction it is
trained to **gather context, plan, implement, test, and refine without waiting for
more prompts**, and to persist until the task is handled end-to-end within the turn
unless genuinely blocked. Your prompt's job is to aim that autonomy, not to
re-supply it.

## AGENTS.md — the canonical place for standing instructions

- Codex auto-discovers `AGENTS.md` (and `AGENTS.override.md`) from `~/.codex` down
  through each directory to the working directory, **merging in order** with later
  (deeper) files overriding earlier ones.
- They are injected as user-role messages prefixed `# AGENTS.md instructions for
  <directory>`. The model is trained to **adhere closely** to them.
- **Implication for prompting:** durable, repo-wide rules (conventions, forbidden
  patterns, required verification commands) belong in AGENTS.md, not re-pasted into
  every task prompt. Put only task-specific intent in the task prompt and *point to*
  AGENTS.md for the standing rules. Use `templates/agents-stanza.md`.

## Planning tool

- Codex has an internal `update_plan` tool. It is told to **skip planning for
  straightforward tasks (~easiest 25%) and never make single-step plans.**
- Do **not** prompt it to "first output a plan, then…" for routine work — forcing
  an upfront plan/preamble can make it **stop abruptly**. Let it plan internally.
- For genuinely multi-step work it reconciles every stated intention/TODO before
  finishing (Done / Blocked / Cancelled). You can rely on that rather than
  scripting the checklist yourself.

## File editing — `apply_patch`, batched

- Primary edit mechanism is `apply_patch`. Codex is trained to **read enough context
  first and batch logical edits**, avoiding many tiny thrashing patches.
- Don't prompt it toward micro-edits or "change one line at a time." If anything,
  reinforce: read the whole region, make the coherent change once.

## Tool & search preferences

- Prefers `rg` / `rg --files` over `grep`/`find` (speed), and **dedicated tools
  over shell equivalents** (e.g. its `read_file` over `cat`).
- Parallelizes independent tool calls (reads, searches) when allowed. If you set up
  a Responses-API harness, `parallel_tool_calls: true` matters; in the CLI this is
  already the default behavior.
- Don't instruct it to `cat`/`grep` by hand — it's slower and off-pattern.

## Preambles & progress updates

- Newer Codex models emit short **preambles** (commentary) before tool calls:
  roughly a 1-sentence acknowledgement + 1–2-sentence plan, then updates every
  ~1–3 steps (and at least every ~6 steps / ~10 tool calls), 1–2 sentences each.
- Tone target: **a real person pairing — low-ceremony.** It is trained to *avoid*
  log-voice, headings/status labels in commentary, and repetitive tics
  ("Got it–", "Good catch", "Aha").
- **Prompting implication:** if you want silent/terse automation (e.g. piping
  `codex exec` output), say so. If you want collaborative narration, the default
  already does it — don't double-prompt for "explain each step" or you get loggy
  output. Do not re-introduce "print an upfront plan and status updates," which the
  guide specifically removed because it causes early stops.

## Code quality priors

- "Act as a discerning engineer: optimize for correctness, clarity, and reliability
  over speed; avoid risky shortcuts, speculative changes, and messy hacks."
- **Conform to codebase conventions** (existing patterns, helpers, naming,
  formatting, localization); if it must diverge it states why.
- **No broad catches or silent defaults** — it won't wrap things in broad try/catch
  to make errors disappear. Don't ask it to "just make it pass."
- **Comprehensiveness:** wires changes across all relevant surfaces so behavior
  stays consistent. If a change has fan-out, it's trained to chase it.

## Frontend / design prior

- For UI work it's told to **avoid "AI slop"** — safe, average, default-looking
  layouts — and to aim for intentional, bold, slightly surprising interfaces with
  expressive fonts (avoid default stacks: Inter, Roboto, Arial, system).
- Prompting implication: you usually don't need to beg for "modern/clean UI"; you
  *do* need to give it the brand/voice constraints, or it will be bold in a
  direction you didn't want.

## Sandbox & approval modes (CLI)

- Codex runs under a sandbox + approval policy (read-only / workspace-write /
  full-access; on-request / on-failure / never approvals). The model is aware it
  may be sandboxed.
- **Prompting implication:** if the task needs network, out-of-workspace writes, or
  destructive commands, say so and assume the human/orchestrator handles approvals.
  Don't write prompts that silently assume full access. For unattended runs
  (`codex exec`), the approval mode is set at launch, not by the prompt — call out
  any operation that will need elevated access so the launcher can grant it.

## Final-message formatting

- Final answers use **plain natural language with optional short Title-Case
  headers**, backticks for commands/paths, and **standalone file references**
  (e.g. `path/to/file.ts:42`). It avoids nested bullet hierarchies and ANSI codes.
- Don't demand heavy markdown structure in the final message; it's tuned against it.

## Reasoning effort & long tasks

- Reasoning effort is a real lever: `low` / `medium` / `high` / `xhigh`. **Medium**
  is the recommended all-around interactive setting; escalate only for the hardest
  tasks. See `references/models.md` for per-model nuance.
- For multi-hour work, context compaction exists (the harness can compact and carry
  encrypted context forward). You rarely set this in a prompt, but for very long
  tasks, prefer telling Codex to checkpoint progress (commits, notes) over assuming
  infinite context.

## Metaprompting (tuning loop)

- If Codex produces good-but-slow results, ask it directly: *"Is there a way to
  clarify your instructions so you can reach a response this good faster next
  time?"* and have it propose targeted instruction edits (e.g. reduce
  time-to-first-tool-call, or fix preamble quality). Fold the result back into
  AGENTS.md or the system prompt. This is the supported way to converge a Codex
  prompt.
