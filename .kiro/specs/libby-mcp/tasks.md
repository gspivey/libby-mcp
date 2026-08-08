# Tasks: Libby MCP Server

## Workflow Policy

**Red-Commit / Green-PR Rule:** Tests-first and implementation land in the SAME PR. Red (failing tests committed before implementation) is allowed at commit granularity, never at merge. Every PR must be green before merge — CI enforces this via required status checks.

**Tasks 2-9 are ONE PR.** The TDD cycle within this PR:
1. Write tests (Task 2) — committed as red (collection errors expected because implementation modules don't exist yet).
2. Implement modules (Tasks 3-9) — tests go green incrementally.
3. All tests pass before the PR merges.

To prevent pytest collection errors while implementation modules are missing, test files MUST use `pytest.importorskip()` for not-yet-implemented modules:
```python
# Example: tests/test_thunder_client.py
thunder_client = pytest.importorskip("src.thunder_client", reason="Implementation not yet written (TDD red phase)")
```
This allows `pytest --collect-only` to succeed even before implementation lands, preventing CI red on intermediate commits.

---

## Task 0: Clone and Inventory Template Repository

**Requirements:** All (foundation), NFR-7

**Description:** Clone the `GerardsCuriousTech/agent-router-template` repository as the project base. Inventory what the template provides (Lambda handler skeleton, API Gateway config, SAM template, auth patterns, deploy scripts, GitHub Actions workflow, test harness, AGENTS.md structure) so subsequent tasks extend it rather than building greenfield. Verify harness engineering foundations are present.

**Steps:**
1. Clone `GerardsCuriousTech/agent-router-template` into the `libby-mcp` repo.
2. Inventory the template structure: identify provided files for Lambda entry point, SAM/CloudFormation template, API Gateway configuration, authentication patterns, deployment scripts, and CI/CD configuration.
3. **Locate spec files for later copying (Task 1, step 8):**
   Spec files live at `.kiro/specs/libby-mcp/` within the brief repo. Contents:
   - `requirements.md`, `design.md`, `tasks.md` — the spec documents
   - `validate_spec.py` — the validation script
   - `context/` — reference material (Thunder API docs, etc.)
   These will be copied into the implementation repo by Task 1.
4. **Verify harness engineering foundations in template:**
   - GitHub Actions workflow exists (`.github/workflows/ci.yml` or similar)
   - Test harness structure exists (`tests/`, `conftest.py`, fixtures pattern)
   - `AGENTS.md` structure at repo root (or skeleton to extend)
   - Pre-commit hook configuration (`.pre-commit-config.yaml` or equivalent)
   - Makefile/justfile with standard targets (`test`, `lint`, `ci`)
5. Document what the template provides vs. what needs to be added/modified for this project (in `TEMPLATE_INVENTORY.md`). Include a section on harness engineering gaps.
6. Identify any template conventions (file naming, directory layout, config patterns) that subsequent tasks must follow.
7. Remove or rename any template placeholder code that conflicts with the Libby MCP implementation.
8. Inventory what's provided vs what needs extension for NFR-7 compliance:
   - CI workflow: provided? Needs extension for ruff/mypy/pytest jobs?
   - Test harness: provided? Needs pytest fixtures for Thunder API mocking?
   - AGENTS.md: provided? Needs Libby-specific content?
   - Linter config: provided? Needs ruff rules for architecture constraints?

**Acceptance Criteria:**
- Repository is initialized from `GerardsCuriousTech/agent-router-template`
- Template-provided infrastructure (Lambda, API Gateway, SAM, deploy) is identified and documented
- Harness engineering components inventoried: CI workflow, test harness, AGENTS.md, linter config, pre-commit hooks
- Gaps between template and NFR-7 requirements are documented (what needs to be added in Tasks 1, 1a, 1b)
- Subsequent tasks extend the template rather than creating conflicting greenfield files
- Template placeholder code is cleaned up without breaking the template's infrastructure patterns


---

## Task 1: Project Scaffolding and Dependencies

**Requirements:** FR-6, NFR-1, NFR-2, NFR-7

**Description:** Set up the project structure, Python packaging, dependencies, and test infrastructure. Extend the directory layout provided by `agent-router-template` (from Task 0) — do not duplicate infrastructure files the template already provides. Establish the test-driven foundation with pytest fixtures, test utilities, and mock Thunder API responses. Copy spec and context files into the repo so all validation tooling can reference them without external paths.

**Steps:**
1. Extend the project directory structure from the template to match design.md (add `src/tools/`, `tests/`, `tests/fixtures/`, `tests/integration/`, `scripts/`, `docs/design-docs/`, `docs/references/` alongside any template-provided directories).
2. Create `pyproject.toml` with project metadata, Python 3.12 requirement, and dependencies (`httpx`, `pytest`). Include ruff and mypy configuration:
   - ruff: import sorting, no wildcard imports, max line length 100, no unused imports/variables, ANN201 (require return type annotations on public functions)
   - mypy: strict mode for `src/`
   - pytest markers registration:
     ```toml
     [tool.pytest.ini_options]
     markers = ["integration: marks tests that call real Thunder API (deselect with '-m not integration')"]
     ```
3. Create `requirements.txt` with pinned versions of runtime dependencies for Lambda packaging.
4. Create `requirements-dev.txt` with pinned versions of dev dependencies: `pytest`, `pytest-cov`, `ruff`, `mypy`, `pre-commit`, `httpx` (for test client).
5. Create `src/__init__.py` and `src/tools/__init__.py` package files.
6. Create `tests/__init__.py` and `tests/integration/__init__.py`.
7. Create `tests/integration/test_placeholder.py` as a placeholder so `make integration` doesn't fail before Task 11:
    ```python
    """Placeholder so pytest collection doesn't fail on empty integration dir."""
    import os
    import pytest

    pytestmark = [
        pytest.mark.integration,
        pytest.mark.skipif(
            not os.environ.get("RUN_INTEGRATION"),
            reason="requires RUN_INTEGRATION=1",
        ),
    ]

    def test_integration_placeholder() -> None:
        """Replaced by real integration tests in Task 11."""
        pytest.skip("No live integration tests implemented yet")
    ```
7. Add a `.gitignore` for Python/Lambda artifacts (merge with any template-provided .gitignore).
8. **Copy spec and context files into the repo:**
   - Source location: `.kiro/specs/libby-mcp/` (this is where Kiro spec mode stores them; it is the source of truth for the spec).
   - Copy `validate_spec.py` → `scripts/validate_spec.py`
   - Copy spec files (requirements.md, design.md, tasks.md) → `docs/design-docs/`
   - Copy `context/` → `docs/references/`
   - Note: The copies in `docs/design-docs/` are the working copies that validation tooling and agents reference at runtime. The `.kiro/specs/` originals are the source of truth for Kiro spec mode only and should not be referenced by scripts or CI.
9. **Create test infrastructure (NFR-7.1):**
   - `tests/conftest.py` with shared pytest fixtures:
     - `mock_thunder_client` — a mocked ThunderClient that returns fixture data
     - `sample_search_response` — loads `tests/fixtures/search_response.json`
     - `sample_availability_response` — loads `tests/fixtures/availability_response.json`
     - `sample_error_responses` — loads `tests/fixtures/error_responses.json`
   - `tests/fixtures/search_response.json` — realistic Thunder API search response (2-3 items with all fields)
   - `tests/fixtures/availability_response.json` — realistic availability response envelope
   - `tests/fixtures/error_responses.json` — collection of error scenarios (timeout, 500, bad slug)

10. **Create Makefile with standard targets:**
    - `make test` — runs `pytest tests/ --ignore=tests/integration --cov=src --cov-report=term-missing --cov-fail-under=80` (handles exit code 5 via trivial smoke test — see step 12)
    - `make lint` — runs `ruff check src/ tests/` and `ruff format --check src/ tests/`
    - `make typecheck` — runs `mypy src/`
    - `make ci` — runs lint + typecheck + test (mirrors CI pipeline locally)
    - `make format` — runs `ruff format src/ tests/`
    - `make validate` — runs `python scripts/validate_spec.py`
    - `make integration` — runs `RUN_INTEGRATION=1 pytest tests/integration/ -v -m integration`
11. **Create AGENTS.md** at repo root with:
    - Project overview (one paragraph)
    - How to build/test/deploy (commands only)
    - Directory layout guide (what's where)
    - Links to spec files in `docs/design-docs/`
    - Validation commands: `make validate`, `make compliance`, `make preflight`
    - How to interpret validation output
    - Workflow: write tests → run tests (fail) → implement → run tests (pass) → `make preflight` → commit
12. **Create a trivial smoke test** at `tests/test_smoke.py`:
    ```python
    """Smoke test to prevent pytest exit code 5 (no tests collected)."""
    def test_smoke():
        assert True, "Smoke test — project is importable"
    ```
    This ensures `make test` exits 0 even before implementation tests are written.
12. Create `cdk.json` placeholder in repo root (actual CDK config lives in `infra/`).

**Acceptance Criteria:**
- Project installs cleanly with `pip install -e .`
- Directory structure matches design.md layout
- All package imports resolve without errors
- `make test` runs successfully and exits 0 (smoke test passes)
- `make lint` runs successfully against empty/skeleton source files
- `make ci` mirrors the CI pipeline locally
- Test fixtures contain realistic Thunder API response shapes matching the OpenAPI spec
- `tests/conftest.py` provides reusable fixtures for all test files
- `AGENTS.md` exists and provides sufficient context for an agent to navigate the repo
- Dev dependencies (ruff, mypy, pytest) install cleanly from `requirements-dev.txt`
- Spec files are copied to `docs/design-docs/` and context to `docs/references/`
- `scripts/validate_spec.py` exists and is runnable from the repo root
- `TEMPLATE_INVENTORY.md` exists documenting template vs custom files


---

## Task 1a: CI/CD Pipeline

**Requirements:** NFR-7.2, NFR-7.3

**Description:** Create the GitHub Actions CI/CD workflow that runs on every PR and provides automated feedback. This is the agent's primary feedback loop — CI status tells the agent whether its changes are correct without manual intervention. **This task runs immediately after scaffolding so that all subsequent implementation tasks (3-9) have CI feedback from the start.**

**Steps:**
1. Extend or create `.github/workflows/ci.yml` with the following jobs:
   - **lint:** Run `ruff check src/ tests/` and `ruff format --check src/ tests/`. Fail-fast on style violations.
   - **typecheck:** Run `mypy src/` with strict mode. Depends on lint passing.
   - **test:** Run `pytest tests/ --ignore=tests/integration -v --tb=short --cov=src --cov-report=term-missing --cov-fail-under=80`. Depends on typecheck passing.
   - **integration:** Run `pytest tests/integration/ -v --tb=short -m integration`. Triggered ONLY on schedule (daily cron) or manual dispatch — NOT on every PR. Gated with `RUN_INTEGRATION=1` environment variable (see HIGH-4 fix).
2. Configure the workflow trigger:
   ```yaml
   on:
     push:
       branches: [main]
     pull_request:
       branches: [main]
     schedule:
       - cron: '0 6 * * *'  # Daily at 6 AM UTC for integration tests
     workflow_dispatch:       # Manual trigger
   ```
3. Set up Python environment in CI:
   - Use `actions/setup-python@v5` with Python 3.12
   - Cache pip dependencies for speed
   - Install from `requirements.txt` and `requirements-dev.txt`
4. **Integration test job (scheduled only):**
   - Runs `pytest tests/integration/ -v -m integration`
   - Sets environment variable `RUN_INTEGRATION=1`
   - Uses a longer timeout (5 minutes) to accommodate Thunder API latency
   - Runs only when `github.event_name == 'schedule'` or `github.event_name == 'workflow_dispatch'`
   - Marks as non-blocking for PRs (only runs on schedule)
5. **Coverage threshold:** The test job enforces `--cov-fail-under=80` (80% coverage minimum).

6. **PR comment job (separate, runs on failure too):**
   ```yaml
   comment:
     runs-on: ubuntu-latest
     if: always() && github.event_name == 'pull_request'
     needs: [lint, typecheck, test]
     permissions:
       contents: read
       pull-requests: write
     steps:
       - uses: actions/github-script@v7
         with:
           script: |
             // Post structured results: pass/fail per job, test counts, failure details with remediation hints
   ```
   This job runs even when upstream jobs fail (`if: always()`), has explicit permissions for PR comments, and depends on all check jobs so it can report their status.
7. **Branch protection rules** (documented, applied manually):
   - Require status checks to pass: `lint`, `typecheck`, `test`
   - Require PR reviews (optional for solo dev, but documented)
   - No direct pushes to main

**Acceptance Criteria:**
- `.github/workflows/ci.yml` exists and is valid YAML
- Workflow runs successfully on PR creation (lint → typecheck → test)
- Integration tests run only on schedule or manual dispatch with `RUN_INTEGRATION=1` (not blocking PRs)
- PR gets a comment with structured results even on failure (separate job with `if: always()`)
- PR comment job has explicit `permissions: {contents: read, pull-requests: write}`
- agent-router can query CI status via GitHub API (`gh pr checks`)
- Lint failures produce actionable output (file, line, rule, fix suggestion)
- Test failures show the failing assertion with context (not just "FAILED")
- CI mirrors what `make ci` runs locally (no surprises between local and CI)
- Coverage threshold of 80% is enforced (`--cov-fail-under=80`)
- Python 3.12 is used consistently
- Dependencies are cached for fast CI runs (< 2 minutes for lint+typecheck+test)


---

## Task 1b: Agent Feedback Harness

**Requirements:** NFR-7.1, NFR-7.3, NFR-7.4

**Description:** Create the tooling that enables the agent to validate its own work locally, detect spec drift, and get actionable feedback on failures. This is the "inner loop" complement to CI (Task 1a) — the agent runs these tools before pushing, catching issues early. **This task runs immediately after CI so the feedback harness is available for all implementation tasks (3-9).**

**Steps:**
1. **Install `scripts/validate_spec.py`** (parameterized, copied from spec in Task 1):
   - Accept `--spec-dir` CLI argument or `SPEC_DIR` environment variable (defaults to `docs/design-docs/`)
   - Add check: every tool in requirements.md has a corresponding test file
   - Add check: every testable acceptance criterion (AC-*) has at least one test that references it (via comment or test name). See exempt-list below for non-unit-testable ACs.
   - Add check: project structure matches design.md layout (directories exist, key files present)
   - Add check: no orphaned test files — **demoted to WARN level** (not ERROR). During TDD red phase, tests exist before implementation; this is expected and must not block.
   - Output format: structured JSON with `{check, status, message, remediation}` per check
   - Exit codes: **0=PASS or WARN (non-blocking)**, 2=ERROR (blocking drift detected). Exit code 1 is NEVER used by validate_spec.py (reserved for check_compliance.py failures to avoid collision in `make preflight`).
2. **Create `scripts/check_compliance.py`:**
   - Validates implementation against acceptance criteria mechanically:
     - Imports each module and checks expected classes/functions exist
     - Validates MCP tool definitions match the schema in design.md (tool names, required params)
     - Checks that error handling patterns match the error table in design.md
   - **Scoped to modules that already exist under `src/`** — does not fail on modules not yet implemented (supports incremental TDD workflow)
   - Run as: `python scripts/check_compliance.py`
   - Output: per-criterion pass/fail with remediation hints
   - Exit codes: 0=all pass, 1=failures found (with details)

2a. **Add Makefile targets for compliance and preflight** (extending the Makefile from Task 1):
    - `make compliance` — runs `python scripts/check_compliance.py`
    - `make preflight` — runs validate + compliance + ci (full pre-push check)

3. **Non-unit-testable AC exempt list:**
   The following ACs cannot be verified by unit tests and are exempt from the "every AC has a test" check:
   - AC-4.4: Known working slugs (requires live API verification)
   - AC-5.3: API key configured in `~/.claude/mcp.json` (client-side config)
   - AC-6.7: Full Streamable HTTP deferred (negative scope — nothing to test)
   - AC-1.17: Results match Libby app (requires manual validation against live app)
   These are validated via integration tests (Task 11) or manual acceptance testing instead.
4. **Create `.pre-commit-config.yaml`:**
   ```yaml
   exclude: ^docs/references/
   repos:
     - repo: https://github.com/astral-sh/ruff-pre-commit
       rev: v0.5.0  # pin to specific version
       hooks:
         - id: ruff
           args: [--fix]
         - id: ruff-format
     - repo: local
       hooks:
         - id: typecheck
           name: mypy
           entry: mypy src/
           language: system
           pass_filenames: false
           types: [python]
   ```
   Note: The top-level `exclude: ^docs/references/` prevents pre-commit from linting vendored context files copied from the spec (which may contain lint violations like E401, ANN201).
5. **Write error messages for agent consumption:**
   - Every assertion in tests includes a message explaining WHAT went wrong and HOW to fix it
   - Example: `assert "library_slug" in params, "REMEDIATION: search_titles must validate library_slug is present — add validation at top of handle()"`
   - Every lint rule in ruff config includes a comment explaining why it exists
   - Script failures include the file path and line number to fix
6. **Create `docs/references/` directory** with:
   - `thunder_api_reference.md` — key endpoints, response shapes, known quirks (extracted from context files)
   - `mcp_spec_excerpt.md` — relevant MCP 2025-06-18 protocol details (Streamable HTTP, tool result format, error codes)
   - These serve as the agent's domain reference — it can look things up without external searches
7. **Update `AGENTS.md`** with:
   - Validation commands: `make validate`, `make compliance`, `make preflight`
   - How to interpret validation output
   - Link to `docs/references/` for domain context
   - Workflow: write tests → run tests (fail) → implement → run tests (pass) → `make preflight` → commit

**Acceptance Criteria:**
- `python scripts/validate_spec.py` runs successfully and validates spec-to-code alignment
- `validate_spec.py` accepts `--spec-dir` or `SPEC_DIR` env var (no hardcoded paths)
- `validate_spec.py` returns exit 0 for both PASS and WARN; exit 2 for ERROR only
- Orphaned test files are reported as WARN (not ERROR) — supports TDD red phase
- `python scripts/check_compliance.py` runs and reports per-criterion pass/fail
- `check_compliance.py` only checks modules that exist under `src/` (no failure on unimplemented modules)
- Non-unit-testable ACs (AC-4.4, AC-5.3, AC-6.7, AC-1.17) are on the exempt list
- Pre-commit hooks run ruff lint+format and mypy on every commit
- `make preflight` runs the full validation suite (validate + compliance + lint + typecheck + test)
- Error messages include remediation hints (not just "assertion failed")
- `docs/references/` contains Thunder API and MCP spec reference documents
- `AGENTS.md` documents the full agent workflow including validation commands
- An agent can clone the repo, read `AGENTS.md`, and know exactly how to validate its work
- The feedback harness catches common issues BEFORE CI runs (faster iteration loop)


---

## Task 2: Unit Test Specifications (TDD Foundation)

**Requirements:** All FR, NFR-6, NFR-7

**Description:** Write the complete unit test suite BEFORE implementation. This task defines all test files that verify the acceptance criteria mechanically. Per the Red-Commit/Green-PR policy, these tests are committed in a red (failing) state and go green as Tasks 3-9 implement the corresponding modules. Each implementation task (3-9) references the test file written here.

**TDD Contract:** Tests committed here define the interface contract. Implementation in Tasks 3-9 MUST satisfy these tests without modifying them (except to fix genuine test bugs).

**Steps:**
1. Create `tests/test_thunder_client.py`:
   - Test URL construction for various library slugs and parameter combinations
   - Test repeated `subject` params generate correct query string (`subject=24&subject=80`)
   - Test format mapping in URL (`audiobook-overdrive`, `ebook-overdrive`)
   - Test `get_availability` URL construction: `/v2/libraries/{slug}/media/availability?titleIds=12345&titleIds=67890`
   - Test timeout handling (mock timeout at 10s, verify graceful error)
   - Test HTTP error wrapping (mock 500, verify structured error)
   - Test User-Agent header is set
2. Create `tests/test_auth.py`:
   - Test valid Bearer token passes
   - Test invalid token fails
   - Test missing Authorization header fails
   - Test malformed header (no "Bearer " prefix) fails
3. Create `tests/test_mcp_server.py`:
   - Test `initialize` returns `{protocolVersion: "2025-06-18", capabilities: {tools: {}}, serverInfo: {name: "libby-mcp", version: "1.0.0"}}`
   - Test `ping` returns empty result `{}` (not -32601 error)
   - Test tools/list returns all three tool definitions
   - Test tools/call dispatches to correct handler
   - Test tool results are wrapped in `{"content": [{"type": "text", "text": "<json>"}]}` format
   - Test tool-execution errors include `isError: true` in result (not JSON-RPC error codes)
   - Test `notifications/initialized` is handled without error (no response)
   - Test unknown method returns JSON-RPC error -32601 (protocol fault)
   - Test malformed request returns JSON-RPC error -32600 (protocol fault)
   - Test invalid tool params return `isError: true` result (not JSON-RPC error)

4. Create `tests/test_search_titles.py`:
   - Test parameter mapping: `subjects=[24,80]` → `subject=24&subject=80` (accepts strings or integers, coerced to string)
   - Test format mapping: `audiobook` → `audiobook-overdrive`
   - Test `available=true` → `showOnlyAvailable=true`; omitted → `availableFirst=true`
   - Test client-side BISAC filtering (keep items with matching bisacCodes)
   - Test client-side duration filtering (parse "12:34:56" → 12.6h, filter by under_hours; items with no duration are INCLUDED)
   - Test response normalization: Title Object schema matches design.md (id is string)
   - Test availability computation: `isAvailable AND availableCopies >= 1`
   - Test deep link format: `https://{slug}.overdrive.com/media/{id}`
   - Test pagination metadata from `links.last.page` (reflects server count before filtering)
   - Test `filtered_count` reflects post-client-filter count
   - Test `facets` included in response from Thunder API response
   - Test validation: missing library_slug returns error
   - Test invalid slug detection: empty items + totalItems=0 with no filters → error (best-effort heuristic)
5. Create `tests/test_get_availability.py`:
   - Test that both library_slug and title_ids are required (missing → error)
   - Test that empty title_ids array returns error
   - Test that title_ids accepts both strings and integers, coerces to string internally
   - Test successful retrieval iterates `response["items"]` and returns copies_owned, copies_available, holds_count, estimated_wait_days per title
   - Test that the correct endpoint is called: `/v2/libraries/{slug}/media/availability?titleIds=...`
   - Test titles not found in `response["items"]` get error indicator
   - Test response does NOT contain recommendations, rankings, or verdicts
   - Test multiple title IDs all returned in single response
6. Create `tests/test_deep_link.py`:
   - Test URL format: `https://lcpl.overdrive.com/media/12345`
   - Test both params required (missing title_id or library_slug returns error)
7. Create `tests/test_handler.py`:
   - Test auth rejection: invalid key → 401
   - Test successful dispatch: valid key + valid MCP request → 200 with result
   - Test unhandled error: exception in tool → 500 with generic message

**Acceptance Criteria:**
- All test files exist and are importable
- Tests reference specific AC IDs in test names or comments (e.g., `test_search_format_mapping__ac_1_3`)
- Running `pytest` shows all tests FAILING (red phase — implementation not yet written)
- Tests are committed before any implementation code (verifiable in git history)
- Test files use fixtures from `tests/conftest.py` (no real API calls)
- Each test includes a remediation hint in its assertion message


---

## Task 3: Thunder API Client

**Requirements:** FR-1, FR-2, FR-4, NFR-1, NFR-3, NFR-4, NFR-6

**Description:** Implement the shared HTTP client for the OverDrive Thunder API. The Thunder API is public (no authentication), so this client only needs URL construction, proper headers, timeout handling, and error normalization.

**TDD:** Tests in `tests/test_thunder_client.py` (from Task 2) must pass after this task. Do NOT modify the tests.

**Steps:**
1. Create `src/thunder_client.py` with a `ThunderClient` class.
2. Implement base URL construction: `https://thunder.api.overdrive.com/v2/libraries/{slug}/media`.
3. Implement `search(slug: str, params: dict) -> dict` method that:
   - Constructs the full URL with query parameters
   - Maps repeatable params correctly (e.g., multiple `subject` values → `subject=24&subject=80`)
   - Sets `User-Agent: libby-mcp/1.0` header (matching libby.py pattern)
   - Makes the GET request with 10-second timeout (within Lambda's 30s budget)
   - Returns the parsed JSON response
4. Implement `get_availability(slug: str, title_ids: list[str]) -> dict` method that:
   - Calls `GET /v2/libraries/{slug}/media/availability?titleIds={id1}&titleIds={id2}...`
   - Note: this is the library-scoped availability endpoint that returns an envelope `{items: [AvailabilityItem, ...]}`
   - NOT `/v2/media/bulk` which only returns GlobalMediaResponse
   - Sets the same User-Agent header and 10-second timeout
   - Returns the parsed JSON response (caller iterates `response["items"]`)
5. Implement error handling:
   - Timeouts → return structured error dict with message "Library catalog temporarily unavailable"
   - HTTP 4xx/5xx → catch, wrap with status code and Thunder API error body if available
   - JSON parse errors → wrap gracefully
6. Add structured logging for request URL (sans any sensitive data), duration, and response status.

**Acceptance Criteria:**
- `tests/test_thunder_client.py` passes (all green)
- `ThunderClient` constructs correct URLs for any library slug (AC-4.1, AC-4.2)
- `get_availability` constructs correct URL with library-scoped availability endpoint
- Requests include `User-Agent: libby-mcp/1.0` header (NFR-3.2)
- Requests time out after 10 seconds (NFR-6.2)
- Thunder API errors are caught and returned as structured error objects (NFR-6.1, NFR-6.3)
- No auth headers sent — Thunder API is public (AC-5.4)
- Tests committed before implementation (verifiable in git history)


---

## Task 4: Authentication Module

**Requirements:** FR-5, NFR-5

**Description:** Implement MCP API key validation for incoming requests. This validates the key that MCP clients use to authenticate to OUR server — not a Thunder API key (Thunder is public).

**TDD:** Tests in `tests/test_auth.py` (from Task 2) must pass after this task. Do NOT modify the tests.

**Steps:**
1. Create `src/auth.py` with a `validate_api_key(headers: dict) -> bool` function.
2. Read the expected API key from `os.environ["LIBBY_MCP_API_KEY"]`.
3. Extract the Bearer token from the `Authorization` header.
4. Compare using constant-time comparison (`hmac.compare_digest`).
5. Return `True` for valid keys, `False` for invalid/missing.
6. Never log the key value — only log "auth success" or "auth failed".

**Acceptance Criteria:**
- `tests/test_auth.py` passes (all green)
- Valid Bearer tokens pass validation (AC-5.1)
- Missing or incorrect tokens fail validation (AC-5.2)
- API key value is never logged or included in error responses (NFR-5.1)
- Uses constant-time comparison to prevent timing attacks
- Tests committed before implementation (verifiable in git history)

---

## Task 5: MCP Protocol Layer

**Requirements:** FR-6

**Description:** Implement the MCP 2025-06-18 Streamable HTTP protocol handler including tool discovery, invocation routing, and proper result formatting.

**TDD:** Tests in `tests/test_mcp_server.py` (from Task 2) must pass after this task. Do NOT modify the tests.

**Steps:**
1. Create `src/mcp_server.py` with an `MCPServer` class.
2. Implement `handle_request(body: dict) -> dict` as the main JSON-RPC dispatcher.
3. Implement `handle_initialize(params)` returning InitializeResult: `{"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "libby-mcp", "version": "1.0.0"}}`.
4. Implement `handle_ping()` returning an empty result object `{}`.
5. Implement `handle_notifications_initialized()` as a no-op acknowledgment.
6. Implement `handle_tools_list()` returning the three tool definitions with schemas from design.md.
7. Implement `handle_tools_call(params)` that dispatches to the correct tool handler based on `params["name"]`.
8. Format tool results as MCP text content blocks: `{"content": [{"type": "text", "text": "<json-serialized-output>"}]}`. On tool-execution failures, add `"isError": true`.
9. Format all responses as valid MCP JSON-RPC (with `jsonrpc`, `id`, `result` or `error` fields).
10. Handle unknown methods with JSON-RPC method-not-found error (-32601).
11. Handle malformed requests with JSON-RPC invalid-request error (-32600).
12. For notifications (no `id` field in request), return HTTP 202 Accepted with an empty body.

**Acceptance Criteria:**
- `tests/test_mcp_server.py` passes (all green)
- `initialize` returns correct InitializeResult (AC-6.8)
- `ping` returns empty result `{}` — NOT -32601 (AC-6.9)
- `tools/list` returns all three tool definitions with correct input schemas (AC-6.2)
- `tools/call` dispatches to the correct handler and returns results (AC-6.3)
- Tool results formatted as `{"content": [{"type": "text", "text": "<json>"}]}` (AC-6.5)
- All responses are valid JSON-RPC 2.0 (AC-6.4)
- Tests committed before implementation (verifiable in git history)


---

## Task 6: search_titles Tool Implementation

**Requirements:** FR-1, FR-4

**Description:** Implement the `search_titles` MCP tool. This builds Thunder API query params, fetches results, applies client-side filters (bisac, duration), and normalizes the response.

**TDD:** Tests in `tests/test_search_titles.py` (from Task 2) must pass after this task. Do NOT modify the tests.

**Steps:**
1. Create `src/tools/search_titles.py` with a `handle(params: dict, client: ThunderClient) -> dict` function.
2. Validate `library_slug` is present; return tool error (`isError: true`) if missing.
3. Build Thunder API query params from MCP input using the mapping table:
   - `subjects` → repeated `subject` params (coerced to string)
   - `format` → `format` with value transform (`audiobook` → `audiobook-overdrive`, `ebook` → `ebook-overdrive`)
   - `available=true` → `showOnlyAvailable=true`; `available=false/omitted` → `availableFirst=true`
   - `creator` → `creator` (direct)
   - `bisac` → `bisacCode` (send server-side — belt-and-suspenders)
   - `series_id` → `seriesId` (direct)
   - `maturity_level` → `maturityLevel` (`general` → `generalcontent`, others direct)
   - `sort_by` → `sortBy` (direct)
   - `query` → `query` (direct)
   - `per_page` → `perPage` (clamp to max 100, default 24)
   - `page` → `page` (default 1)
4. Call `ThunderClient.search()` with the constructed parameters.
5. **Invalid slug detection (best-effort):** If Thunder returns HTTP 200 but `response["items"]` is empty AND `response["totalItems"]` is 0, and no narrowing filters were applied, return a tool error.
6. Apply client-side filters on the response `items` array:
   - `bisac`: keep only items where `bisacCodes[]` contains the code
   - `under_hours`: parse duration → hours; keep items where hours <= `under_hours`; items with no duration are INCLUDED
7. Normalize each item into a Title Object (per design.md schema).
8. Return search response: `{ titles, total_items, page, total_pages, filtered_count, facets }`.

**Acceptance Criteria:**
- `tests/test_search_titles.py` passes (all green)
- All parameters from FR-1 are accepted and correctly mapped (AC-1.1 through AC-1.18)
- BISAC filtering uses belt-and-suspenders (server + client) (AC-1.6)
- Duration filtering applied client-side; items without duration INCLUDED (AC-1.8)
- Response includes pagination metadata and `filtered_count` (AC-1.15)
- Response includes `facets` with subjects (AC-1.18)
- Tests committed before implementation (verifiable in git history)


---

## Task 7: get_availability Tool Implementation

**Requirements:** FR-2, FR-4

**Description:** Implement the `get_availability` MCP tool. This fetches raw availability data for specific titles via the Thunder API library-scoped bulk availability endpoint.

**TDD:** Tests in `tests/test_get_availability.py` (from Task 2) must pass after this task. Do NOT modify the tests.

**Steps:**
1. Create `src/tools/get_availability.py` with a `handle(params: dict, client: ThunderClient) -> dict` function.
2. Validate `library_slug` and `title_ids` are present; return tool error if missing.
3. Validate `title_ids` is a non-empty array; coerce all elements to strings.
4. Call `ThunderClient.get_availability(slug, title_ids)`.
5. Iterate `response["items"]` to extract per-title availability fields.
6. For title IDs not found in response, include an entry with `error: "not_found"`.
7. Return response: `{ library_slug, titles: [...] }`.

**Acceptance Criteria:**
- `tests/test_get_availability.py` passes (all green)
- Uses the correct endpoint: `/v2/libraries/{slug}/media/availability?titleIds=...` (AC-2.3)
- Does NOT use `/v2/media/bulk`
- Returns raw availability data per title (AC-2.4)
- Does NOT include recommendations or rankings (AC-2.5)
- Handles titles not found gracefully (AC-2.6)
- Tests committed before implementation (verifiable in git history)

---

## Task 8: get_deep_link Tool Implementation

**Requirements:** FR-3

**Description:** Implement the `get_deep_link` MCP tool. Simple URL construction — generates collision-proof links using the numeric title ID.

**TDD:** Tests in `tests/test_deep_link.py` (from Task 2) must pass after this task. Do NOT modify the tests.

**Steps:**
1. Create `src/tools/deep_link.py` with a `handle(params: dict) -> dict` function.
2. Validate both `title_id` and `library_slug` are present; return tool error if missing.
3. Construct the URL: `https://{library_slug}.overdrive.com/media/{title_id}`.
4. Return the Deep Link Object: `{ url, title_id, library_slug }`.

**Acceptance Criteria:**
- `tests/test_deep_link.py` passes (all green)
- Both required parameters are validated (AC-3.1, AC-3.2)
- Returns URL in format `https://{slug}.overdrive.com/media/{id}` (AC-3.3)
- Missing parameters return clear validation errors (AC-3.4)
- Tests committed before implementation (verifiable in git history)

---

## Task 9: Lambda Handler and Request Routing

**Requirements:** FR-5, FR-6, NFR-1, NFR-4

**Description:** Implement the Lambda entry point that ties together auth, MCP protocol handling, and tool dispatch.

**TDD:** Tests in `tests/test_handler.py` (from Task 2) must pass after this task. Do NOT modify the tests.

**Steps:**
1. Create `src/handler.py` with a `lambda_handler(event, context)` function.
2. Parse the HTTP request body from the API Gateway event.
3. Call `auth.validate_api_key()` on the request headers; return 401 if invalid.
4. Instantiate `ThunderClient` at **module level** (reused across warm invocations).
5. Instantiate `MCPServer` with the Thunder client reference.
6. Pass the request body to `mcp_server.handle_request()`.
7. Return HTTP response based on request type:
   - JSON-RPC request (has `id`): return HTTP 200 with response body
   - Notifications only (no `id`): return HTTP 202 Accepted with empty body
   - GET/DELETE: return HTTP 404 at API Gateway level
8. Wrap in try/except for unhandled errors — log full traceback, return HTTP 500 with generic message.
9. Add structured logging: tool name, duration, success/failure.

**Acceptance Criteria:**
- `tests/test_handler.py` passes (all green)
- Auth is checked before any tool processing (AC-5.2)
- Valid JSON-RPC requests return HTTP 200 (AC-6.6)
- Notification-only POSTs return HTTP 202 Accepted (AC-6.6)
- Module-level ThunderClient reuse enables warm invocation performance (NFR-1.2)
- Unhandled errors return 500 without leaking internals (NFR-6.1)
- Tests committed before implementation (verifiable in git history)


---

## Task 10: AWS Infrastructure (CDK Stack)

**Requirements:** NFR-1, NFR-2, NFR-4, NFR-5, NFR-8

**Description:** Create the AWS CDK stack (TypeScript) defining the Lambda function, API Gateway, and supporting resources for the Libby MCP server.

**Steps:**
1. Initialize CDK app in `infra/`:
   ```bash
   mkdir -p infra && cd infra
   npx cdk init app --language typescript
   ```
2. Install CDK constructs for Lambda, API Gateway, Secrets Manager:
   ```bash
   npm install @aws-cdk/aws-lambda @aws-cdk/aws-apigatewayv2 @aws-cdk/aws-secretsmanager
   ```
3. Create `lib/libby-mcp-stack.ts` with:
   - Lambda function: Python 3.12, ARM64, 256MB memory, 30s timeout
   - Handler: `src/handler.handler`
   - Code asset pointing to `../` (the Python source)
   - API Gateway HTTP API with `POST /mcp` route integrated to Lambda
   - Secrets Manager secret using `generateSecretString()` for API key
   - Lambda environment variable `LIBBY_MCP_API_KEY` from secret
   - IAM role with CloudWatch Logs write + Secrets Manager read permissions
   - CloudWatch log group with 14-day retention
4. Configure API Gateway throttling: 10 req/s burst, 5 req/s sustained.
5. Add CDK outputs:
   - `ApiEndpoint`: The API Gateway URL
   - `SecretArn`: The Secrets Manager secret ARN
6. Create `scripts/deploy.sh`:
   ```bash
   #!/bin/bash
   set -e
   cd "$(dirname "$0")/../infra"
   npm install
   npx cdk deploy --require-approval never --outputs-file ../cdk-outputs.json
   echo "Deployed. API endpoint:"
   jq -r '.LibbyMcpStack.ApiEndpoint' ../cdk-outputs.json
   ```
7. Create `scripts/get-api-key.sh`:
   ```bash
   #!/bin/bash
   SECRET_ARN=$(jq -r '.LibbyMcpStack.SecretArn' cdk-outputs.json)
   aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text
   ```
8. Create `scripts/destroy.sh`:
   ```bash
   #!/bin/bash
   cd "$(dirname "$0")/../infra"
   npx cdk destroy --force
   ```

**Acceptance Criteria:**
- `cd infra && cdk synth` produces valid CloudFormation template
- `cdk deploy` succeeds from clean checkout (NFR-8.2)
- `cdk destroy` removes all resources without orphans (NFR-8.3)
- Lambda cold start < 3s (NFR-1.1)
- IAM role follows least privilege (NFR-5.3)
- API Gateway throttling configured (NFR-3.3)
- Secrets Manager secret auto-generates API key (NFR-5.2)
- Deploy/destroy scripts work from repo root

---

## Task 11: Integration Testing and Documentation

**Requirements:** All FR, NFR-1, NFR-7.3

**Description:** Create integration test scripts and project documentation. Integration tests are gated with `@pytest.mark.integration` and only run when `RUN_INTEGRATION=1` is set.

**Steps:**
1. Create `tests/integration/test_live.py` with integration tests marked `@pytest.mark.integration`:
   ```python
   import os
   import pytest
   
   pytestmark = [
       pytest.mark.integration,
       pytest.mark.skipif(
           not os.environ.get("RUN_INTEGRATION"),
           reason="requires RUN_INTEGRATION=1",
       ),
   ]
   ```
   - Verify Thunder API search returns valid JSON with expected structure
   - Verify response has `items[]` with expected fields
   - Verify `links.last.page` present for pagination
   - Tests skip automatically unless `RUN_INTEGRATION=1` environment variable is set
2. Add pytest marker configuration in `pyproject.toml`:
   ```toml
   [tool.pytest.ini_options]
   markers = ["integration: marks tests that call real Thunder API (deselect with '-m not integration')"]
   ```
3. Create `scripts/test_mcp.sh` for manual testing against deployed endpoint.
4. Write `README.md` with:
   - Project overview and what the tools do
   - Setup instructions (Python 3.12, pip install)
   - Local testing (`pytest`)
   - Deployment steps (`cd infra && cdk deploy`)
   - MCP client configuration example for `~/.claude/mcp.json`
5. Add example MCP tool invocations in the README.

**Acceptance Criteria:**
- Integration tests are gated with `@pytest.mark.integration` AND `RUN_INTEGRATION=1` env var
- Integration tests do NOT run during normal `make test` or PR CI (NFR-7.3 + Task 1a scheduled job)
- Task 1a's scheduled CI job sets `RUN_INTEGRATION=1` to enable them daily
- Integration test validates Thunder API response structure against real endpoint
- README provides complete setup-to-deployment instructions
- MCP client configuration example is copy-paste ready

---

## Task 12: Branch Protection and Final Documentation

**Requirements:** NFR-7.2

**Description:** Apply branch protection rules and finalize project documentation. This is the last task — all implementation and CI are already in place.

**Steps:**
1. Document branch protection rules to apply via GitHub UI or API:
   - Require status checks: `lint`, `typecheck`, `test`
   - Require PR reviews (optional for solo dev)
   - No direct pushes to main
2. Verify `make preflight` passes cleanly on the final state of all code.
3. Run `make ci` and confirm it mirrors CI output.
4. Final update to `AGENTS.md` with any last changes.
5. Update `TEMPLATE_INVENTORY.md` with final delta between template and implementation.

**Acceptance Criteria:**
- Branch protection rules are documented (and applied if repo access permits)
- `make preflight` passes: validate + compliance + lint + typecheck + test
- `make ci` passes cleanly
- All spec acceptance criteria are satisfied (verified by `scripts/check_compliance.py`)
- `AGENTS.md` is current and complete
