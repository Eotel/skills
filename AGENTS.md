# Eotel Skills

Agent skills packaged for APM, used by both Claude Code and Codex. Each skill is
a directory with a `SKILL.md`, optional `references/` and `scripts/`, and tests
under `tests/`.

## Authoring

- The description says what the skill does, then the action that triggers it,
  in one or two sentences. Name an action, not a domain: "Create and validate
  Postgres schema migrations. Use when adding or changing a migration, or
  reviewing its rollout." rather than "Use when working with databases, queries,
  models, or persistence."
- Root `SKILL.md` is a router: what the skill does, which reference or script to
  read and when, what done looks like, and the safety boundary. Ordered
  procedures, command recipes, templates, and environment details live in
  `references/` or `scripts/`; a step itinerary in the root overconstrains
  current models and loads on every use.
- Point to material with the condition that makes it relevant ("read
  `references/x.md` when ..."), not as reading every time.
- Ordinary reversible work gets no approval gate because a skill is active;
  state only the real boundaries (destructive, external, or production actions).
- Check model IDs, effort levels, CLI flags, and SDK details against current
  primary documentation instead of copying them into skills.
- Prompt-level and `AGENTS.md`-level guidance for Codex lives in
  `codex-prompting/references/prompt-design.md`.

## Scripts and tests

- Scripts use the Python standard library and document their options with
  `--help`.
- Tests live in `tests/test_<skill>_scripts.py` as offline `unittest` cases:
  real git in temporary directories, external CLIs mocked at the boundary.

## Validation

- `python3 -m unittest discover -s tests`
- `uv run --with pyyaml python <skill-creator>/scripts/quick_validate.py ./<skill>`
  with the `skill-creator` copy bundled with Codex (and Claude Code's, when
  installed).

## Adding a skill

- Add it to the Skills list in `README.md`.
- After it merges, declare `Eotel/skills/<skill>` in chezmoi-dotfiles
  `dot_apm/apm.yml`; until then it is not installed anywhere.
