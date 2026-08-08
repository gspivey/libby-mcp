# agent-router-template

A GitHub template for agent-router–driven autonomous development: write a Kiro spec, serialize
it into an ordered work queue, and let an agent ship one item per session.

## What this is

This is a starting point for **agent-router–driven autonomous software development**. You write
a [Kiro](https://kiro.dev) spec describing what to build, **serialize it into an ordered
[`ROADMAP.md`](ROADMAP.md) work queue**, point the
[agent-router](https://github.com/gspivey/agent-router) daemon at the repo, and the agent ships
**one ROADMAP item per session**: branch, implement and test, open a PR, react to CI, and
squash-merge when green.

The template ships the **harness** — conventions ([AGENTS.md](AGENTS.md)), the reusable session
prompt ([prompts/agent-router.md](prompts/agent-router.md)), the ROADMAP format, and a
post-results-back CI workflow — plus **one tiny example spec** so the loop is demonstrable the
moment you connect the daemon. It ships **no implementation code**.

It is **language- and domain-neutral**. Nothing here assumes Python, Rust, Node, or any
particular stack; the CI auto-detects your toolchain. The only example is clearly labeled and
meant to be deleted.

## The loop

The [agent-router](https://github.com/gspivey/agent-router) daemon runs on your machine, watches
this repo over a webhook, and drives a Kiro session per pull request. One full cycle:

1. The **agent-router daemon** runs on your machine (Node.js), exposed via a `cloudflared`
   tunnel, listening for GitHub webhooks.
2. `config.json` lists the repos to watch (`owner`/`name`, a per-repo or default GitHub **PAT**,
   and the **webhook HMAC secret**) and the path to the agent CLI (`kiroPath`).
3. A GitHub webhook (`check_run`, `issue_comment`, `pull_request_review_comment`) reaches the
   tunnel, hits `POST /webhook`, is HMAC-verified, and runs through the **wake policy**.
4. Sessions are **bound to a PR**: every event for that PR routes back to the same agent
   conversation, so context accumulates across CI cycles.
5. **Trust tiers** (from `comment.author_association`) gate waking: Tier 1 = repo **owner** or
   `github-actions[bot]` wakes on any event; Tier 2 = MEMBER/COLLABORATOR wakes only on a
   comment starting with `/agent`; Tier 3 = everyone else never wakes.
6. You start a session — `echo "Implement the next ROADMAP item." | agent-router prompt --new`,
   or on a `cron` entry in `config.json`.
7. The agent follows [prompts/agent-router.md](prompts/agent-router.md): it picks the **first
   unchecked** ROADMAP item, reads the linked spec files, creates `agent/<slug>`, implements and
   tests, pushes, opens **one** PR, and registers the PR number with the daemon.
8. After every push the agent **stops and waits** — it never polls CI in a loop. CI runs in
   GitHub Actions and **posts results back** as a PR comment (from `github-actions[bot]`, a
   Tier-1 author) and as a `check_run` event. Either delivery wakes the agent, which fixes
   failures and pushes again, or proceeds when green.
9. When green, the agent ticks the ROADMAP checkbox (with the PR number) and the matching
   `tasks.md` checkboxes, then **squash-merges to `development`**. Session complete —
   **one PR per session, never a second.**

```
   GitHub webhook  ──►  agent-router daemon  ──►  wake policy
 (check_run / comment)      (cloudflared           (trust tier +
                              tunnel)                rate limit)
                                                        │
                                                        ▼
   squash-merge   ◄──  Kiro session  ──►  push  ──►  pull request
   to development      (one ROADMAP                       │
        │              item / session)                    ▼
        │                   ▲                       GitHub Actions CI
        │                   │                              │
        └───── repeat ──────┴───── CI posts result ◄───────┘
                            (PR comment + check_run wake the agent)
```

## Use this template

1. **Create your repo from this template.** Click **"Use this template"** on GitHub, or run:

   ```bash
   gh repo create <owner>/<repo> --template GerardsCuriousTech/agent-router-template
   ```

2. **Replace the placeholders.** Swap every `<owner>` / `<repo>` for your repo and delete the
   bundled example — see [Replace these placeholders](#replace-these-placeholders) below.

3. **Write your Kiro spec.** Add `requirements.md`, `design.md`, and `tasks.md` under
   `.kiro/specs/<your-feature>/`. See [kiro.dev](https://kiro.dev) for the spec format and the
   bundled `.kiro/specs/example-feature/` for the shape.

4. **Serialize it into [`ROADMAP.md`](ROADMAP.md).** Turn each task group into an ordered,
   checkbox work item that links back to its spec and tasks. The method, with a worked example,
   is in [docs/roadmap-from-kiro-specs.md](docs/roadmap-from-kiro-specs.md).

5. **Activate the workflows and connect the daemon.** Copy the sample CI workflows into place
   (`mkdir -p .github/workflows && cp sample-workflows/*.yml .github/workflows/`), then create a
   PAT, configure the webhook and tunnel, fill in `config.json`, and set the `ENABLE_PR_COMMENTS`
   Actions variable. Step by step in [docs/agent-router-setup.md](docs/agent-router-setup.md);
   the workflows are explained in [sample-workflows/README.md](sample-workflows/README.md).

6. **Start a session.** Kick off the first item:

   ```bash
   echo "Implement the next ROADMAP item." | agent-router prompt --new
   ```

   The agent picks the first unchecked ROADMAP item and runs the loop. Watch it with
   `agent-router tail <session_id>`.

## Replace these placeholders

After creating your repo, replace every `<owner>` / `<repo>` placeholder and remove the example.
Work through this checklist:

- [ ] [`prompts/agent-router.md`](prompts/agent-router.md) — `<owner>/<repo>` references in the
      session prompt.
- [ ] [`config.example.json`](config.example.json) — the `repos[]` entry's `owner` / `name`
      (and any `cron[].repo` slug).
- [ ] [`docs/agent-router-setup.md`](docs/agent-router-setup.md) — `<owner>/<repo>` in the
      webhook, PAT, and config instructions.
- [ ] `README.md` — the `gh repo create <owner>/<repo>` command above (and any other
      `<owner>/<repo>` you keep).
- [ ] **Delete the bundled example:** remove `.kiro/specs/example-feature/` and the **two**
      example items from [`ROADMAP.md`](ROADMAP.md) once you have added your real spec.

## What's in here

```
README.md                                   This file: what it is, the loop, setup, placeholder checklist.
ROADMAP.md                                  Serialized work queue; format header + 2 example items.
AGENTS.md                                   Generic agent conventions (spec-first, one item/session, branch model, voice).
CLAUDE.md                                   Short pointer to AGENTS.md plus the most-broken rules.
prompts/agent-router.md                     Reusable RFC-2119 session-driver prompt (uses <owner>/<repo>).
docs/roadmap-from-kiro-specs.md             How to serialize a Kiro spec into ROADMAP.md, with a worked example.
docs/agent-router-setup.md                  PAT + webhook + tunnel + config.json + ENABLE_PR_COMMENTS, step by step.
config.example.json                         agent-router config snippet: one repos[] entry, ENV: refs, optional cron.
sample-workflows/ci.yml                     Auto-detect lint + test, build report.md, post results back to the PR.
sample-workflows/integration-tests.yml      Template integration workflow: dispatch + PR, JUnit artifact, post-back.
sample-workflows/perf-tests.yml             Template perf workflow: dispatch inputs, artifact retention, post-back.
sample-workflows/README.md                  What the sample workflows do and how to activate them (copy into .github/workflows/).
.github/PULL_REQUEST_TEMPLATE.md            PR body: ROADMAP item, spec tasks, tests, tradeoffs, checklist.
.kiro/specs/example-feature/requirements.md Bundled example (EARS/Kiro requirements) — delete when you add yours.
.kiro/specs/example-feature/design.md       Bundled example design — delete when you add yours.
.kiro/specs/example-feature/tasks.md        Bundled example tasks — delete when you add yours.
.gitignore                                  Cross-language ignores (node, python, rust, OS, .env, agent-router local).
LICENSE                                     MIT.
```

## Bundled example

`.kiro/specs/example-feature/` is a tiny, clearly-labeled example: a **`greet` command-line
tool** that prints `Hello, world!` by default and `Hello, <NAME>!` with `--name <NAME>`. The
template ships **only the spec** — the agent writes the actual code when it implements the items.
It exists so the loop runs end to end the moment you connect the daemon, and so you can read a
real spec-to-ROADMAP mapping before writing your own.

Two ROADMAP items correspond to it:

1. Greeting function and default output.
2. The `--name` option.

**Delete the example when you add your real spec:** remove the `.kiro/specs/example-feature/`
directory and those two items from [`ROADMAP.md`](ROADMAP.md).

## Live examples

Real repos running this loop today:

- **[gspivey/agent-router](https://github.com/gspivey/agent-router)** — the daemon itself: bridges
  GitHub webhooks to PR-scoped Kiro sessions with a trust-tiered wake policy.
- **[gspivey/dpdk-stdlib-rust](https://github.com/gspivey/dpdk-stdlib-rust)** — a Rust networking
  library (DPDK-accelerated `UdpSocket`) built this way; see its `development` branch, `ROADMAP.md`,
  and `prompts/agent-router.md` for a full serialized queue.
- **[GerardsCuriousTech/Build-Your-Own-Full-Stack-LLM-Service-on-AWS](https://github.com/GerardsCuriousTech/Build-Your-Own-Full-Stack-LLM-Service-on-AWS)**
  — a GitBook course authored through the same loop, showing it is domain-neutral (not just code).

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Gerard Spivey.
