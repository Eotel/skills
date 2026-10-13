---
name: diagnosing-bugs
description: Diagnose hard bugs and performance regressions with a reproducible feedback loop. Use when debugging a failure or investigating slow behavior.
license: MIT
metadata:
  upstream-repo: mattpocock/skills
  upstream-path: skills/engineering/diagnosing-bugs
  upstream-commit: 24fe0ef7737efae15c87225755e9f6f5965e4888
---

# Diagnosing Bugs

Build a feedback loop that reaches the reported symptom, minimise it, and verify
the fix against that loop.

## Diagnosis

Read [references/diagnosis.md](references/diagnosis.md) before investigating a
bug or performance regression. It defines the reproduction, hypothesis,
instrumentation, regression, and cleanup criteria, including child-process and
closed-pipe failures.

When reproduction requires a human action, use
[scripts/hitl-loop.template.sh](scripts/hitl-loop.template.sh); keep credentials
out of captured observations.

## Boundary and completion

Redact secrets in commands and evidence. Ordinary local diagnosis and repair
follow the user's authorized scope; unavailable access or production
instrumentation requires a concrete request.

Finish when the original symptom no longer reproduces, meaningful regression
coverage passes, and temporary instrumentation and owned processes are cleaned
up. Document a missing test seam instead of claiming the regression is covered.
