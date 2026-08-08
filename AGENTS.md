# AGENTS.md

Conventions for AI agents (and humans) working in a repository created from
`agent-router-template`. Read this file before writing any code. If anything here conflicts
with [`prompts/agent-router.md`](prompts/agent-router.md), the prompt's RFC-2119 wording wins
for session mechanics; this file is the explanation behind it.

## Project Overview

This repository was created from
[`GerardsCuriousTech/agent-router-template`](https://github.com/GerardsCuriousTech/agent-router-template).
It is driven by the [agent-router](https://github.com/gspivey/agent-router) daemon: a
maintainer writes a [Kiro](https://kiro.dev) spec, serializes it into an ordered
[`ROADMAP.md`](ROADMAP.md) work queue, points the daemon at the repo, and an agent ships one
ROADMAP item per session.

The template itself is language- and domain-neutral. It ships the harness — these
conventions, the session prompt, the ROADMAP format, and a CI workflow that posts results
back to the PR — plus one tiny, clearly-labeled example spec at
[`.kiro/specs/example-feature/`](.kiro/specs/example-feature/) so the loop is demonstrable
immediately. It ships no implementation code. The actual deliverables vary by project: a
library, a service, a CLI, a course, anything a spec can describe. Do not assume a language
or stack from this file; read the spec and detect the toolchain from what is in the repo.

## How a session works

The flow is the same every time. An agent-router session is bound to one PR, and every
GitHub event for that PR routes back to the same conversation, so context accumulates across
CI cycles.

1. Pick the first unchecked item in [`ROADMAP.md`](ROADMAP.md).
2. Read the spec files that item links before writing code.
3. Create an `agent/<slug>` branch, implement the item, and add tests.
4. Run the project's checks locally; fix failures.
5. Push, open exactly one PR targeting `development`, and register it with agent-router.
6. Stop. Wait for CI to post results back. Iterate on the feedback or proceed when green.
7. Tick the ROADMAP checkbox and the matching `tasks.md` checkboxes, then squash-merge to
   `development`. The session is complete.

The sections below explain the rules behind each step.

## Read the spec first

ROADMAP items are intentionally terse. The full requirements live in the spec. Before
writing any code for an item:

- Read [`requirements.md`](.kiro/specs/example-feature/requirements.md) for the spec named on
  the item's `Spec:` line (`.kiro/specs/<spec>/requirements.md`) — the EARS/Kiro acceptance
  criteria define what "done" means.
- Read `design.md` in the same directory — the chosen language, file layout, public
  signatures, and test locations. Follow them; do not invent a different structure.
- Open `tasks.md` and find the task numbers the ROADMAP item cites (for example `1.1`,
  `1.2`). Those are the exact checkboxes you implement and tick this session.

If the spec is ambiguous or self-contradictory, do not guess past it. Implement what is
unambiguous, and surface the gap in the PR body so a human can resolve it.

## ROADMAP is the work queue

[`ROADMAP.md`](ROADMAP.md) is the single source of truth for what to build next, in what
order. One item is one session. Each item carries the work in a fixed shape:

```
### <n>. <Item title>

<One-paragraph scope description.>

- Spec: `.kiro/specs/<spec>/` · tasks `<n.n>`, `<n.n>`
- [ ] Complete · PR: —
```

Select the **first** item whose `Complete` checkbox is unchecked. Implement exactly that one
item and open exactly one PR. Do not skip ahead, batch two items, or start a second feature
after merging — even if the next item looks trivial. If the first unchecked item is blocked
(missing spec, missing toolchain, unresolvable ambiguity), stop and report the blocker rather
than silently moving to a later item.

ROADMAP items map one-to-one to spec tasks. The generation method is documented in
[`docs/roadmap-from-kiro-specs.md`](docs/roadmap-from-kiro-specs.md); read it if you need to
understand why an item is scoped the way it is, but the maintainer owns the queue — agents
implement items, they do not add or reorder them.

## Branch and merge model

Three branch roles, used by these exact names everywhere:

| Branch          | Role                                                                       |
| --------------- | -------------------------------------------------------------------------- |
| `main`          | Stable, default, protected. Humans promote to it deliberately.             |
| `development`   | Integration branch that agent PRs merge into.                             |
| `agent/<slug>`  | One per feature/session. Branched from `development`. PR targets it.       |

Rules:

- Branch from `development` with a short, descriptive slug: `git checkout -b agent/<slug>`.
- The PR **targets `development`**, never `main`.
- Merge with **squash** so each item lands as one commit on `development`.
- **Never push to `main`.** Never open a PR against `main`. Promotion to `main` is a human
  decision made outside the agent loop.
- One feature branch and one PR per session. Do not reuse a branch across sessions.

CI runs on `pull_request` (any base) and on pushes to `main`, `development`, and
`agent/**`, so your branch is checked from the first push.

## Build and validate

This repository is language-agnostic. Run whatever checks the project defines — read
`design.md` and look at the repo to find them (a `Makefile`, `package.json` scripts, a
`justfile`, `pyproject.toml`, `Cargo.toml`, and so on). Do not invent a build system the
project does not use.

The CI workflow ([`sample-workflows/ci.yml`](sample-workflows/ci.yml)) auto-detects the
toolchain and runs the matching checks if present:

- `pytest`/Python tests -> run them (linting such as `ruff` is optional).
- `package.json` present -> `npm test`.
- `Cargo.toml` present -> `cargo test`.
- No tests found -> reported as a neutral pass.

The workflows ship in [`sample-workflows/`](sample-workflows/) and are activated by copying
them into `.github/workflows/` (see [`sample-workflows/README.md`](sample-workflows/README.md)
and [`docs/agent-router-setup.md`](docs/agent-router-setup.md)); once active, CI runs from
`.github/workflows/ci.yml`.

Run the same checks locally before you push, and fix every failure locally first. Pushing a
red branch to get CI to tell you what you already know wastes a wake cycle. Tests are
required for the code an item adds; commit tests alongside (or before) the implementation.

Two optional workflow templates ship for heavier suites —
[`sample-workflows/integration-tests.yml`](sample-workflows/integration-tests.yml) and
[`sample-workflows/perf-tests.yml`](sample-workflows/perf-tests.yml). They contain
clearly-marked placeholder steps for the maintainer to fill in. Do not run integration or
performance suites locally and treat the output as authoritative; trigger the workflow and
read the result CI posts back.

## Post-back and no-poll

After every `git push`, **stop and wait.** Do not poll CI.

CI runs in GitHub Actions and posts its result back to the PR two ways: as a comment from
`github-actions[bot]` (a Tier-1 author in agent-router's trust model) and as a `check_run`
completion event. Either delivery wakes the session. That is the whole point of the loop —
the agent does not need to watch CI, because CI comes to it.

Concretely:

- Do **not** run `gh run watch`, `gh run view`, or `gh run list` in a loop, and do not sleep
  and re-check. That burns the session and races the webhook.
- After pushing, end your turn. When the result arrives, act on it: fix failures and push
  again, or proceed if green.
- The PR comment delivery is gated on the repo Actions variable `ENABLE_PR_COMMENTS`. The
  `check_run` event fires regardless, so the session still wakes even if comments are off.
  Setup is documented in [`docs/agent-router-setup.md`](docs/agent-router-setup.md).

If CI cannot be made green after a reasonable number of cycles, post a PR comment summarizing
the blocker and stop. Do not thrash.

## Finishing the loop

A session is not done when CI goes green. Before you squash-merge, the feature branch must
contain:

1. The ROADMAP item's line updated from `- [ ] Complete · PR: —` to
   `- [x] Complete · PR: #<number>`.
2. The matching `tasks.md` checkboxes ticked for the task numbers the item cited.

Then squash-merge to `development`. One PR per session — never open a second.

## Voice

Write like an engineer leaving notes for the next engineer.

- No emoji. Anywhere — code, comments, commit messages, PR bodies, docs.
- No AI-chat artifacts: no "Sure!", "Great question!", "As an AI", "I'd be happy to", "Let me
  explain", "Certainly". Just say the thing.
- Direct, concise, technical prose. American spelling. Hard-wrap prose near 95 columns where
  it reads naturally.
- Commit messages and PR bodies describe what changed and why, not that an agent did it.

## DO

- Read the linked spec (`requirements.md` + `design.md`) before writing code.
- Implement exactly one ROADMAP item per session and tick its tasks before merge.
- Branch from `development` as `agent/<slug>`; target the PR at `development`.
- Run the project's checks locally and fix failures before pushing.
- Write tests for what you add; commit them with (or before) the implementation.
- Push, then stop and wait for CI to post back.
- Open exactly one PR and register it with agent-router.
- Squash-merge to `development` once green and the ROADMAP/tasks are ticked.
- Report blockers (missing toolchain, auth failure, ambiguous spec) and stop.

## DON'T

- Don't push to `main` or open a PR against `main`.
- Don't poll CI (`gh run watch`/`view`/`list` in a loop, or sleep-and-recheck).
- Don't start a second ROADMAP item or open a second PR in one session.
- Don't skip the spec because an item "looks like a one-liner".
- Don't add, remove, or reorder ROADMAP items — the maintainer owns the queue.
- Don't run `sudo`, bootstrap a toolchain via a user-space package manager, or edit
  credentials. Report the missing dependency or auth error and stop.
- Don't use emoji or AI-chat filler in any output.
- Don't treat local integration/perf output as authoritative — use the workflow result.

## Domain knowledge: which doc to read when

| When you need to...                                  | Read                                                                 |
| ---------------------------------------------------- | -------------------------------------------------------------------- |
| Know what to build next                              | [`ROADMAP.md`](ROADMAP.md)                                            |
| Run a session correctly (the RFC-2119 rules)         | [`prompts/agent-router.md`](prompts/agent-router.md)                  |
| Understand what an item requires                     | `.kiro/specs/<spec>/requirements.md` + `design.md`                   |
| Find the exact tasks an item maps to                 | `.kiro/specs/<spec>/tasks.md`                                         |
| Understand how the ROADMAP was generated             | [`docs/roadmap-from-kiro-specs.md`](docs/roadmap-from-kiro-specs.md) |
| Set up the daemon, tunnel, PAT, and webhook          | [`docs/agent-router-setup.md`](docs/agent-router-setup.md)           |
| Understand the CI post-back contract                 | [`sample-workflows/ci.yml`](sample-workflows/ci.yml)                 |
| Write the PR body                                    | [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) |
| See the loop end to end                              | [`README.md`](README.md)                                             |
| See real projects driven this way                   | [dpdk-stdlib-rust](https://github.com/gspivey/dpdk-stdlib-rust) · [Build-Your-Own-Full-Stack-LLM-Service-on-AWS](https://github.com/GerardsCuriousTech/Build-Your-Own-Full-Stack-LLM-Service-on-AWS) |
