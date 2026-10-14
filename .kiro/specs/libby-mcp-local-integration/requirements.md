# Requirements: Local Integration Test Harness (Docker + CI)

## Overview

libby-mcp currently has two test tiers:

1. **Unit tests** (`tests/`, excluding `tests/integration/`) — fully mocked
   against the Thunder API, run on every PR via the `test` CI job with an 80%
   coverage gate.
2. **Live integration tests** (`tests/integration/test_live.py`) — validate real
   Thunder API response shapes, gated behind `RUN_INTEGRATION=1`, and run only on
   schedule or manual dispatch (never on PRs) because they historically required
   cloud secrets to reach a deployed endpoint.

There is a gap: nothing exercises the full MCP protocol surface — the ASGI app,
`AuthMiddleware`, FastMCP tool registration, JSON-RPC request/response handling, and
the real Thunder API end to end — on every PR. The deployed-endpoint integration job
cannot run on PRs because it needs cloud secrets.

This feature adds a **local integration test harness**: a Docker image that runs the
ASGI app via uvicorn (no Lambda, no Mangum, no AWS), a docker-compose service so
developers can run the same thing locally, an MCP protocol test suite that drives the
containerized server over HTTP, and a new CI job (`local-integration`) that runs on
every PR with **no cloud dependency and no secrets**. The server authenticates with a
hardcoded test token (`ci-test-key`); the Thunder API (`api.overdrive.com`) is public,
read-only, and free, so tests hit it live.

## Reference

- #[[file:.kiro/specs/libby-mcp-local-integration/context/handler.py]] — ASGI app factory (`_build_app`) and the three registered tools
- #[[file:.kiro/specs/libby-mcp-local-integration/context/auth.py]] — `AuthMiddleware` (POST-only, Bearer token via `LIBBY_MCP_API_KEY`, GET → 405)
- #[[file:.kiro/specs/libby-mcp-local-integration/context/test_live.py]] — existing Thunder-shape integration test pattern (stays gated on secrets)
- #[[file:.kiro/specs/libby-mcp-local-integration/context/ci.yml]] — current CI workflow (the `integration` job is extended, not replaced)
- #[[file:.kiro/specs/libby-mcp-local-integration/context/Makefile]] — existing targets (`test`, `lint`, `integration`, etc.)
- #[[file:.kiro/specs/libby-mcp-local-integration/context/pyproject.toml]] — dependencies, pytest markers, asyncio config

## Design Decisions (Pre-Decided — Do Not Re-Derive)

These are fixed inputs to the spec. Encode them; do not re-litigate them.

- **DD-1:** The test server runs via `uvicorn src.handler:_build_app --factory`.
  `_build_app()` returns a plain ASGI app (`AuthMiddleware` wrapping the FastMCP
  Streamable HTTP app). No Mangum, no Lambda, no AWS needed to run it.
- **DD-2:** The Thunder API (`api.overdrive.com` / `thunder.api.overdrive.com`) is hit
  **live** in CI. No mocking or cassette recording. Catalog metadata (title IDs, slugs,
  names) is stable; only availability counts fluctuate, and tests avoid asserting on
  volatile counts.
- **DD-3:** The test token is `LIBBY_MCP_API_KEY=ci-test-key`, hardcoded in
  `Dockerfile.integration`. It is not a secret (grants access only to a public,
  read-only catalog proxy), so it can live in the image and the workflow.
- **DD-4:** Fixed test fixtures: title ID **Going Postal = 149795**; library slugs
  **lcpl** and **fairfax**. Tests use these fixed values rather than discovering them.
  The **`fairfax` slug is marked "needs verification before implementing"**: it has not
  been confirmed against the live Thunder catalog for this feature. Before implementing
  the multi-library fan-out test (AC-3.10), the developer MUST verify that `fairfax`
  resolves to a real library returning `totalItems > 0` by running a curl against the
  public Thunder search endpoint, e.g.:

  ```bash
  curl -s "https://thunder.api.overdrive.com/v2/libraries/fairfax/media?query=postal&perPage=1" \
    | python -c "import sys,json; print(json.load(sys.stdin).get('totalItems'))"
  # expect an integer > 0
  ```

  If `fairfax` does not resolve (404) or returns `totalItems == 0`, substitute a verified
  second slug (e.g., `nypl`) and update AC-3.10, the design test map, and Task 3
  accordingly before writing the test. The `lcpl` slug is already verified in prior
  items.
- **DD-5:** The new `local-integration` CI job runs on **every PR** (no secrets). The
  existing `integration` job (live deployed endpoint / Thunder-shape tests in
  `test_live.py`) **stays gated** on schedule/dispatch as it is today.
- **DD-6:** `get_deep_link` is **NOT present post-v2** (removed by FR-B3: AC-B3.1,
  AC-B3.4). After the v2 items 7+8 merge, only two tools remain. The protocol tests MUST
  cover exactly these two tools: `search_titles` and `get_availability`. **Dependency:**
  This feature is ROADMAP item 9 and depends on ROADMAP item 8 (libby-mcp-v2 Group C
  content enrichment) being merged, so the v2 enrichment fields exist in `search_titles`
  responses and the AC-3.7 assertions can cover them. Do not start item 9 until item 8 is
  complete.
- **DD-7:** Per `AuthMiddleware`, a `GET /mcp` returns **HTTP 405** (not 200, not 404)
  with an `Allow: POST` header. Tests MUST assert 405.

## Functional Requirements

### FR-1: Integration Test Docker Image

**Description:** Provide a `Dockerfile.integration` at the repo root that builds a
test-ready image running the ASGI app under uvicorn with no AWS or Lambda dependencies.

**Acceptance Criteria:**
- AC-1.1: A file named `Dockerfile.integration` exists at the repository root.
- AC-1.2: The image is based on an official Python 3.12 image (matching the project's
  `requires-python` and CI Python version).
- AC-1.3: The image installs runtime dependencies from `requirements.txt` plus
  `uvicorn` (and its standard extras) so the ASGI app can be served over HTTP.
- AC-1.4: The image copies the `src/` tree (and any files `src/` imports at runtime)
  into the image and installs the package (e.g., `pip install -e .` or copying + a
  `PYTHONPATH` that resolves `src.handler`).
- AC-1.5: The image sets `ENV LIBBY_MCP_API_KEY=ci-test-key` so `AuthMiddleware`
  accepts `Authorization: Bearer ci-test-key`.
- AC-1.6: The image's default command runs
  `uvicorn src.handler:_build_app --factory --host 0.0.0.0 --port 8000`.
- AC-1.7: The container exposes port 8000.
- AC-1.8: The image builds successfully on a standard Linux Docker runner (GitHub
  Actions `ubuntu-latest`) with no AWS credentials and no secrets.
- AC-1.9: A `.dockerignore` (existing or new) excludes `tests/`, `infra/`, `.git/`,
  and other non-runtime files from the build context so the image stays lean. The
  existing `.dockerignore` MUST NOT be broken by this change.

### FR-2: docker-compose Service for Local Runs

**Description:** Provide a `docker-compose.integration.yml` that defines the test
server service so a developer can start it with one command and run pytest against it.

**Acceptance Criteria:**
- AC-2.1: A file named `docker-compose.integration.yml` exists at the repository root.
- AC-2.2: It defines a single service (e.g., `libby-mcp`) that builds from
  `Dockerfile.integration` (using `build:` with `dockerfile: Dockerfile.integration`).
- AC-2.3: The service maps container port 8000 to host port 8000.
- AC-2.4: The service sets `LIBBY_MCP_API_KEY=ci-test-key` (via the image default or an
  explicit `environment:` entry — the two MUST agree).
- AC-2.5: The service defines a healthcheck (or the compose file documents a readiness
  wait) so callers can know when the MCP endpoint is accepting requests. Because the
  endpoint is POST-only and auth-gated, the healthcheck MUST use a probe that returns a
  deterministic non-5xx status when the server is up (e.g., a `GET /mcp` that returns
  405 is proof-of-life, or a POST `initialize` with the Bearer token).
- AC-2.6: `docker compose -f docker-compose.integration.yml up` starts a server that
  responds on `http://localhost:8000/mcp`.

### FR-3: MCP Protocol Integration Test Suite

**Description:** Provide `tests/integration/test_mcp_protocol.py` with protocol-level
tests that drive a running endpoint (local container or remote) over HTTP. The endpoint
base URL is configurable so the same suite runs locally and in CI.

**Acceptance Criteria:**
- AC-3.1: The file `tests/integration/test_mcp_protocol.py` exists.
- AC-3.2: The target endpoint is read from an environment variable (e.g.,
  `MCP_ENDPOINT`, default `http://localhost:8000/mcp`) and the Bearer token from an
  environment variable (e.g., `MCP_API_KEY`, default `ci-test-key`).
- AC-3.3: Tests are marked with `@pytest.mark.local_integration` (a marker distinct from
  the existing `integration` marker) so they are excluded by the existing unit-test
  command (`pytest tests/ --ignore=tests/integration`) and selected by
  `-m local_integration`. The marker MUST be registered in `pyproject.toml` under
  `[tool.pytest.ini_options].markers` so pytest emits no unknown-marker warning. In
  addition, the test module MUST carry a module-level
  `pytest.mark.skipif(os.environ.get("RUN_LOCAL_INTEGRATION") != "1", ...)` guard so the
  suite is skipped unless `RUN_LOCAL_INTEGRATION=1` is set in the environment. This
  prevents the suite from accidentally running under the existing live-Thunder job or the
  `make integration` target.
- AC-3.4: Tests perform a correct MCP Streamable HTTP handshake: each request is a
  JSON-RPC 2.0 POST to `/mcp` with headers `Authorization: Bearer <token>`,
  `Content-Type: application/json`, and `Accept: application/json, text/event-stream`
  (FastMCP Streamable HTTP requires the SSE media type in `Accept`). Responses are
  parsed as JSON (the server runs with `json_response=True`).
- AC-3.5: **initialize:** An `initialize` request returns a result whose `serverInfo.name`
  is `libby-mcp` and whose `capabilities` advertises `tools`.
- AC-3.6: **tools/list:** A `tools/list` request (after the init handshake) returns
  exactly two tools whose names are `search_titles` and `get_availability`.
  (`get_deep_link` is NOT present post-v2 — see DD-6.)
- AC-3.7: **search_titles — basic search:** A `tools/call` of `search_titles` with
  `library_slug=lcpl` returns a non-error result containing at least one title. Each
  returned title exposes the required base fields (`title` and a deep `link`) and the test
  asserts the `link` matches `https://lcpl.overdrive.com/media/<id>` (where `<id>` is the
  title's id — the v2 link format from DD-6 / libby-mcp-v2 B3). Each title MUST also
  expose all of the following v2 enrichment keys (present on every title, value may be
  `None`/empty per the v2 contract) and the test asserts each key is present:
  `holds_per_copy`, `estimated_wait_days`, `series_name`, `series_position`,
  `narrator` (a list), `publisher`, `edition`, `is_abridged`, and `hours`.
- AC-3.8: **search_titles — bad library slug:** A `search_titles` call with a bogus slug
  (using `library_slug` only, no other filters) returns a human-readable error (an MCP
  tool error / `isError` result or a JSON-RPC error whose message contains the library
  slug string). The test asserts the error text contains the slug and is NOT a raw HTTP
  status code dump (e.g., not `"OverDrive service error (HTTP 404)"`).

  **Pre-implementation verification required:** Before writing this test, confirm whether
  a bogus slug returns a Thunder 404 (which triggers the human-readable message) or a
  Thunder 200 with `totalItems == 0` (which would mean no error is raised). Run:
  ```bash
  curl -s "https://thunder.api.overdrive.com/v2/libraries/bogus-slug-xyz/media?query=a&perPage=1" \
    | python -c "import sys,json; d=json.load(sys.stdin); print('status:', d.get('errorCode', 'ok'), 'items:', d.get('totalItems'))"
  ```
  If the slug returns 200+0 items without an error, update the AC to assert the
  `search_titles` result has zero titles and no error (rather than an `isError` result).
- AC-3.10: **search_titles — multi-library fan-out:** A `search_titles` call with
  `library_slugs=["lcpl","fairfax"]` returns the v2 grouped fan-out shape. The test
  asserts: `payload["libraries"]` is a list of length 2; the `library_slug` values across
  the blocks are exactly `["lcpl", "fairfax"]` (fan-out order preserved); and each block
  contains a `titles` key and does NOT contain an `error` key (i.e., both libraries
  succeeded). (`fairfax` must be verified to return `totalItems > 0` before implementing
  this test — see DD-4.)
- AC-3.11: **get_availability — partial batch:** A `get_availability` call with
  `title_ids=["149795", "999999999"]` (one valid, one bogus) returns per-title data
  where:
  - The valid ID (`149795`) is present and has `copies_owned`, `copies_available`, and
    `holds_count` keys (values may be any integer).
  - The bogus ID (`999999999`) is present and has `error == "not_found"` (not
    `copies_owned: 0` — unknown IDs are reported with an explicit error field per
    `availability.py`).
  - The overall request succeeds (HTTP 200, no top-level `isError`).

  **Pre-implementation verification required:** Before writing this test, confirm that
  Thunder's availability endpoint returns 200 with a mixed per-title response (rather
  than failing the whole batch on an unknown ID). Run:
  ```bash
  curl -s "https://thunder.api.overdrive.com/v2/libraries/lcpl/media/availability?titleIds=149795,999999999" \
    | python -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d, indent=2))" | head -40
  ```
  If Thunder returns 404 for the whole batch when an unknown ID is included, update this
  AC: the test should then assert `isError: true` with a message that names the bad ID,
  and update `availability.py` accordingly.
- AC-3.13: **auth — missing Bearer:** A POST to `/mcp` with no `Authorization` header
  returns HTTP 401.
- AC-3.14: **auth — wrong token:** A POST to `/mcp` with `Authorization: Bearer
  wrong-token` returns HTTP 401.
- AC-3.15: **method guard — GET:** A `GET /mcp` returns HTTP 405 with an `Allow: POST`
  header (per DD-7).
- AC-3.16: Thunder-dependent tests (AC-3.7, AC-3.8, AC-3.10, AC-3.11) are resilient to transient
  Thunder API flakiness via generous HTTP timeouts and/or a retry/flaky marker, so an
  occasional upstream hiccup does not cause a false CI failure.

### FR-4: CI Job — local-integration (No Secrets)

**Description:** Extend `.github/workflows/ci.yml` with a `local-integration` job that
builds the integration image, runs it as a service, and executes the protocol suite on
every PR without any secrets.

**Acceptance Criteria:**
- AC-4.1: A new job `local-integration` is added to `.github/workflows/ci.yml`.
- AC-4.2: The job runs on `pull_request` (and `push` to the same branches as the other
  jobs) with **no `if:` secret gate** — it runs on every PR.
- AC-4.3: The job requires **no AWS credentials and no repository/organization secrets**.
- AC-4.4: The job builds the image from `Dockerfile.integration` and starts the container
  (via `docker compose -f docker-compose.integration.yml up -d`, `docker run`, or a job
  `services:` container), then waits for readiness before running tests.
- AC-4.5: The job installs the Python test dependencies and runs
  `pytest tests/integration/test_mcp_protocol.py -v -m local_integration` against the
  running container, with `RUN_LOCAL_INTEGRATION=1` and `MCP_ENDPOINT` / `MCP_API_KEY`
  set in the job/step env and pointed at it (token = `ci-test-key`). The
  `RUN_LOCAL_INTEGRATION=1` env is required so the module-level `skipif` guard (AC-3.3)
  does not skip the suite.
- AC-4.6: The job sets a sensible timeout (e.g., `timeout-minutes: 10`) to bound hangs
  from Thunder latency.
- AC-4.7: The job tears down / logs the container on completion (collecting container
  logs on failure aids debugging), and does not leave the workflow hanging.
- AC-4.8: The `local-integration` job MUST be in the `comment` job's `needs` list and
  included in the PR results table. It is wired as a required-style check in the same
  manner as `test` where branch protection applies (it blocks PRs when it fails).
  Documentation of the branch protection update is required: `README.md` (or a `docs/`
  page) MUST describe adding the required status check named **`Local Integration Tests`**
  (the `name:` of the `local-integration` job) to the `development` branch protection
  rule. Applying the GitHub setting itself is a manual operator step.

### FR-5: Preserve Existing Jobs and Tests

**Description:** The existing unit suite and the existing live-endpoint integration job
remain exactly as they are, aside from additive changes.

**Acceptance Criteria:**
- AC-5.1: The existing `test` job (unit tests, `--ignore=tests/integration`,
  `--cov-fail-under=80`) is unchanged.
- AC-5.2: `tests/integration/test_live.py` is unchanged and stays gated behind
  `RUN_INTEGRATION=1` and schedule/dispatch.
- AC-5.3: The existing `integration` job keeps its `if: github.event_name == 'schedule'
  || github.event_name == 'workflow_dispatch'` gate (it does not start running on PRs).
- AC-5.4: The `lint` and `typecheck` jobs still apply to any new Python test file
  (`tests/integration/test_mcp_protocol.py` passes `ruff check`, `ruff format --check`,
  and does not break `mypy src/` since mypy scope is `src/` only).
- AC-5.5: No change to `src/` application code is required by this feature. (Only test,
  Docker, compose, Makefile, and CI files change.)

### FR-6: Makefile Target for Local Runs

**Description:** Add a Makefile target that spins up the container and runs the protocol
suite against it in one command.

**Acceptance Criteria:**
- AC-6.1: A `make integration-test` target exists in the Makefile.
- AC-6.2: The target builds/starts the compose service, waits for readiness, runs
  `pytest tests/integration/test_mcp_protocol.py -v -m local_integration`, and tears the
  service down afterward (including on test failure, so no orphaned container is left).
- AC-6.3: The new target is declared in `.PHONY` and does not alter the existing
  `integration` target (which still runs the live Thunder-shape suite with
  `RUN_INTEGRATION=1`).

## Non-Functional Requirements

### NFR-1: No Cloud Dependency

- NFR-1.1: The `local-integration` job and `make integration-test` require no AWS
  account, credentials, or deployed Lambda/API Gateway.
- NFR-1.2: The only external network dependency is the public Thunder API over HTTPS.

### NFR-2: GitHub Actions Free Tier Compatibility

- NFR-2.1: Everything runs on Linux `ubuntu-latest` runners with the Docker daemon
  that is preinstalled on GitHub-hosted runners.
- NFR-2.2: Total `local-integration` wall-clock time stays well within the job timeout
  (target under ~5 minutes: image build + container start + a handful of HTTP tests).

### NFR-3: Resilience to Upstream Flakiness

- NFR-3.1: Thunder-dependent assertions avoid volatile data (exact availability counts);
  they assert on stable structure and fixed test identifiers.
- NFR-3.2: HTTP calls use generous timeouts and Thunder-dependent tests tolerate a
  transient failure via a retry or `@pytest.mark.flaky` mechanism, so a single upstream
  blip does not fail the build. The retry predicate also inspects grouped fan-out
  payloads: a `libraries[*].error` containing a transient phrase ("temporarily
  unavailable" / "service error") triggers a retry, while a non-transient per-library
  error (e.g., unknown slug) does not.

### NFR-4: Developer Ergonomics

- NFR-4.1: A developer can run the full local integration flow with a single command
  (`make integration-test`) or two (`docker compose ... up` then `pytest`).
- NFR-4.2: The endpoint and token are overridable via environment variables so the same
  suite can target a locally running `uvicorn` process (without Docker) or a remote
  endpoint.

### NFR-5: Security

- NFR-5.1: `ci-test-key` is a non-secret test token scoped to a public read-only proxy;
  it is acceptable to hardcode it in the image and workflow. No real secrets are
  introduced by this feature.
- NFR-5.2: Auth negative-path tests (missing / wrong Bearer) confirm the server rejects
  unauthenticated requests with 401, so the harness also validates the auth boundary.

## Out of Scope

- Recording Thunder API responses to cassettes (VCR / pytest-recording). DD-2 fixes the
  approach as live calls.
- Changing the deployed-endpoint integration tests in `test_live.py` beyond leaving them
  as-is.
- Any change to `src/` application behavior, tool schemas, or the Lambda handler.
- Running the container as a long-lived service outside of tests, or publishing the
  integration image to a registry.
- Load/performance testing (the repo already has `sample-workflows/perf-tests.yml` for
  that concern).
- Mutating OverDrive operations (holds, checkouts) — all tests are read-only.
