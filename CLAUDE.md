# CLAUDE.md

Read [AGENTS.md](AGENTS.md) first. It is the full guide for working in this repository — the
session loop, the branch model, build and validate, post-back, voice, and the DO/DON'T lists.
The session mechanics are specified, RFC-2119 style, in
[prompts/agent-router.md](prompts/agent-router.md). The work queue is [ROADMAP.md](ROADMAP.md).

This file is a short pointer plus the rules that get broken most often.

## Non-negotiables

- **Never push to `main`.** Branch from `development` as `agent/<slug>`; the PR targets
  `development`; squash-merge there. Promotion to `main` is a human decision.
- **One ROADMAP item per session, one PR.** Implement the first unchecked
  [ROADMAP.md](ROADMAP.md) item only. Do not start a second item or open a second PR.
- **Read the spec first.** Before writing code, read the item's
  `.kiro/specs/<spec>/requirements.md` and `design.md`, and find its tasks in `tasks.md`.
  Do not skip this because an item "looks trivial".
- **Finish the loop before merge.** After CI is green, tick the ROADMAP checkbox (with the PR
  number) and the matching `tasks.md` checkboxes on the branch, then squash-merge.
- **Don't poll CI.** After every push, stop and wait. CI posts results back as a PR comment
  and a `check_run` event, which wakes the session. No `gh run watch`/`view`/`list` loops.
- **Voice.** No emoji. No AI-chat filler ("Sure!", "Great question!", "As an AI"). Direct,
  technical prose, in code, comments, commits, PR bodies, and docs alike.
