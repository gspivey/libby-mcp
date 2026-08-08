<!--
One PR per agent-router session. This PR must address exactly one ROADMAP item and
target the `development` branch (never `main`). Fill in every section below; delete
the guidance comments before submitting.
-->

## ROADMAP item

<!-- The exact ROADMAP.md item this PR implements: number and title, e.g.
"1. Greeting function and default output". Exactly one item per PR. -->

## Spec tasks

<!-- The tasks.md IDs from the item's linked `.kiro/specs/<spec>/` that this PR
addresses, e.g. `1.1`, `1.2`. These must match the item's `tasks` line in ROADMAP.md. -->

## Summary

<!-- What changed and why, in a few sentences. Reference the requirements.md and
design.md decisions this implementation follows. -->

## Tests added

<!-- The tests added or updated and what they cover. Unit tests are required;
note any integration or performance tests added where the spec calls for them. -->

## Tradeoffs / notes

<!-- Design tradeoffs, deferred work, known limitations, or anything a reviewer
should know. Write "None." if there are none. -->

## Checklist

- [ ] Read the linked `requirements.md` and `design.md` before writing code.
- [ ] This PR addresses **exactly one** ROADMAP item.
- [ ] Ticked the ROADMAP.md checkbox for this item and recorded the PR number
      (`- [x] Complete · PR: #<n>`).
- [ ] Ticked the matching `tasks.md` sub-task checkboxes in the spec.
- [ ] Base branch is `development`, not `main`.
- [ ] Unit tests added and passing locally; ran the project's checks.
- [ ] CI considered: pushed and waited for the posted-back CI report rather than
      polling; failures addressed before requesting merge.
