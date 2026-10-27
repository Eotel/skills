---
name: adoption-triage
description: Classify and record proposed improvements from retrospectives. Use when deciding whether to adopt an improvement, log a decision for later review, present retrospective proposals, or consolidate stale proposals in the ledger.
---

# Adoption triage

Read [references/policy.md](references/policy.md) when a retrospective or review produces an improvement to tools, checks, skills, rules, or settings. Apply its adoption, review, and omission criteria before presenting the result. Present only adoption and review items; assign an owner to adoption work and record review items with `scripts/ledger.py` so work can continue. Do not end by asking the user to choose whether to adopt all items.

Read [references/ledger.md](references/ledger.md) when writing, collecting, or reading decisions. Use the script for all ledger writes. Human decisions belong in the adoption triage page; repository merge and Mac deployment follow each repository's rules.

Read [references/consolidation.md](references/consolidation.md) when a session is started to consolidate the ledger, such as from the adoption page's 整理 button. It reviews every candidate and proposes merges and retirements with evidence; decisions stay on the adoption page.
