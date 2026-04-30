# Core Beliefs

Last reviewed: YYYY-MM-DD

## Docs Are The Agent Interface

`AGENTS.md` is a map. `docs/` is the durable source of truth. Put design
decisions, product intent, execution plans, and quality policy in the repository
so agents can work without private context.

## Boundaries Beat Convenience

Prefer clear ownership and verifiable interfaces over short-term placement
convenience. Document important boundaries and connect them to checks when
possible.

## Promote Only After Reuse

Start local. Promote abstractions, docs, and rules after repetition proves they
belong at a higher level.

## Enforce What Matters

Important rules should not remain prose only. Promote recurring corrections into
tests, schemas, type checks, lint rules, or scripts when practical.
