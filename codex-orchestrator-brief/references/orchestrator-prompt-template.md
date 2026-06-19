# codex-goal-prompt.md — Template and Section Guidance

Eight sections. The file opens with a usage note ("paste this into the main codex
session"; prerequisite: refactor-instructions.md exists at repo root), then the
prompt body. Write in the repo's documentation language.

## §0 Role declaration + first action

Open with the role: "あなたはオーケストレーターである。自分ではコードを編集しない。"
File edits allowed only for the memory log and the final report. Then the first
action — invoke `/goal` with a goal of this shape:

> refactor-instructions.md の Phase 0〜N を本プロンプトの Wave 構成に従って完遂する。
> 終了条件は「§7 Rubric の全項目が、実装に関与していない独立検証エージェントによって
> PASS と判定されていること」。Rubric が PASS するまで自分の判断で作業を打ち切らない。

The termination condition must reference the rubric + independent verifier, never
the orchestrator's own satisfaction.

## §1 Role split

Three roles, with the reasoning spelled out (the implementer model behaves better
when it understands why):

- **Orchestrator** (main session): wave planning, subsession lifecycle, message
  relay, PR operations, memory log. Never implements, never verifies.
- **Implementer subsession**: scoped to exactly one wave's spec phase.
- **Verifier subsession**: fresh context, has never seen the implementation
  conversation. Receives ONLY: the spec, its rubric lines, the branch/PR diff.
  Must run every check itself; implementer self-reports are not evidence.

State explicitly: implementer and verifier may never be the same subsession, and
the orchestrator must not forward the implementer's report to the verifier
(self-report bias re-enters through summaries).

Write the prompt assuming the harness's real delegation primitives — codex (and
comparable harnesses) can spawn separate sessions/subagents and exchange messages
with them. Instruct the main session to use them as its normal mode of operation:
spawn one implementer session per wave and **keep it alive across retries** (FAIL
findings go back to the same session as a message, preserving its working
context); spawn a **fresh** verifier session per verification round (its value is
precisely that it has no accumulated context); communicate with sessions only via
messages, never by doing their work inline. Do not include "if you can't spawn
sessions, do it yourself" escape hatches — they invite the orchestrator to
implement, which collapses the role split. Only if the user states the target
harness genuinely lacks delegation should you adapt the template to sequential
fresh-context phases.

## §2 Wave table

| Wave | 内容 | 対応 Phase | 並列可否 |

One row per spec phase. Derive parallelism from file-set intersection only
(backend-only vs frontend-only waves can run together); spell out ordering
dependencies as "W3 は W2 マージ後のみ" with the reason (e.g. W3 relies on W2's
safety-net tests being on main). Baseline wave runs first, alone, on main —
it is the comparison basis for everything.

Rules: merges are serialized even when work is parallel; after each merge,
in-flight branches absorb main and re-run their rubric commands.

## §3 Subsession message templates

Two literal templates the orchestrator fills in:

**Implementer**: wave number, scope (exact phase + debt items, "他に触れるな"),
branch name, pointer to the spec's Behaviors-To-Preserve / Non-Negotiables /
Stop-And-Ask / Out-of-scope sections, the current Distilled Rules pasted in, and
the required report shape (files changed, commit hashes, verification command
output tails, items stopped under Stop-And-Ask with evidence, pitfalls learned).

**Verifier**: "実装の経緯は知らされていない。先入観なしで判定せよ", the target
diff, the pasted rubric lines, the procedure (read diff → run every rubric command
yourself → sweep diff for spec violations), and the output contract: per-item
PASS / FAIL / BLOCKED with command output as evidence; any FAIL ⇒ overall FAIL with
minimal actionable findings.

**Relay rules**: FAIL findings (and only those) go back to the same implementer
subsession; bounded retries per wave (default 3), after which the wave is parked
and recorded as a human question. Never an unbounded loop.

## §4 PR and merge policy

1. Branch naming (`refactor/w<N>-<slug>`), conventional commits.
2. Orchestrator opens the PR; body lists wave, debt items, verification results.
3. Verifier judges the PR diff.
4. **Verifier PASS is the only merge authorization.** Not orchestrator judgment,
   not implementer self-report. CI green also required if CI exists.
5. Post-merge: other branches rebase/merge main, re-verify, then continue.
6. BLOCKED verdicts (environment missing etc.) never merge; record and escalate.

## §5 Memory log

`orchestration-log.md` at repo root, orchestrator-only writes. Sections:
Baseline / Wave Status / Failures / **Distilled Rules**. Distilled Rules admits
only verified facts, via the progression fail → investigate → verify → distill —
no speculation ("maybe X?" stays in Failures until verified). Every new implementer
gets the latest Distilled Rules pasted in: the same failure must not happen twice.

## §6 Orchestrator-level stop conditions

- Implementer hits a spec Stop-And-Ask → skip that item, record, continue the wave.
- Immediate full stop (all waves): direct push to main; diff in generated/protected
  files; a migration appears; out-of-scope paths in any diff. These map 1:1 to the
  spec's §11 — list them concretely for this repo.
- Unanswered pre-implementation questions: follow the spec's safe-side defaults;
  incorporate answers if they arrive mid-run.

## §7 Rubric

The contract the verifier executes. Format: grouped per wave (R0, R1, … plus RG
global), each line a checkbox of **command + expected result**, e.g.:

```
- [ ] `rg 'agent-runner' ARCHITECTURE.md` が 0 件。
- [ ] `pnpm exec moon run web:test` が PASS、カバレッジゲートを下回っていない。
- [ ] プロダクションコードへの変更が diff に含まれない。
```

Requirements:
- Every line independently checkable by a fresh agent with repo access.
- Negative assertions included (what must NOT appear in the diff) — these protect
  the 🛑 items and out-of-scope list.
- RG (global, run on main after all merges): all baseline gates pass with zero
  regression; protected-path diff empty against the baseline commit; migration
  check clean; every merged PR has a recorded verifier PASS in the memory log.
- Derive each line from the spec's per-item 検証 entries — the rubric must test
  exactly what the spec's phases produce, nothing it doesn't.

## §8 Final report

Point at the spec's Reporting Format section, plus orchestration extras: verifier
verdict history per wave, retry counts, final Distilled Rules, parked items and
questions for the human.
