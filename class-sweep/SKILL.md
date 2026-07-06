---
name: class-sweep
description: After fixing any bug, sweep the codebase for the same defect class and check sibling views for consistency before declaring done. Use whenever a bug fix is completed, when the user asks 他でも起きていないか / 同じ問題 / 全componentチェック, or when a reviewer finding reveals a pattern that plausibly recurs elsewhere.
---

# Class Sweep

A found defect is a **class**, not an instance. Fixing exactly what was pointed
at and stopping is the #5 correction pattern across ~2,000 sessions (「多分同じ
ような問題が他の component でも起きているんじゃない?」「同じ問題を二度と放置するな」).

## Procedure

1. **Name the class.** State the general rule the bug violated (e.g. "Popover
   with minChars:0 re-opens on blur", "placeholder replacement misses block-level
   w:sdt wrappers", "list view derives count independently of detail view").
   If you cannot name the class, you have not found the root cause yet — go back.

2. **Sweep for the pattern.** Search the codebase (rg / ast-grep for structural
   patterns) for every other occurrence of the same construct. Shared components
   get fixed **at the component level**, not per call site (「compoent 単位で
   やらなきゃダメじゃないか」).

3. **Check sibling surfaces for consistency:**
   - list view ↔ detail view (a count/status fixed in one must match the other)
   - create ↔ update ↔ bulk paths
   - primary implementation ↔ fallback implementation (parity)
   - the same widget on other tabs/pages
   - sibling repos that share the library or convention (fix upstream when the
     defect lives in the shared code — PR to the library, not a local patch)

4. **Fix or file.** In-scope occurrences: fix now, same PR. Out-of-scope or
   broad-blast-radius occurrences: file a tracked issue (assigned to Eotel)
   citing the class — never leave them as prose in chat.

5. **Prevent recurrence.** Add a regression test for the class (not just the
   instance). If the class is mechanically detectable, add an ast-grep/lint rule.

## Boundary

This skill applies to **defects**. For a bounded explicit task (swap a package,
one copy edit), do exactly that and nothing else — unrequested cleanup is never
invited. The discriminator is: did I just fix something broken? → sweep. Was I
asked to make a specific bounded change? → stay narrow.
