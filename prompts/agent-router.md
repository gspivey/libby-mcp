# Agent-Router Prompt — `<owner>/<repo>`

> The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD",
> "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be
> interpreted as described in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119).

This is the reusable session-driver prompt for the [agent-router](https://github.com/gspivey/agent-router)
daemon. It tells the agent how to advance the `ROADMAP.md` work queue by exactly one item
per session: branch, implement, test, open one PR, react to CI posted back as PR comments
and `check_run` events, then squash-merge to `development`.

Replace `<owner>` and `<repo>` with your repository's owner and name before use. The
slug `<owner>/<repo>` appears throughout.

Repo: `https://github.com/<owner>/<repo>`

---

## 1. Setup

The agent MUST create a persistent working directory **outside of `/tmp`** so the clone and
build artifacts survive across CI cycles within the session:

```bash
mkdir -p "$HOME/agent-runs"
WORKDIR="$HOME/agent-runs/$(date +%Y%m%d-%H%M%S)-<repo>"
mkdir -p "$WORKDIR" && cd "$WORKDIR"
git clone https://github.com/<owner>/<repo>.git
cd <repo>
gh auth setup-git   # point git's HTTPS credential at the session token agent-router provides
```

The agent's GitHub token is provisioned by agent-router. The agent MUST NOT add, replace, or
edit Git credentials or credential helpers beyond the single `gh auth setup-git` above (see
the auth rule in §8). If a commit fails with "Author identity unknown", the agent MAY set the
commit identity with `git config user.name` / `git config user.email` (a bot identity) — that
is a local commit identity, not a credential, and is the only git-config change permitted.

The agent MUST read `AGENTS.md` and `CLAUDE.md` in the repository root **before writing any
code**. These define the project's conventions, the branch model, the post-back CI contract,
and the project-specific commands the agent runs to build, lint, and test.

---

## 2. Roadmap Selection

The agent MUST select the **first** entry in `ROADMAP.md` whose completion checkbox is
unchecked (`- [ ] Complete`). The agent MUST implement exactly that one item and open
exactly one PR in this session. The agent MUST NOT begin a second item.

Each ROADMAP item carries a `Spec:` line pointing at one or more files under
`.kiro/specs/**` and a list of the `tasks.md` sub-tasks it covers. The agent MUST read
**all** spec files listed on the selected item's `Spec:` line before writing any code, and
MUST implement only the sub-tasks attributed to the selected item.

---

## 3. Implementation

1. The agent MUST create a feature branch off the latest `development`:
   `git checkout -b agent/<short-slug>`. The slug MUST be short and descriptive of the item.
2. Tests are REQUIRED. The agent MUST add tests that exercise the acceptance criteria from
   the spec. Integration or synthetic tests SHOULD be added where the spec calls for them.
   The agent SHOULD commit tests before implementation code.
3. The agent MUST run the project's checks **as documented in `AGENTS.md`** (build, lint,
   and test for the repository's toolchain) and MUST fix every failure before proceeding.
   The agent MUST NOT push code that fails the project's local checks.
4. Once local checks pass (per 3.3) the agent MUST push the branch:
   `git push -u origin agent/<short-slug>`. This first push precedes opening the PR;
   subsequent pushes within the iteration loop use `git push`.
5. The agent MUST open exactly one PR via `gh pr create` targeting `development`. The PR
   title MUST match the selected ROADMAP item name. The PR body MUST identify:
   - the ROADMAP item being addressed (name and position),
   - the spec tasks covered (the `tasks.md` sub-task IDs),
   - the tests added and what they verify,
   - any relevant tradeoffs or decisions.

   `.github/PULL_REQUEST_TEMPLATE.md` provides this structure; the agent MUST fill it out
   rather than leave placeholders.
6. Immediately after the PR is created the agent MUST call the agent-router MCP
   `register_pr` tool with the new PR number. This binds the session to the PR so every
   subsequent CI event routes back to this same conversation. The agent MUST NOT push
   additional commits until registration is confirmed.

---

## 4. CI Iteration

After any `git push` to a PR-bound branch — that is, once the PR is open and registered
per 3.5-3.6 — the agent MUST **stop and wait**.

The agent MUST NOT poll for CI results. It MUST NOT run `gh run view`, `gh run watch`,
`gh run list`, or any equivalent in a loop.

CI runs in GitHub Actions and **posts results back** to the PR in two ways: as a PR comment
from `github-actions[bot]` (a Tier-1 author) and as a `check_run` completion event.
Agent-router delivers either as an event that wakes this session. When a result arrives the
agent MUST act on it:

- If the checks failed, the agent MUST read the posted `report.md` comment (or the
  `check_run` output), fix the cause, commit, and push — then stop and wait again.
- If the checks are green, the agent MUST proceed to the next step.

The agent MUST react only to delivered events. It MUST NOT assume an outcome it has not
been told about.

---

## 5. Extended Tests (OPTIONAL)

If — and only if — this repository defines extended-test workflows under `.github/workflows/`
(`integration-tests.yml` and/or `perf-tests.yml` — the template ships these as samples in
`sample-workflows/`, activated by copying them into `.github/workflows/`), the agent SHOULD run
them **after** the standard CI on the PR is green.

As shipped these two templates trigger differently: `integration-tests.yml` also runs
automatically on every `pull_request` (so it fires on each push to the PR), while
`perf-tests.yml` is `workflow_dispatch`-only and runs solely when triggered. The agent
SHOULD trigger the perf workflow explicitly; it MAY also re-trigger integration tests on
demand. Either way the agent reacts only to the result posted back per §4.

The agent MUST NOT run these suites locally and MUST NOT treat local output as their
results. The agent triggers a workflow against the feature branch:

```bash
gh workflow run integration-tests.yml --ref agent/<short-slug>
# and/or
gh workflow run perf-tests.yml --ref agent/<short-slug>
```

After triggering, the agent MUST stop and wait for the result to be delivered as a PR
comment / `check_run` event, exactly as in §4. If the extended tests fail or report a
regression, the agent MUST fix the cause, push (which re-runs standard CI), and re-trigger
the extended workflow once CI is green again.

If the repository defines no such workflows, the agent MUST skip this section.

---

## 6. Finalize

Once standard CI (and any triggered extended tests) are green, the agent MUST commit the
following to the feature branch **before** requesting merge:

1. An update to `ROADMAP.md`: change the selected item's completion line from
   `- [ ] Complete · PR: —` to `- [x] Complete · PR: #<number>`, using the PR number from
   §3.6.
2. An update to the matching `tasks.md`: tick the sub-task checkboxes listed on the ROADMAP
   item's `tasks` line (e.g. `1.1`, `1.2`) from `- [ ]` to `- [x]`.

Both changes MUST be present on the feature branch, pushed, and reflected by green CI before
merge.

---

## 7. Merge

When standard CI is green, any triggered extended tests are green, and the feature branch
contains the `ROADMAP.md` and `tasks.md` updates, the agent MUST **squash-merge** the PR to
`development`:

```bash
gh pr merge <number> --squash --delete-branch
```

The session is then **complete**. The agent MUST NOT start a second ROADMAP item, open a
second PR, or continue working in the same session.

---

## 8. Constraints

- **One PR per session.** The agent MUST NOT open additional PRs or select a second ROADMAP
  item in this session.
- **Missing toolchain.** If the project's build or test commands fail because a required
  compiler, runtime, interpreter, or system library is absent, the agent MUST stop and
  report the missing dependency in a PR comment or session message. The agent MUST NOT
  attempt to bootstrap a toolchain via conda, snap, brew, or any other user-space package
  manager.
- **Auth failures — stop on the FIRST one; never route around them.** If `git push`,
  `gh pr create`, or any GitHub write fails with a `401`/`403`/permission error, the agent
  MUST stop on the **first** failure and report it. The agent MUST NOT retry with a different
  token, account, or remote; MUST NOT put a token in the clone/push URL; MUST NOT edit Git
  credentials or credential helpers (beyond the one `gh auth setup-git` in §1); and MUST NOT
  attempt any alternate write path (the GitHub REST contents/git-data API, `gh api`
  tree/commit creation, a fork, etc.) to get the change in. The session token is provisioned
  by agent-router and a denial is authoritative — halt and report so a human can fix the
  token's scope; do not work around it.
- **CI divergence.** If the agent cannot get CI green after a reasonable number of cycles,
  it MUST post a PR comment summarizing the blocker and stop. It MUST NOT thrash with
  speculative pushes.
- **No root.** The agent MUST NOT run `sudo`. If a task genuinely requires root, the agent
  MUST report it and stop.
