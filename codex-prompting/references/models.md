# Codex model lineup & per-model tuning

> Snapshot: **2026-06**. Source: <https://developers.openai.com/codex/models>.
> **This file goes stale fast — re-check the page (or ask the user) before pinning
> a model id.** The Codex prompting *guide* lags the model lineup: as of this
> snapshot the guide still references `gpt-5.1-codex-max` / `gpt-5.3-codex` while
> the models page already lists the 5.4/5.5 generation. Trust the models page for
> *which model exists*, and the guide for *how Codex behaves* (the harness priors
> carry across versions).

## Current lineup (2026-06)

| Model id | Best for | Availability |
|---|---|---|
| `gpt-5.5` | Complex coding, computer use, knowledge work, research. **Default starting point for most tasks.** | CLI/SDK, app, IDE, Cloud, API |
| `gpt-5.4` | Professional work — strong coding, reasoning, tool use, agentic workflows | All Codex platforms |
| `gpt-5.4-mini` | Responsive coding tasks **and subagents** (cheap/fast workers) | All Codex platforms |
| `gpt-5.3-codex-spark` | Near-instant, real-time coding iteration | Research preview (ChatGPT Pro) |

**Deprecated** (do not pin for new work): `gpt-5.2`, `gpt-5.3-codex`. Older still:
`gpt-5.1-codex-max`, `gpt-5.1-codex`, `gpt-5.1-codex-mini`, `gpt-5-codex`.

## Reasoning effort

- Levels: **`low` / `medium` / `high` / `xhigh`.**
- **`medium` is the recommended all-around interactive default** — balances
  intelligence and speed. Escalate to `high`/`xhigh` only for the hardest tasks.
- Newer generations do *more with the same effort*: e.g. the guide notes
  `gpt-5.1-codex-max` at `medium` beat the prior `gpt-5.1-codex` at `medium` while
  using ~30% fewer thinking tokens. So when you move up a generation,
  **re-evaluate downward** before assuming you still need `high`.

## Choosing model + effort (decision rules)

- **Default:** `gpt-5.5` (or current top general model) at `medium`.
- **Hard/ambiguous architecture or deep debugging:** top model at `high`/`xhigh`.
- **Cheap parallel workers / subagents / high-volume mechanical edits:** a `-mini`
  model (e.g. `gpt-5.4-mini`) — set per-agent via `model` in the agent TOML.
- **Tight iteration loops / latency-critical:** a spark/near-instant model if
  available, accepting lower ceiling.
- **Verbosity ≠ effort:** keep effort for difficulty, set `text.verbosity` for
  output length (see `gpt5-prompting.md`).

## How to record model-specific overrides

When a project standardizes on a model/effort, don't bury it in each task prompt:

- Put it in **AGENTS.md** (human + model both read it), and
- For subagents, set `model` / `model_reasoning_effort` in the `.codex/agents/*.toml`
  so each worker role gets the right tier (e.g. explorer on a mini model, final
  reviewer on the top model at `high`).

> To adjust this skill for a new model, edit **this file** (the lineup + decision
> rules) — the templates reference "the model from `models.md`" rather than
> hardcoding ids, so updating here propagates everywhere.
