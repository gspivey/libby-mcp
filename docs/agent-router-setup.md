# Connecting this repo to agent-router

This document is the operator runbook for wiring a repository created from
`GerardsCuriousTech/agent-router-template` to the
[agent-router](https://github.com/gspivey/agent-router) daemon, so the autonomous loop
described in [`AGENTS.md`](../AGENTS.md) and [`prompts/agent-router.md`](../prompts/agent-router.md)
runs end to end: webhook in, agent session out, CI results posted back to the PR.

Throughout, replace `<owner>/<repo>` with your repository's slug (for example
`octocat/my-service`). This is a document-only guide — it tells you which secrets and
variables to create; it never contains real secret values. Generate your own.

The numbered steps are roughly in dependency order. Do them top to bottom the first time.

---

## How the pieces fit together

```
GitHub repo (<owner>/<repo>)
   |  webhook: check_run, issue_comment, pull_request_review_comment
   v
cloudflared tunnel  -->  POST https://<tunnel>/webhook
   v
agent-router daemon (your machine, Node.js)
   |  HMAC verify -> wake policy (trust tier) -> PR-bound session
   v
agent CLI implements, pushes to agent/<slug>, opens one PR
   |
   +-- GitHub Actions CI runs, posts report.md back to the PR
        (github-actions[bot] comment + check_run event) --> wakes the same session
```

The agent never polls CI. After each push it stops and waits; the post-back comment and the
`check_run` completion event are what wake it again. See [Post-back CI](#7-enable-the-post-back-ci-variable)
below.

---

## 1. Prerequisites

Install and verify these on the machine that will run the daemon:

- **Node.js 20 or newer** and **npm** — `node --version` should print `v20.x` or higher.
- **git** — `git --version`. The daemon shells out to git for branch and PR work.
- **The agent CLI** — the coding agent that agent-router drives over the Agent Client
  Protocol (Kiro CLI in the reference setup). Note its absolute path; you will set it as
  `kiroPath` in the config. Confirm it runs standalone before continuing.
- **cloudflared** — Cloudflare's tunnel client, used to expose the local webhook server to
  GitHub. Install per Cloudflare's docs and confirm `cloudflared --version`.
- **The agent-router daemon itself**, cloned and installed:

  ```bash
  git clone https://github.com/gspivey/agent-router && cd agent-router
  npm install
  ```

- **A GitHub account with admin access to `<owner>/<repo>`** — required to add the webhook,
  branch protection, and the Actions variable.

---

## 2. Create a GitHub personal access token (PAT)

The daemon authenticates to GitHub as you, using a PAT. The agent uses this token to push
branches, open and merge PRs, and read check results. Create one of the following:

### Option A — fine-grained PAT (recommended)

GitHub Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens ->
Generate new token.

- **Resource owner:** the owner of `<owner>/<repo>`.
- **Repository access:** Only select repositories -> pick `<owner>/<repo>`.
- **Repository permissions:**
  - **Contents:** Read and write — push branches and commits.
  - **Pull requests:** Read and write — open, comment on, and merge PRs.
  - **Commit statuses:** Read — read CI status.
  - **Checks:** Read — read check-run conclusions.
- **Expiration:** set a reminder to rotate it; the daemon reads it fresh on restart.

### Option B — classic PAT

GitHub Settings -> Developer settings -> Personal access tokens -> Tokens (classic) ->
Generate new token. Select the **`repo`** scope (full control of private repositories). This
is broader than necessary but works for any repo you own.

### Export it

Export the token as the environment variable your config references. The config in this
template uses `GITHUB_TOKEN` as the `defaultGithubToken`, and optionally a per-repo override
such as `GH_TOKEN_<REPO>`:

```bash
export GITHUB_TOKEN="<your-pat>"          # used as defaultGithubToken
# optional per-repo override referenced by repos[].token in config.example.json:
# export GH_TOKEN_MY_REPO="<your-pat>"
```

Put these exports in the shell profile or process-manager unit that launches the daemon so
they survive restarts. Never commit the token. See
[`config.example.json`](../config.example.json) for how `ENV:` references resolve.

---

## 3. Generate the webhook HMAC secret

GitHub signs every webhook delivery with an HMAC-SHA256 of a shared secret; the daemon
verifies the signature and rejects anything that does not match. Generate a high-entropy
random value:

```bash
openssl rand -hex 32
```

Export it as the variable the config references (this template uses `GITHUB_WEBHOOK_SECRET`
as the top-level fallback secret):

```bash
export GITHUB_WEBHOOK_SECRET="<the-value-from-openssl>"
```

You will paste the **same** value into the GitHub webhook form in step 6. The top-level
`webhookSecret` is the fallback for any repo without its own `repos[].webhookSecret`; set a
per-repo secret only if you run separate webhooks with different secrets.

---

## 4. Start the cloudflared tunnel

The daemon's webhook server listens locally (default port `3000`). cloudflared exposes it at
a stable public hostname GitHub can reach. The agent-router repo ships helper scripts:

```bash
# from the agent-router checkout
./scripts/setup-tunnel.sh           # one-time: creates/configures the named tunnel
# then, in a terminal that stays open:
cloudflared tunnel run agent-router
```

cloudflared prints the public hostname (for example
`https://<tunnel-id>.cfargotunnel.com`). Your **Payload URL** is that hostname plus
`/webhook`. Keep this terminal running, or install cloudflared as a service so the tunnel
survives reboots.

---

## 5. Configure the daemon (`config.json`)

In the agent-router checkout, copy the template's example config and edit it:

```bash
cp config.example.json config.json
```

Use [`config.example.json`](../config.example.json) in *this* repo as the model. Set:

- `kiroPath` — absolute path to your agent CLI (from step 1).
- `webhookSecret` — leave as `"ENV:GITHUB_WEBHOOK_SECRET"` (resolves from step 3).
- `defaultGithubToken` — leave as `"ENV:GITHUB_TOKEN"` (resolves from step 2).
- A `repos[]` entry for your repository:

  ```jsonc
  {
    "owner": "<owner>",
    "name": "<repo>",
    "roadmapPath": "ROADMAP.md"
  }
  ```

  Add `"token"` and/or `"webhookSecret"` to the entry only if you want per-repo overrides.

Token resolution is `repos[i].token` -> `defaultGithubToken` -> error. Webhook-secret
resolution is `repos[i].webhookSecret` -> top-level `webhookSecret`.

---

## 6. Add the webhook on GitHub

In the GitHub repo: **Settings -> Webhooks -> Add webhook**.

- **Payload URL:** `https://<tunnel>/webhook` (the hostname from step 4 plus `/webhook`).
- **Content type:** `application/json`.
- **Secret:** the exact value you generated in step 3.
- **SSL verification:** enabled.
- **Which events would you like to trigger this webhook?** Choose **Let me select individual
  events**, then check exactly:
  - **Check runs**
  - **Issue comments**
  - **Pull request review comments**

  Uncheck **Pushes** (and everything else) — the daemon ignores other event types.
- **Active:** checked.

Save. GitHub sends a `ping`; with the daemon running (step 8) you should see a successful
delivery in **Recent Deliveries**. A `401`/signature error means the secret in the form does
not match `GITHUB_WEBHOOK_SECRET`.

---

## 7. Activate the CI workflows and enable the post-back variable

This template ships its GitHub Actions workflows as samples in
[`sample-workflows/`](../sample-workflows/) (GitHub only runs workflows under
`.github/workflows/`, and shipping them outside it lets the repo be created and cloned with an
ordinary token). Activate them once, in the repo:

```bash
mkdir -p .github/workflows
cp sample-workflows/*.yml .github/workflows/
git add .github/workflows && git commit -m "Activate CI workflows" && git push
```

See [`sample-workflows/README.md`](../sample-workflows/README.md) for what each workflow does
and which placeholder steps to fill in for `integration-tests.yml` and `perf-tests.yml`
(`ci.yml` works as-is).

Once active, CI builds a `report.md` and posts it as a PR comment so the agent gets results
back without polling. That comment delivery is gated on a repository **Actions variable** (not
a secret), `ENABLE_PR_COMMENTS`, which is unset (off) by default.

In the GitHub repo: **Settings -> Secrets and variables -> Actions -> Variables tab ->
New repository variable**.

- **Name:** `ENABLE_PR_COMMENTS`
- **Value:** `true`

With it `true`, `.github/workflows/ci.yml` runs
`gh pr comment <number> --body-file report.md` on `pull_request` events; the resulting
`github-actions[bot]` comment is a Tier-1 author, so it wakes the bound session. When the
variable is unset or off, the comment step is skipped and the report falls back to the job's
step summary instead.

Note: the **`check_run` completion event fires regardless** of this variable. Even with
`ENABLE_PR_COMMENTS` off, CI finishing still produces a `check_run` webhook that wakes the
agent. The variable only controls the more readable PR-comment delivery, which is what you
want during normal operation.

---

## 8. Protect `main` and create `development`

The branch model (see [`AGENTS.md`](../AGENTS.md)): `main` is stable and default; agent PRs
target and squash-merge into `development`; each session works on `agent/<slug>`. A fresh
repo from this template ships only `main`, so create `development` and protect `main`:

```bash
# create development off the current main and push it
git switch main
git switch -c development
git push -u origin development
```

Then in **Settings -> Branches -> Add branch protection rule** for `main`:

- Require a pull request before merging.
- Require status checks to pass (select the **CI** check once it has run at least once).
- Disallow direct pushes / require the branch be up to date as you prefer.

You may apply lighter protection to `development` (for example, require the CI check) so
agents can squash-merge their own PRs once green.

---

## 9. Add the repo to the daemon and start a session

With config saved (step 5), tunnel up (step 4), webhook added (step 6), and the env vars
exported (steps 2-3), start the daemon:

```bash
# from the agent-router checkout
npm run dev
```

You are now listening. Kick off the first session, which makes the agent pick the first
unchecked item in [`ROADMAP.md`](../ROADMAP.md):

```bash
echo "Implement the next ROADMAP item." | agent-router prompt --new
```

The agent reads [`prompts/agent-router.md`](../prompts/agent-router.md), creates an
`agent/<slug>` branch, implements and tests the item, pushes, and opens one PR against
`development`. CI then runs and posts back; the same session wakes on that feedback, fixes or
proceeds, and squash-merges when green. One PR per session.

### Optional: run on a schedule with cron

Instead of (or in addition to) kicking off sessions by hand, add a `cron` entry to the
daemon's `config.json` pointing at a prompt file whose contents become the session prompt on
each fire. See the commented `cron` block in [`config.example.json`](../config.example.json)
and the agent-router README's "Cron sessions" section. A daily weekday example:

```jsonc
"cron": [
  {
    "name": "daily-roadmap",
    "schedule": "0 9 * * 1-5",
    "repo": "<owner>/<repo>",
    "promptFile": "/etc/agent-router/prompts/<repo>.md"
  }
]
```

Cron skips a fire if a session is already active for the repo, or if the previous session
ended in a non-clean state (you re-trigger those by hand after fixing the cause).

---

## The trust-tier model (why untrusted comments are safe)

agent-router decides whether a webhook should wake an agent using
`comment.author_association` from the payload. This is the defense against prompt injection
from arbitrary commenters — there is no allowlist to maintain.

- **Tier 1 — always wake:** the repository **owner**, and **`github-actions[bot]`**. Any
  event from these wakes the bound session unconditionally. This is precisely why CI's
  post-back comment (authored by `github-actions[bot]`) closes the loop, and why
  `check_run` completion events always wake the agent.
- **Tier 2 — wake only on `/agent`:** members and collaborators (`MEMBER` / `COLLABORATOR`).
  Their comments wake the agent **only** when the comment body starts with `/agent`. Normal
  discussion does not trigger the agent.
- **Tier 3 — never wake:** everyone else. Their comments are logged and ignored.

Combined with PR-scoped sessions (the daemon only wakes if a session is registered for that
PR), this keeps the agent focused on work you authorized and unreachable by drive-by
comments.

---

## Quick verification checklist

- [ ] `node --version` >= 20, `git`, agent CLI, and `cloudflared` all installed.
- [ ] PAT created (Contents RW + Pull requests RW + Commit statuses/Checks read, or classic
      `repo`) and exported as `GITHUB_TOKEN`.
- [ ] `GITHUB_WEBHOOK_SECRET` generated and exported.
- [ ] cloudflared tunnel running; Payload URL is `https://<tunnel>/webhook`.
- [ ] `config.json` has a `repos[]` entry for `<owner>/<repo>` and a valid `kiroPath`.
- [ ] Webhook added with content type `application/json`, the secret, and events Check runs +
      Issue comments + Pull request review comments. Ping delivery succeeded.
- [ ] Sample workflows copied from `sample-workflows/` into `.github/workflows/` and committed.
- [ ] Actions variable `ENABLE_PR_COMMENTS=true`.
- [ ] `development` branch created; `main` protected.
- [ ] Daemon running (`npm run dev`); first session started.

## Related docs

- [`AGENTS.md`](../AGENTS.md) — conventions the agent follows.
- [`prompts/agent-router.md`](../prompts/agent-router.md) — the session-driver prompt.
- [`ROADMAP.md`](../ROADMAP.md) — the work queue the agent pulls from.
- [`docs/roadmap-from-kiro-specs.md`](roadmap-from-kiro-specs.md) — how to fill the ROADMAP.
- [`config.example.json`](../config.example.json) — the daemon config snippet.
- [agent-router daemon](https://github.com/gspivey/agent-router) — full daemon README.
