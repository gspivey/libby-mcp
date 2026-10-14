# ROADMAP

Ordered work queue for agent-router sessions. This is the serialized form of the Kiro
specs under [`.kiro/specs/`](.kiro/specs/): one dependency-ordered list of PR-sized items
an agent ships one at a time.

**How an agent uses this file:** pick the **first item whose checkbox is unchecked**
(`- [ ] Complete`), implement exactly that one item, and read every spec file named on the
item's `Spec:` line *before* writing any code. When CI is green, tick the item's box with
the PR number (`- [x] Complete · PR: #<n>`), tick the matching checkboxes in the relevant
`tasks.md`, and squash-merge the branch into `development`. **One item per session — never
start a second.** The full session contract lives in
[`prompts/agent-router.md`](prompts/agent-router.md) and the conventions in
[`AGENTS.md`](AGENTS.md).

Each item is sized for a single reviewable PR (roughly 300–500 lines of new or modified
code, tests included) and bundles a handful of related `tasks.md` sub-tasks. Items are
topologically ordered: by the time an agent reaches an item, every prerequisite it builds
on has already merged to `development`. For the method used to turn a spec into this queue,
see [`docs/roadmap-from-kiro-specs.md`](docs/roadmap-from-kiro-specs.md). For a real,
large-scale example of this format in a production project, see the ROADMAP of
[`gspivey/dpdk-stdlib-rust`](https://github.com/gspivey/dpdk-stdlib-rust/blob/development/ROADMAP.md).

---

## Active Roadmap

> **Libby MCP Server** — an MCP server exposing OverDrive/Libby library catalog search
> to Claude/Kiro. Three fact-finding tools (no recommendations or advisory logic — the
> LLM caller does reasoning). Serialized from `.kiro/specs/libby-mcp/` after 8 Kiro
> revisions and 10 Claude reviews.

### 1. Project scaffolding, CI, and test harness

Clone `agent-router-template`, extend the directory layout for a Python Lambda MCP server,
and establish the harness engineering foundations: pytest fixtures for Thunder API mocking,
CI pipeline with lint/typecheck/test/integration jobs targeting `development` branch,
pre-commit hooks, and the agent feedback harness (`check_compliance.py`). Copy spec context
files into `docs/references/` so the agent can look up Thunder API details without external
searches. Creates the AGENTS.md navigation document and Makefile with standard targets.
This is the foundation — all implementation PRs land on top of it.

- Spec: `.kiro/specs/libby-mcp/` · tasks `0`, `1`, `1a`, `1b`
- [x] Complete · PR: #2

---

### 2. TDD tests and full implementation (Tasks 2-9)

**This is a single large PR** per the spec's "Tasks 2-9 are ONE PR" workflow policy. The
TDD cycle within this PR:
1. Write all unit tests (Task 2) — committed as red (tests skip via `pytest.importorskip`)
2. Implement modules (Tasks 3-9) — tests go green incrementally
3. All tests pass before the PR merges

**Task 2 — Test specifications:** Unit tests for Thunder client (timeout, retries, response
parsing), auth module (case-insensitive `authorization` header, Bearer token extraction,
401 on missing/invalid), MCP server (`isError: true` envelope, JSON-RPC validation), and
all three tools (search_titles filters, get_availability batch, get_deep_link URL format).

**Task 3 — Thunder client:** `src/thunder_client.py` — HTTP client for OverDrive Thunder API.
10s timeout, structured error dict on failure, `User-Agent: libby-mcp/1.0`.

**Task 4 — Auth module:** `src/auth.py` — case-insensitive header lookup (API Gateway HTTP
API lowercases headers), Bearer token extraction, `LIBBY_MCP_API_KEY` env var comparison.

**Task 5 — MCP server:** `src/mcp_server.py` — JSON-RPC 2.0 protocol layer. Request
validation, tool dispatch, error envelope (`isError: true, content: [{type: "text", text}]`).

**Task 6 — search_titles:** `src/tools/search_titles.py` — catalog search with 11 filter
params. Calls `/v2/libraries/{slug}/media`. Returns `{titles[], facets, total_items, page,
total_pages, filtered_count}`.

**Task 7 — get_availability:** `src/tools/availability.py` — batch availability lookup.
Calls `/v2/libraries/{slug}/media/availability?titleIds=...`. Returns raw counts only.

**Task 8 — get_deep_link:** `src/tools/deep_link.py` — generates
`https://libbyapp.com/library/{slug}/.../{title_id}` URL. No API call.

**Task 9 — Lambda handler:** `src/handler.py` — API Gateway event parsing, auth check,
MCPServer dispatch, response formatting. Module-level Thunder client for warm invocations.

- Spec: `.kiro/specs/libby-mcp/` · tasks `2`, `3`, `4`, `5`, `6`, `7`, `8`, `9`
- [x] Complete · PR: #3

---

### 3. AWS infrastructure (CDK) and integration tests

Create the CDK stack at `infra/lib/libby-mcp-stack.ts` using `PythonFunction` from
`@aws-cdk/aws-lambda-python-alpha` for automatic dependency bundling. Lambda: Python 3.12,
ARM64, 256MB, 30s timeout. API Gateway HTTP API with `POST /mcp` route. Secrets Manager
for auto-generated API key injected via `unsafeUnwrap()` into env var — no runtime Secrets
Manager read needed. Deploy/destroy scripts in `scripts/`. Integration tests at
`tests/integration/test_live.py` — gated with `RUN_INTEGRATION=1`, tests against real
Thunder API, validates response shapes. Branch protection for `development`. This item
makes the server deployable.

- Spec: `.kiro/specs/libby-mcp/` · tasks `10`, `11`, `12`
- [x] Complete · PR: #4

---

### 4. FastMCP migration: backend (PR A)

Read `.kiro/specs/mcp-protocol-upgrade/requirements.md`, `design.md`, and `tasks.md` in
full, then implement Tasks 1–6 (`.dockerignore` fix, requirements.txt update,
`AuthMiddleware`, `handler.py` rewrite, delete `mcp_server.py`, update tests).

- Spec: `.kiro/specs/mcp-protocol-upgrade/` · tasks `1`, `2`, `3`, `4`, `5`, `6`
- [x] Complete · PR: #6

---

### 5. FastMCP migration: CDK GET route + deploy (PR B)

Read `.kiro/specs/mcp-protocol-upgrade/requirements.md`, `design.md`, and `tasks.md` in
full, then implement Task 7 (add `GET /mcp` route to CDK stack, deploy, post-deploy
verification). Depends on item 4 (backend migration merged).

The `GET /mcp` CDK route (AC-4.3.1) is implemented and merged in PR #7. The live
`cdk deploy` and post-deploy 405/200 verification (AC-4.3.2, AC-4.3.3) remain a
**pending operator step** — they require AWS credentials (SSO) and Docker (for
`PythonFunction` bundling), neither available to the unattended agent. An operator
must run `./scripts/deploy.sh` and the Task 7 curl checks, then close out this item.

- Spec: `.kiro/specs/mcp-protocol-upgrade/` · tasks `7`
- [x] Complete (code; AC-4.3.1) · PR: #7 — deploy + AC-4.3.2/4.3.3 pending operator

---

### 6. libby-mcp v2 — Group A: Targeted fixes (PR-A)

Read `.kiro/specs/libby-mcp-v2/requirements.md`, `design.md`, and `tasks.md` in full,
then implement tasks `A1`, `A2`, `A3`, `A4`.

- Spec: `.kiro/specs/libby-mcp-v2/` · tasks `A1`, `A2`, `A3`, `A4`
- [ ] Complete · PR: --

---

### 7. libby-mcp v2 — Group B: Multi-library search + hold intelligence (PR-B)

Read `.kiro/specs/libby-mcp-v2/requirements.md`, `design.md`, and `tasks.md` in full,
then implement tasks `B1`, `B2`, `B3`. Depends on item 6 (PR-A merged).

- Spec: `.kiro/specs/libby-mcp-v2/` · tasks `B1`, `B2`, `B3`
- [ ] Complete · PR: --

---

### 8. libby-mcp v2 — Group C: Content enrichment (PR-C)

Read `.kiro/specs/libby-mcp-v2/requirements.md`, `design.md`, and `tasks.md` in full,
then implement tasks `C1`, `C2`, `C3`. Depends on item 7 (PR-B merged).

- Spec: `.kiro/specs/libby-mcp-v2/` · tasks `C1`, `C2`, `C3`
- [ ] Complete · PR: --

---

### 9. Local integration test harness (Docker + CI)

Read `.kiro/specs/libby-mcp-local-integration/requirements.md`, `design.md`, and
`tasks.md` in full, then implement tasks `1`–`7`. Adds a `Dockerfile.integration` that
serves the ASGI app under uvicorn (no AWS/Lambda), a `docker-compose.integration.yml`
for local runs, an MCP protocol test suite (`tests/integration/test_mcp_protocol.py`)
gated by `RUN_LOCAL_INTEGRATION=1` and the `local_integration` marker, a secret-free
`local-integration` CI job that runs on every PR, and a `make integration-test` target.
Depends on item 8 (v2 Group C content enrichment merged) so the v2 enrichment fields
(`holds_per_copy`, `estimated_wait_days`, `series_name`, `series_position`, `narrator`,
`publisher`, `edition`, `is_abridged`, `hours`) exist in `search_titles` responses and
the AC-3.7 assertions can cover them.

- Spec: `.kiro/specs/libby-mcp-local-integration/` · tasks `1`, `2`, `3`, `4`, `5`, `6`, `7`
- [ ] Complete · PR: --

---

## Completed

Items move here after they merge to `development`.

### 1. Project scaffolding, CI, and test harness

- Spec: `.kiro/specs/libby-mcp/` · tasks `0`, `1`, `1a`, `1b`
- [x] Complete · PR: #2

### 2. TDD tests and full implementation (Tasks 2-9)

- Spec: `.kiro/specs/libby-mcp/` · tasks `2`, `3`, `4`, `5`, `6`, `7`, `8`, `9`
- [x] Complete · PR: #3

### 3. AWS infrastructure (CDK) and integration tests

- Spec: `.kiro/specs/libby-mcp/` · tasks `10`, `11`, `12`
- [x] Complete · PR: #4

### 4. FastMCP migration: backend (PR A)

- Spec: `.kiro/specs/mcp-protocol-upgrade/` · tasks `1`, `2`, `3`, `4`, `5`, `6`
- [x] Complete · PR: #6