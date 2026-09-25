---
name: agentic-docs
description: Create or reorganize repository documentation for agentic work. Use for AGENTS.md, architecture docs, product specs, exec plans, or documentation governance.
---

# Agentic Docs

Use this skill to make repository knowledge discoverable, resumable, and
verifiable for coding agents. Keep this file as the routing layer; load
references only when needed.

## Portability And Privacy

Treat this skill as portable infrastructure. It must be safe to commit to a
personal dotfiles repo or share across unrelated projects.

- Do not include source-project names, customer names, private repository paths,
  internal URLs, secrets, tenant names, product codenames, or proprietary
  architecture details in the skill or templates.
- Generalize from examples into reusable placement rules and workflows.
- Keep project-specific details in the target repository's docs, not in this
  skill.
- Before committing updates to this skill, run a targeted `rg` check for known
  private names and local absolute paths.

## Decision Rule

Create or update docs when the project lacks a reliable answer to one of these
questions:

- Where should an agent start reading?
- Where do stable architecture decisions live?
- Where do product/user-facing requirements live?
- How is multi-turn implementation state resumed?
- Which generated contracts or external references are trusted?
- Which docs rules are mechanically checked?

If the repository already answers these questions clearly, avoid new structure.
Tighten links, freshness, or verification instead.

## Workflow

1. Inspect the project before writing.
   - Start with applicable `AGENTS.md`, `CLAUDE.md`, `README.md`, and docs
     indexes. Read architecture, plans, scripts, and lint/test configs only when
     the current documentation mode needs them.
   - Use `rg` to find existing mentions of docs, plans, architecture, ADRs,
     generated schemas, and product specs.
   - Identify the real subsystems and checks. Do not invent categories that do
     not fit the repository.
2. Choose the mode.
   - Bootstrap: no coherent docs system exists.
   - Audit: docs exist but navigation, freshness, or ownership is unclear.
   - Update: a code or architecture change needs docs to stay in sync.
3. For bootstrap or structural changes, read `references/doc-structure.md`.
4. For template generation, read `references/templates.md`, then prefer
   `scripts/bootstrap_docs.py` over manually recreating files.
5. Connect every new doc from the appropriate index.
6. Prefer checks over prose: link durable rules to tests, schemas, type checks,
   lint rules, textlint, or scripts when those checks exist.
7. Verify before reporting completion.
   - Run relevant markdown/text lint if available.
   - Run link or grep checks when docs were moved.
   - Report any pre-existing failures separately from the files you changed.

## Resources

- `references/doc-structure.md`: placement rules, bootstrap shape, and quality bar.
- `references/templates.md`: template catalog and generation workflow.
- `references/design-md-format.md`: Google Labs DESIGN.md format reference for visual design tokens.
- `scripts/bootstrap_docs.py`: copy the generic docs scaffold into a target repo.
- `scripts/check_plans.py`: check that exec plans hold only current and next work.
- `assets/templates/`: generic templates used by the script.
- `assets/optional/`: opt-in templates not copied by the bootstrap script (currently `DESIGN.md`).
