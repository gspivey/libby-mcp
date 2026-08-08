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
> revisions and 7 Claude opus-5 reviews.

### 1. Project scaffolding, CI, and test harness

Clone `agent-router-template`, extend the directory layout for a Python Lambda MCP server,
and establish the harness engineering foundations: pytest fixtures for Thunder API mocking,
CI pipeline with lint/typecheck/test/integration jobs, pre-commit hooks, and the agent
feedback harness (`validate_spec.py`, `check_compliance.py`). Copy spec files into
`docs/design-docs/` so validation tooling can reference them. Creates the AGENTS.md
navigation document and Makefile with standard targets. This is the foundation — all
implementation PRs (item 2 onward) land on top of it.

- Spec: `.kiro/specs/libby-mcp/` · tasks `0`, `1`, `1a`, `1b`
- [ ] Complete · PR: —

---

### 2. TDD test specifications

Write all unit tests for Tasks 3-9 before any implementation code exists. Tests use
`pytest.importorskip()` to handle missing modules during the TDD red phase. Covers:
Thunder client tests (timeout, retries, response parsing), auth tests (API key header
injection, missing key errors), MCP protocol tests (JSON-RPC request/response, error
envelopes), and tool tests (search_titles, get_availability, get_deep_link). Tests are
committed as "red" (collection passes, assertions would fail), then go green as
implementation lands in items 3-5. Builds on the fixtures created in item 1.

- Spec: `.kiro/specs/libby-mcp/` · tasks `2`
- [ ] Complete · PR: —

---

### 3. Thunder client and authentication

Implement `ThunderClient` at `src/thunder_client.py` — the HTTP client for OverDrive's
Thunder API. Handles base URL configuration, request timeout (10s), response parsing, and
error normalization. Implement the auth module at `src/auth.py` — API key injection via
`x-api-key` header, validation of key presence, and error response for missing credentials.
Tests from item 2 should go green for these modules.

- Spec: `.kiro/specs/libby-mcp/` · tasks `3`, `4`
- [ ] Complete · PR: —

---

### 4. MCP protocol layer and search_titles tool

Implement the MCP JSON-RPC protocol layer at `src/mcp_protocol.py` — request validation,
response formatting, error envelope construction (`{error: {code, message, details?}}`).
Implement `search_titles` at `src/tools/search_titles.py` — the primary catalog search tool
with filters for subjects, format, availability, creator, BISAC, series, duration, maturity
level, and sort. Calls `/v2/libraries/{slug}/media` via the Thunder client. Returns titles
array plus facets. Builds on Thunder client from item 3.

- Spec: `.kiro/specs/libby-mcp/` · tasks `5`, `6`
- [ ] Complete · PR: —

---

### 5. get_availability, get_deep_link, and Lambda handler

Implement `get_availability` at `src/tools/get_availability.py` — returns raw availability
data (copies_owned, copies_available, holds_count, estimated_wait_days) for a list of title
IDs. Calls `/v2/libraries/{slug}/media/availability`. Implement `get_deep_link` at
`src/tools/get_deep_link.py` — returns the HTTPS URL to a title on OverDrive. Implement
the Lambda handler at `src/handler.py` — routes incoming MCP requests to the appropriate
tool, handles errors, returns MCP-formatted responses. All unit tests from item 2 should
now pass.

- Spec: `.kiro/specs/libby-mcp/` · tasks `7`, `8`, `9`
- [ ] Complete · PR: —

---

### 6. AWS infrastructure and integration tests

Create the CDK stack at `infra/lib/libby-mcp-stack.ts` — Lambda function (Python 3.11, ARM64), API
Gateway HTTP API with `x-api-key` auth, Secrets Manager for auto-generated API key. Add deploy/destroy
scripts. Create integration tests at `tests/integration/test_live.py` — tests against real Thunder
API (gated with `RUN_INTEGRATION=1`), validates response shapes match spec. Update documentation
and finalize branch protection rules. This item makes the server deployable.

- Spec: `.kiro/specs/libby-mcp/` · tasks `10`, `11`, `12`
- [ ] Complete · PR: —

---

## Completed

Items move here after they merge to `development`.

*(none yet)*
