---
name: class-sweep
description: Sweep for recurrence after fixing a plausibly reusable defect pattern, including sibling views and regression coverage.
---

# Class Sweep

Treat a defect as a reusable class when the same construct, shared component, or
parallel product surface could plausibly contain it.

## Procedure

1. **Name the class.** State the general rule the bug violated (e.g. "Popover
   with minChars:0 re-opens on blur", "placeholder replacement misses block-level
   w:sdt wrappers", "list view derives count independently of detail view").
   If the issue is genuinely isolated, record that conclusion and stay within
   the requested scope.

2. **Sweep for the pattern.** Search the codebase (rg / ast-grep for structural
   patterns) for every other occurrence of the same construct. Shared components
   get fixed **at the component level**, not per call site.

3. **Check sibling surfaces for consistency:**
   - list view ↔ detail view (a count/status fixed in one must match the other)
   - create ↔ update ↔ bulk paths
   - primary implementation ↔ fallback implementation (parity)
   - the same widget on other tabs/pages
   - sibling repositories that share the library or convention, when they are
     explicitly in scope

4. **Fix or report.** Fix in-scope occurrences in the same change. Report
   out-of-scope or broad-blast-radius occurrences with evidence; create an issue
   only when issue creation is in scope.

5. **Prevent recurrence.** Add a regression check for the class, not just the
   instance, when the repository has a meaningful test surface. Add a structural
   lint rule only when the pattern is mechanical and likely to recur.

## Boundary

This skill applies to defects with a plausible repeated pattern. Bounded package,
copy, and mechanical changes stay narrow.
