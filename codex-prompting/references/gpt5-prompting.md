# GPT-5.x prompting principles (the model under Codex)

> Snapshot: 2026-06. Source: OpenAI prompt guidance
> (<https://developers.openai.com/api/docs/guides/prompt-guidance>).
> Codex models are GPT-5.x post-trained for coding; these general GPT-5.x prompting
> rules still apply on top of the harness priors in `codex-harness.md`.

## Outcome-first, not process-first

GPT-5.x works best with **prompts that define the target, not the procedure.** It
chooses efficient solution paths on its own; over-specified step-by-step
instructions make it worse, not better. State:

- the **user-visible outcome** (Goal),
- the **success criteria** (what must be true before it's done),
- the **constraints** (policy / safety / business / repo limits),

and let it pick the steps. This is the biggest behavioral shift from older models.

> *"Avoid carrying over every instruction from an older prompt stack. Legacy
> prompts often over-specify the process because earlier models needed more help
> staying on track."* — port old codex/GPT-4-era prompts down, not up.

## Don't fight yourself with absolute rules

- Reserve `ALWAYS` / `NEVER` / `must` for **true invariants only.** For judgment
  calls, give **decision rules** ("prefer X over Y when Z") instead of absolutes.
- The model tries to honor *every* instruction; **contradictory instructions burn
  reasoning** and produce hedged output. Audit a draft prompt for rules that can't
  all be true at once and resolve them.

## Eagerness / agentic persistence (two-sided dial)

- More efficient reasoning means **`low`/`medium` effort should be re-evaluated
  before escalating** — higher effort is not a free quality lever, it's a
  cost/latency knob.
- Control how readily it acts vs. asks via an explicit **collaboration style**
  block: *"Prefer making progress over stopping for clarification when the request
  is already clear enough to attempt."* — or the opposite if you want it to
  confirm first.
- Bound the loop with **stop conditions**: "resolve the query in the fewest useful
  tool loops," define what counts as sufficient evidence, and state when to retry /
  fall back / abstain. Without these it may either stop early or over-search.

## Verbosity

- `text.verbosity` (`low` / `medium` / `high`) controls output length independent
  of reasoning. Set `low` for concise/automation contexts; default `medium` for
  conversation.
- Note this is **separate** from reasoning effort — you can have a deeply-reasoned
  but tersely-reported answer (low verbosity, high effort).

## Personality vs. collaboration style — two short blocks

Keep them separate and short:

- **Personality** — tone, warmth, directness, formality, empathy.
- **Collaboration style** — when it asks vs. assumes, how it handles uncertainty.

## Tool preambles & phase metadata

- For streaming UIs where first-token-latency matters, prompt a **short preamble**:
  a brief visible update acknowledging the request and stating the first step.
- In long Responses workflows, the model emits `phase: "commentary"` for
  intermediate updates and `phase: "final_answer"` for the closeout. **If you
  reconstruct history, preserve `phase` values exactly** — dropping them degrades
  performance. (Harness concern; relevant if you build a custom Codex loop.)

## Checking its own work

- Give it **tools that let it validate outputs** when validation is possible. For
  coding specifically: ask for **targeted unit tests for changed behavior** and
  **build checks for affected packages** — that closes the loop better than telling
  it to "be careful."

## Grounding / retrieval budgets (for research-y tasks)

- Add explicit **stopping rules for search**: start with one broad search, only
  re-search if the top results lack core support or a specific fact is missing —
  *not* to improve phrasing or add nonessential citations.

## Suggested system/developer prompt skeleton

The guidance offers this structure (mirrored in `templates/codex-system-prompt.md`):

```
Role: [1-2 sentences]

# Personality
[tone, demeanor, collaboration style]

# Goal
[user-visible outcome]

# Success criteria
[what must be true before final answer]

# Constraints
[policy, safety, business limits]

# Output
[sections, length, tone]

# Stop rules
[when to retry, fallback, abstain]
```

## Output formatting

- Default to **plain paragraphs**; use headers / bold / bullets / numbered lists
  **sparingly**, and respect any user-stated formatting preference explicitly.
