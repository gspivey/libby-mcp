# Sample workflows

Ready-to-use GitHub Actions workflows that implement the agent-router post-back loop: they run
the project's checks, build a `report.md`, and post it back to the pull request so the agent
gets results without polling.

## Why they live here and not in `.github/workflows/`

GitHub only runs workflows under `.github/workflows/`. This template ships them one directory
over, in `sample-workflows/`, on purpose:

- The template repository can be created, cloned, and pushed with an ordinary token. Writing
  to `.github/workflows/` requires a token with the `workflow` scope (classic) or "Workflows"
  permission (fine-grained); shipping the files as samples avoids forcing that on every clone.
- Activation is a deliberate, reviewable step. You copy in only the workflows you want, after
  filling in the placeholder steps in the integration and perf templates.

Nothing here runs until you activate it.

## Activate

From the root of a repository created from this template:

```bash
mkdir -p .github/workflows
cp sample-workflows/*.yml .github/workflows/
git add .github/workflows
git commit -m "Activate CI workflows"
git push
```

Then set the post-back variable so CI comments on PRs (see
[`docs/agent-router-setup.md`](../docs/agent-router-setup.md), "Activate the CI workflows and
enable the post-back variable"):

- **Settings -> Secrets and variables -> Actions -> Variables -> New repository variable**
- Name `ENABLE_PR_COMMENTS`, value `true`.

The `check_run` completion event wakes the agent regardless of this variable; the variable only
controls the more readable PR-comment delivery.

## What each workflow does

| File | Trigger | What it does |
| --- | --- | --- |
| `ci.yml` | `pull_request`; push to `main`/`development`/`agent/**` | Auto-detects the toolchain (Python+pytest, Node+npm, Rust+cargo), runs the checks, builds `report.md` from the real outcomes, and posts it back to the PR (gated on `ENABLE_PR_COMMENTS`, with a step-summary fallback). Green on an empty repo: with no tests it reports a neutral pass. Ready to use as-is. |
| `integration-tests.yml` | `workflow_dispatch`; `pull_request` | Template for a heavier integration suite. Contains a clearly-marked placeholder step to replace with your real integration command; uploads a JUnit-style artifact and posts a summary back. |
| `perf-tests.yml` | `workflow_dispatch` (with inputs) | Template for performance runs. Contains a clearly-marked placeholder perf step; uploads a results artifact and posts a summary back. Meant to be triggered by the agent with `gh workflow run perf-tests.yml --ref agent/<slug>` after standard CI is green. |

## Before you rely on the extended suites

`ci.yml` works out of the box. `integration-tests.yml` and `perf-tests.yml` ship with
placeholder steps that echo guidance instead of running real tests — replace those steps with
your project's actual integration and performance commands before treating their results as
authoritative.
