# Codex model lineup & per-model tuning

> Snapshot: **2026-07**. Source: <https://developers.openai.com/codex/models>
> (now redirects to <https://learn.chatgpt.com/docs/models>).
> **This file goes stale fast — re-check the page (or ask the user) before pinning
> a model id.** Trust the models page for *which model exists*, and the prompting
> guide for *how Codex behaves* (the harness priors carry across versions). As of
> this snapshot the prompt-guidance page covers the 5.6 generation directly.

## Current lineup (2026-07)

| Model id | Best for | Availability |
|---|---|---|
| `gpt-5.6-sol` | Flagship — strongest capability for complex coding, computer use, research, and cybersecurity | App, web, CLI, IDE, Cloud, API |
| `gpt-5.6-terra` | Balanced everyday work, competitive with GPT-5.5 at lower cost. **Default starting point for most tasks.** | App, web, CLI, IDE, Cloud, API |
| `gpt-5.6-luna` | Fast and affordable; strong capability at the lowest cost — workers/subagents | App, web, CLI, IDE, Cloud, API |
| `gpt-5.3-codex-spark` | Near-instant, real-time coding iteration (text-only) | Research preview (ChatGPT Pro) |

**Previous generation** (still available, no longer the starting point):
`gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`.

**Deprecated** (do not pin for new work): `gpt-5.2`, `gpt-5.3-codex`. Older still:
`gpt-5.1-codex-max`, `gpt-5.1-codex`, `gpt-5.1-codex-mini`, `gpt-5-codex`.

## Reasoning effort

- Codex levels: **`low` / `medium` (default) / `high` / `xhigh` / `max` /
  `ultra`** — `max` is for the hardest quality-first workloads, `ultra` runs
  subagent-based delegation. The API additionally exposes `none` for
  latency-critical calls.
- **`medium` is the recommended all-around interactive default.**
- Newer generations do *more with the same effort*. When migrating a 5.5/5.4
  prompt to 5.6, **keep its effort baseline, then test one level lower** before
  assuming you still need `high`.

## Choosing model + effort (decision rules)

- **Default:** `gpt-5.6-terra` at `medium`.
- **Hard/ambiguous architecture, deep debugging, research, computer use:**
  `gpt-5.6-sol` at `high`/`xhigh`; `max` only for quality-first work.
- **Cheap parallel workers / subagents / high-volume mechanical edits:**
  `gpt-5.6-luna` (or `gpt-5.4-mini`) — set per-agent via `model` in the agent
  TOML. `ultra` effort (subagent delegation) is a design choice, not a default.
- **Tight iteration loops / latency-critical:** `gpt-5.3-codex-spark` if
  available, accepting lower ceiling.
- **Verbosity ≠ effort:** keep effort for difficulty, set `text.verbosity` for
  output length (see `gpt5-prompting.md`).

## How to record model-specific overrides

When a project standardizes on a model/effort, don't bury it in each task prompt:

- Put it in **AGENTS.md** (human + model both read it), and
- For subagents, set `model` / `model_reasoning_effort` in the `.codex/agents/*.toml`
  so each worker role gets the right tier (e.g. explorer on `gpt-5.6-luna`, final
  reviewer on `gpt-5.6-sol` at `high`).

> To adjust this skill for a new model, edit **this file** (the lineup + decision
> rules) — the templates reference "the model from `models.md`" rather than
> hardcoding ids, so updating here propagates everywhere.
