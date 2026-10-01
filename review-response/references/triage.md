# Triage and End States

Read when building or refreshing the work list.

## Intake

The work list comes from the tracker items returned to the author and from
follow-up issues filed by post-merge reviews. Reviewers keep returning items while
the run is in progress: pull the tracker again at every milestone and before the
final report, and classify new items the same way. When an item comes back with
nothing on the PR itself, look for the feedback on the issues the PR closed.

## Classification

| PR state | Review outcome | End state |
| -------- | -------------- | --------- |
| open | approved, possibly with optional points | merged after each point is addressed or answered (when the user authorized merging; otherwise ready to merge) |
| open | changes requested or comments only | fixed, answered, and review re-requested |
| merged | follow-up issues filed | fix PRs open with review requested; source item completed |
| merged | nothing left | source item completed |
| closed or superseded | any | source item completed with the reason |
| authored by someone else | any | out of scope for the author's run; listed with the reason |

A merged PR whose optional points were not in the merge gets them in the next PR
on the same work, or in one small PR that collects such points; it does not get a
follow-up PR per point.

## Rules that decide an item

- A hand-off to another session is not an end state. The row stays open until the
  end state is observed.
- Verify an issue's premise in the code before choosing among its options, and
  prefer the option that keeps today's public contract.
- Whether follow-up issues are fixed in this run is the user's scope decision.
- The tracker item for a review is completed when every finding has a fix or an
  answer, following the team's review skill.
