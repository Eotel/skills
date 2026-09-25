# Agent Notes

## Context Pointers

- Read [ARCHITECTURE.md](ARCHITECTURE.md) when changing subsystem boundaries.
- Use [docs/index.md](docs/index.md) to find task-relevant documentation.
- Put multi-step implementation plans under `docs/exec-plans/active/`. Plans
  hold only current state and next steps; delete a plan when its work lands.

## Verification

- Run the narrowest check that could detect a regression in the changed behavior.
- Broaden verification for shared boundaries, refactors, release work, or failures.
- If a check cannot run, state what was attempted and the next best check.

## Project Rules

- Keep this file short. Link to detailed rules instead of duplicating them.
- Add project-specific commands and architecture pointers here.
- Carry out reversible, in-scope work without asking for repeated approval.
- Ask before destructive, externally visible, or materially scope-changing work.
