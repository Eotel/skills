# Subagents & sessions — the capability Codex hides from itself

> Snapshot: 2026-06. Sources: <https://developers.openai.com/codex/subagents>,
> Codex CLI reference (<https://developers.openai.com/codex/cli/reference>),
> Codex changelog. **Re-verify command/flag names before relying on them** — the
> CLI surface changes often.

## The core point

Codex can spawn parallel **subagents** and can **resume / fork / instruct other
sessions** — but it treats both as opt-in and **will not do either unless the
prompt explicitly tells it to.** From the subagents docs:

> *"Codex only spawns a new agent when you explicitly ask it to do so."*

So if you want fan-out or session reuse, your prompt must (a) state the intent and
(b) name the mechanism / agent. A vague "use parallelism where helpful" is usually
ignored. This is the highest-leverage thing to add to a Codex prompt for big tasks.

---

## A) Subagents (parallel workers, config-driven)

### Global config — `config.toml`

```toml
[agents]
max_threads = 6                  # concurrent open agent threads (default 6)
max_depth = 1                    # nesting depth of spawned agents (default 1 — no deep nesting)
job_max_runtime_seconds = 1800   # default per-worker timeout
```

### Custom agent definitions

Standalone TOML files in `~/.codex/agents/` (personal) or `.codex/agents/`
(project-scoped). Required keys:

```toml
name = "reviewer"
description = "When to use this agent"
developer_instructions = "Core behavior definition for this worker"
```

Optional overrides: `model`, `model_reasoning_effort`, `sandbox_mode`,
`mcp_servers`, `skills.config`, `nickname_candidates`.

See `templates/codex-subagent.toml` for a fill-in template.

### How orchestration works

- Codex handles orchestration across agents: **spawning, routing follow-up
  instructions, waiting for results, and closing threads.**
- When multiple agents run, **Codex waits until all requested results are
  available, then returns one consolidated response.** (Parallel fan-out → join.)
- `/agent` switches between active threads in an interactive session.
- Batch fan-out over rows: `spawn_agents_on_csv` (params include `csv_path`,
  `instruction` with `{column_name}` placeholders, `id_column`, `output_schema`,
  `output_csv_path`). Each worker must call `report_agent_job_result` exactly once.

### How to instruct Codex to use them (the phrasings that work)

Name the shape explicitly. Working examples from the docs:

> *"Spawn one agent per point, wait for all of them, and summarize the result for
> each point."*

> *"Have `pr_explorer` map the affected code paths, `reviewer` find real risks,
> and `docs_researcher` verify the framework APIs."*

Pattern for your prompts:

- **Define the agents first** (in `.codex/agents/*.toml` or inline by role), then
  in the task prompt **tell Codex to spawn them, who does what, and that it must
  wait and consolidate.**
- For ad-hoc fan-out without predefined agents: *"Spawn N subagents, one per
  `<unit>`; each does `<task>`; wait for all, then `<aggregate>`."*
- Mind `max_depth` (default 1): a subagent will not itself spawn deeper agents
  unless you raise it.

---

## B) Sessions — resume, fork, exec

Codex won't go hunting for prior conversations on its own. If continuity matters,
either *you* launch into the right session, or you tell Codex (in an environment
where it can shell out) to do it. Key CLI surface:

- `codex resume <SESSION_ID>` — resume a specific past session. `--last` resumes
  the most recent session in the cwd; `--all` considers sessions from any
  directory. Accepts an optional trailing **follow-up prompt**.
- `codex fork --last "..."` — branch off an existing session into a new one with a
  fresh initial prompt (trailing arg is the prompt, not a session id).
- `codex exec "..."` — run Codex **non-interactively** (automation / scripts);
  final results pipe to stdout. `codex exec resume` keeps session context and
  accepts `--output-schema` for structured JSON output.

### Prompting implication

- To continue a thread, prefer launching with `codex resume`/`fork` rather than
  re-pasting context — the session already holds it.
- If you want a running Codex agent to *find and instruct another session*, you
  must (a) confirm it has shell access in this sandbox, and (b) spell out the
  command, e.g. *"List recent sessions with `codex exec resume --last`, identify
  the one working on X, and send it this follow-up: …"* — it will not infer this
  workflow unprompted.
- For unattended pipelines, `codex exec ... --output-schema` gives you a parseable
  contract back; pair it with a schema in the prompt.

---

## C) agmsg — external spawn-and-message transport (cross-agent)

A) and B) are Codex's *own* primitives. **agmsg** (<https://github.com/fujibee/agmsg>)
is an external layer that sits beside them: it spawns **named, addressable** agent
processes — `codex` *or* `claude-code` — and lets you (or another agent) send them
messages, including a goal prompt, over a local SQLite message bus. Use it when you
want long-lived, named peers you can talk to repeatedly, rather than Codex's
fire-and-join subagents.

The model is the same "must teach it explicitly" lesson as A): an agent does not use
agmsg unless told to. Drive it through the `/agmsg` command (Claude Code) or the
shell scripts under `~/.agents/skills/agmsg/scripts/`.

```
/agmsg spawn <type> <name>        # type ∈ {codex, claude-code}; spawns a named agent
                                  #   (in a tmux pane/window), already addressable
/agmsg send <name> "<message>"    # send a message / goal prompt to that named agent
/agmsg                            # check inbox (replies come back here)
/agmsg history                    # full exchange
/agmsg despawn <name>             # graceful teardown
```

Spawn **blocks until the new agent is listening** (prints `status=ready`) unless you
pass `--no-wait`. So the canonical pattern is:

```
/agmsg spawn codex implementer          # bring up a named Codex peer
/agmsg send implementer "<goal prompt>" # hand it the goal — see templates/codex-instruction.md
```

The **goal prompt you send is still a Codex-directed prompt** — everything in this
skill (outcome-first framing, model/effort, AGENTS.md pointer, the must-teach
subagent rule) applies to its *content*. agmsg is only the transport; fill a
`templates/` skeleton for the message body.

### When agmsg vs. Codex-native

- **agmsg** when you want *named, durable, two-way* peers (an `implementer` and a
  `reviewer` you message back and forth across turns), or to mix `codex` and
  `claude-code` agents on one bus.
- **Codex subagents (A)** when you want ephemeral parallel workers that fan out and
  auto-consolidate inside one Codex turn.
- **`codex resume/exec` (B)** when you just need to continue or script one session.

Codex's own sandbox must allow agmsg's runtime dirs; its installer adds them to
`~/.codex/config.toml` `writable_roots` (`…/agmsg/db`, `…/teams`, `…/run`).

### Codex-specific gotchas (verified live)

Two things bite a spawned **codex** peer that don't bite `claude-code`:

1. **Codex has no Monitor → no real-time push delivery.** A `claude-code` peer can
   run in `monitor` mode and react to an inbound message the instant it lands. A
   codex peer cannot: it only sees its inbox **at the start of a turn**. So after a
   spawned codex finishes its startup `actas` turn it goes **idle and never notices
   a message you send afterward**. To make codex act on a goal prompt you must
   *trigger a turn that checks the inbox* — e.g. keep it in a loop, or send the
   message and then nudge it (another `/agmsg send`, or have it run `/agmsg`).
   Don't assume "send = it'll act"; for codex, **send then prompt it to receive.**
2. **Codex stalls on the directory-trust prompt.** Spawning codex into a fresh /
   untrusted dir parks it on *"Do you trust the contents of this directory?"* before
   it runs anything. Spawn codex into an **already-trusted project dir** (or
   pre-trust it) for unattended use, or the agent never starts.

(`/agmsg spawn` of a codex agent prints `codex has no Monitor — skipping readiness
wait (--no-wait implied)` — that line is the tell for caveat 1.)

### Getting a codex peer to actually act — three strategies

Because of caveat 1, picking the delivery model matters more for codex than for
claude-code. In rough order of increasing infrastructure:

1. **Initial-prompt / one-shot (lowest infra — recommended for "hand a worker one
   goal").** Don't `spawn` then `send`. Instead pre-send the goal and boot the
   worker with an *actionable* first-turn prompt — `actas`, then "read your inbox,
   do the latest message, reply to its sender." The single startup turn does the
   whole job; no Monitor, no PATH shim, no lingering bridge. This skill ships a
   helper that does exactly this and is **verified live** (a codex worker read the
   goal off the bus and replied in one turn):

   ```
   scripts/spawn-with-goal.sh codex worker --team <team> --project <dir> \
       -- "<goal text>"
   ```

   Teardown (it launches via `tmux new-window`, not agmsg `spawn`, so `despawn`
   has no placement record): `tmux kill-window -t worker && \
   ~/.agents/skills/agmsg/scripts/leave.sh <team> worker`. Note agmsg's stock
   `spawn` can't do this yet — its boot prompt is hardcoded to `actas` only.

2. **Send-then-nudge (zero setup, manual).** `spawn` (or the wrapper) the worker,
   then trigger a turn so it checks its inbox — e.g. another `/agmsg send`, or
   `tmux send-keys` into its pane to make it run `/agmsg`. Fine for ad-hoc pokes;
   tedious for a loop.

3. **codex-monitor-beta (most infra — for ongoing two-way chat).** Enables real
   push delivery for a long-lived codex peer via a shim + app-server bridge:
   `~/.agents/skills/agmsg/scripts/delivery.sh set monitor codex "$PWD"` and put
   `~/.agents/bin` first on `PATH`. Caveats are real: it intercepts the `codex`
   binary, it's beta/fragile (depends on codex app-server internals), the bridge
   lingers until killed, one codex identity per project, and **never** poll by
   launching full codex sessions on a short interval (a documented run burned
   ~2.2 GB logs / ~158 GB memory). Use `watch-once.sh` as a cheap gate instead.
   See agmsg's `docs/codex-monitor-beta.md`.

A `claude-code` peer sidesteps all of this — it has a real Monitor and receives in
real time — so if the peer doesn't *have* to be codex, that's the simplest path.

---

## When to reach for which

| Need | Mechanism |
|---|---|
| Independent parallel work (explore N modules, review M files) | Subagents (`spawn ... wait ... consolidate`) |
| Repeating the same task over a list/CSV | `spawn_agents_on_csv` |
| Continue earlier work with full context | `codex resume` / `fork` |
| Scripted, structured, non-interactive run | `codex exec [resume] --output-schema` |
| Specialized recurring role (reviewer, docs, explorer) | Predefined `.codex/agents/*.toml` + name it in the prompt |
| **Named, long-lived peers you message a goal prompt to** (codex *or* claude-code) | **agmsg** (`/agmsg spawn <type> <name>` → `/agmsg send <name> "<goal>"`) |
