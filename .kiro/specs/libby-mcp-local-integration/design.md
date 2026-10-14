# Design: Local Integration Test Harness (Docker + CI)

## Architecture Overview

The harness runs the real libby-mcp ASGI app in a container and drives it with an
HTTP-based MCP protocol test suite. There is no AWS, no Lambda, and no Mangum in the
loop — the same ASGI app factory used in production (`src.handler:_build_app`) is served
directly by uvicorn. The only external dependency is the public Thunder API.

```
┌──────────────────────────┐        ┌─────────────────────────────────────────────┐
│ pytest                   │  HTTP   │ Docker container (Dockerfile.integration)   │
│ tests/integration/       │ ──────▶ │   uvicorn src.handler:_build_app --factory  │
│ test_mcp_protocol.py     │ ◀────── │     └─ AuthMiddleware (Bearer ci-test-key)  │
│ (JSON-RPC over POST /mcp)│  :8000  │          └─ FastMCP Streamable HTTP app     │
└──────────────────────────┘        │               (search_titles,               │
         ▲                           │                get_availability)            │
         │ MCP_ENDPOINT              │                                            │
         │ MCP_API_KEY               └───────────────────────┬─────────────────────┘
         │                                                   │ HTTPS (public, no auth)
   docker compose / CI service                               ▼
                                              ┌──────────────────────────────────┐
                                              │ Thunder API                      │
                                              │ thunder.api.overdrive.com        │
                                              └──────────────────────────────────┘
```

## Why This Shape

The production handler already separates "build the ASGI app" (`_build_app`) from "adapt
it to Lambda" (`Mangum` inside `lambda_handler`). That seam is what makes a cloud-free
harness possible:

```python
# src/handler.py (context)
def _build_app() -> AuthMiddleware:
    fastmcp_app = _make_mcp().streamable_http_app()
    return AuthMiddleware(fastmcp_app)

def lambda_handler(event, context):
    handler = Mangum(_build_app(), lifespan="on")
    return handler(event, context)
```

uvicorn can call `_build_app` as an ASGI application factory
(`uvicorn src.handler:_build_app --factory`). Each worker process calls the factory once,
which satisfies the FastMCP constraint that `StreamableHTTPSessionManager.run()` is called
only once per `FastMCP` instance (a fresh instance is built per factory call). We run a
single uvicorn worker so the one-call-per-instance contract holds.

Hitting Thunder live (DD-2) is deliberate: Thunder catalog metadata (title IDs, slugs,
names) is stable, so assertions on known-good identifiers (Going Postal = 149795; slugs
`lcpl`, `fairfax`) are reliable. Only availability counts fluctuate, so the suite never
asserts exact counts — it asserts structure and presence.

## Components

### 1. `Dockerfile.integration` (repo root)

A single-stage image optimized for "build fast, run the server":

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "uvicorn[standard]>=0.30"

# Application source.
COPY src/ ./src/

# Make `src` importable as a top-level package (uvicorn resolves src.handler).
ENV PYTHONPATH=/app
ENV LIBBY_MCP_API_KEY=ci-test-key

EXPOSE 8000

CMD ["uvicorn", "src.handler:_build_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

Notes:
- `requirements.txt` carries the pinned runtime deps (httpx, fastmcp, mangum). `mangum`
  is harmless here — it is only imported, not used, when serving via uvicorn.
- `uvicorn[standard]` is added explicitly (it is a dev/test server, not in
  `requirements.txt`).
- `PYTHONPATH=/app` lets `src.handler` resolve without a full editable install; a
  `pip install -e .` is an acceptable alternative but requires copying `pyproject.toml`
  and the package metadata (shown above) — either path satisfies AC-1.4.
- `--workers 1` keeps a single FastMCP instance per process, honoring the
  run-once-per-instance constraint.

**`.dockerignore`:** ensure `tests/`, `infra/`, `.git/`, `.github/`, `docs/`,
`__pycache__/`, and local virtualenvs are excluded so the build context is small and the
image contains only runtime code. The repo already has a `.dockerignore`; extend it only
if these entries are missing (AC-1.9) — do not remove entries the existing CDK/Lambda
packaging relies on.

### 2. `docker-compose.integration.yml` (repo root)

```yaml
services:
  libby-mcp:
    build:
      context: .
      dockerfile: Dockerfile.integration
    image: libby-mcp-integration:local
    ports:
      - "8000:8000"
    environment:
      LIBBY_MCP_API_KEY: ci-test-key
    healthcheck:
      # GET /mcp returns 405 (AuthMiddleware POST-only guard) — proof the ASGI
      # app is up and routing. 405 is a success signal for liveness here.
      # python:3.12-slim has no curl/wget, so we use stdlib urllib.
      test: ["CMD", "python", "-c", "import sys,urllib.request as u,urllib.error as e\ntry: u.urlopen(u.Request('http://localhost:8000/mcp'),timeout=3)\nexcept e.HTTPError as ex: sys.exit(0 if ex.code==405 else 1)\nexcept: sys.exit(1)\nsys.exit(1)"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 5s
```

Because the endpoint is POST-only and auth-gated, the only unauthenticated,
side-effect-free liveness probe is `GET /mcp`, which the middleware answers with 405. The
healthcheck treats a 405 as "up." A simpler alternative, if the inline Python probe proves
awkward in `CMD-SHELL`, is a tiny probe script baked into the image or a `wget`/`curl`
check that treats HTTP 405 as healthy; the Makefile/CI readiness wait (below) uses the
same 405 signal and is the authoritative gate for tests.

### 3. `tests/integration/test_mcp_protocol.py`

A pure HTTP client suite (uses `httpx`, already a project dependency). It does not import
`src` — it treats the server as a black box over the wire, which is what makes it portable
across local-uvicorn, local-docker, and remote targets.

**Configuration (AC-3.2):**

```python
import os
ENDPOINT = os.environ.get("MCP_ENDPOINT", "http://localhost:8000/mcp")
API_KEY = os.environ.get("MCP_API_KEY", "ci-test-key")
TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "20"))
```

**Markers / selection (AC-3.3):**

```python
import os
import pytest

pytestmark = [
    pytest.mark.local_integration,
    pytest.mark.skipif(
        os.environ.get("RUN_LOCAL_INTEGRATION") != "1",
        reason="set RUN_LOCAL_INTEGRATION=1 to run local integration tests",
    ),
]
```

The file lives under `tests/integration/`, so `pytest tests/ --ignore=tests/integration`
(the unit job) never collects it, and `-m local_integration` selects it. The distinct
marker `local_integration` (not the existing `integration`) ensures these tests do not
accidentally run under the existing live-Thunder job or `make integration` target.
`local_integration` MUST be registered in `pyproject.toml`
`[tool.pytest.ini_options].markers` so pytest emits no unknown-marker warning. The
module-level `skipif` on `RUN_LOCAL_INTEGRATION` is a second, environment-based guard:
even if `-m local_integration` is selected, the suite is skipped unless
`RUN_LOCAL_INTEGRATION=1` is set. The `local-integration` CI job and the
`make integration-test` target both set `RUN_LOCAL_INTEGRATION=1`.

**MCP Streamable HTTP call helper (AC-3.4):** FastMCP's Streamable HTTP transport requires
the client to advertise both JSON and SSE in `Accept`, even when the server replies with
JSON (`json_response=True`). A shared helper issues JSON-RPC 2.0 requests. A separate
`notify()` helper sends JSON-RPC notifications (no `id` field) and expects no response
body:

```python
def mcp_call(method, params=None, *, token=API_KEY, _id=1, client=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    resp = (client or httpx).post(ENDPOINT, json=payload, headers=headers, timeout=TIMEOUT)
    return resp

def notify(method, params=None, *, token=API_KEY, client=None):
    """Send a JSON-RPC notification (no 'id' field, expect no response body)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    resp = (client or httpx).post(ENDPOINT, json=payload, headers=headers, timeout=TIMEOUT)
    return resp
```

**Session handshake:** For stateless Streamable HTTP (`stateless_http=True` in the
handler), each POST is independent, but the MCP lifecycle still expects an `initialize`
first. The `initialize` request MUST include `protocolVersion` (`"2025-03-26"`),
`capabilities` (`{}`), and `clientInfo` (`{"name": "libby-mcp-it", "version": "0"}`).
A session-scoped fixture performs `initialize` (via `mcp_call`) and then sends
`notifications/initialized` (via the `notify()` helper, which omits the `id` field),
carrying any `Mcp-Session-Id` header the server emits so subsequent calls can echo it if
present. In stateless mode no session id is required, but the fixture handles both cases
defensively.

**Test map (one or more tests per AC):**

| Test | AC | Assertion summary |
| --- | --- | --- |
| `test_initialize_returns_server_info` | AC-3.5 | `result.serverInfo.name == "libby-mcp"`, `result.capabilities.tools` present |
| `test_tools_list_has_two_tools` | AC-3.6 | tool names == {`search_titles`, `get_availability`} |
| `test_search_titles_basic` | AC-3.7 | `tools/call search_titles {library_slug: lcpl}` → ≥1 title; each has `title` + `link` matching `https://lcpl.overdrive.com/media/<id>`; all v2 enrichment keys present: `holds_per_copy`, `estimated_wait_days`, `series_name`, `series_position`, `narrator` (list), `publisher`, `edition`, `is_abridged`, `hours` |
| `test_search_titles_bad_slug` | AC-3.8 | bogus slug → human-readable error naming the library, not an opaque 404 |
| `test_search_titles_multi_library` | AC-3.10 | `library_slugs=["lcpl","fairfax"]` → `payload["libraries"]` has length 2; `library_slug` values == `["lcpl","fairfax"]`; each block has a `titles` key and no `error` key |
| `test_get_availability_partial_batch` | AC-3.11 | `[149795, <bogus>]` → valid id has counts, bogus id = not-owned/not-found, request still succeeds |
| `test_auth_missing_bearer` | AC-3.13 | no `Authorization` → HTTP 401 |
| `test_auth_wrong_token` | AC-3.14 | `Bearer wrong-token` → HTTP 401 |
| `test_get_mcp_returns_405` | AC-3.15 | `GET /mcp` → HTTP 405 with `Allow: POST` |

**Reading tool results:** FastMCP returns tool output in the MCP envelope
`result.content[0].text` as a JSON string. A helper parses it, guarding against
`isError` results (where the text is a human-readable error string, not JSON):

```python
import json
def tool_payload(resp):
    data = resp.json()
    if "error" in data:
        return {"_jsonrpc_error": data["error"]}
    result = data["result"]
    content = result["content"]
    if result.get("isError"):
        return {"_is_error": True, "text": content[0]["text"]}
    return json.loads(content[0]["text"])
```

For the bad-slug case (AC-3.8), the handler raises `ToolError(result["error"])`, which
FastMCP surfaces as an error result (`isError: true`) with a human-readable text message.
The test asserts that `_is_error` is `True` and that the `.text` field contains a
substring naming the library (substring match), reading as human prose rather than a bare
status code.

**Flakiness handling (AC-3.16 / NFR-3):** Thunder-dependent tests
(`search_titles`/`get_availability`) use the generous `TIMEOUT` and a lightweight retry.
The retry helper retries when:
- An `httpx.TimeoutException` or HTTP 5xx status is received, OR
- The tool response has `_is_error` True and the `.text` contains "temporarily
  unavailable" or "service error" (since `ThunderClient` turns timeouts/5xx into tool
  errors returned as HTTP 200 + `isError: true`), OR
- The response is a grouped fan-out payload and any `libraries[*].error` string contains a
  transient phrase ("temporarily unavailable" or "service error"). This covers the
  multi-library case (AC-3.10), where a per-library block can carry a transient upstream
  error even though the overall request succeeded. A per-library `error` that is NOT
  transient (e.g., an unknown-slug message) is not retried.

Retry is bounded to 3 attempts with a 3-second delay between attempts. If the project
opts to add `pytest-rerunfailures`, those tests may instead use
`@pytest.mark.flaky(reruns=2, reruns_delay=3)`. Auth and method-guard tests
(AC-3.13/3.14/3.15) do not touch Thunder and are not retried.

### 4. CI job: `local-integration`

Added to `.github/workflows/ci.yml`. It runs on every PR (no secret gate). The server runs
as a container; pytest runs on the runner host against `localhost:8000`.

```yaml
  local-integration:
    name: Local Integration Tests
    runs-on: ubuntu-latest
    needs: test
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: Build integration image
        run: docker compose -f docker-compose.integration.yml build

      - name: Start server
        run: docker compose -f docker-compose.integration.yml up -d

      - name: Wait for readiness (GET /mcp -> 405)
        run: |
          for i in $(seq 1 30); do
            code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/mcp || true)
            if [ "$code" = "405" ]; then echo "server up"; exit 0; fi
            sleep 2
          done
          echo "server did not become ready"; exit 1

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: 'pip'

      - name: Install test deps
        run: pip install -r requirements-dev.txt

      - name: Run MCP protocol tests
        env:
          RUN_LOCAL_INTEGRATION: "1"
          MCP_ENDPOINT: http://localhost:8000/mcp
          MCP_API_KEY: ci-test-key
        run: pytest tests/integration/test_mcp_protocol.py -v -m local_integration

      - name: Dump container logs on failure
        if: failure()
        run: docker compose -f docker-compose.integration.yml logs

      - name: Tear down
        if: always()
        run: docker compose -f docker-compose.integration.yml down -v
```

Design points:
- `needs: test` keeps the (cheap) unit gate ahead of the (heavier) container job, matching
  the existing lint → typecheck → test chain. It is additive and does not change those
  jobs.
- No `if:` gate on event type and no `secrets.*` usage ⇒ runs on every PR with zero cloud
  dependency (AC-4.2, AC-4.3).
- The readiness loop uses the same 405 liveness signal as the compose healthcheck.
- `pip install -r requirements-dev.txt` provides pytest + httpx on the runner; the test
  file imports only `httpx`, `pytest`, `json`, `os` — no `src` import, so no
  `pip install -e .` is needed for this job.

The existing `integration` job is left intact, keeping its
`if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'` gate and
`RUN_INTEGRATION=1` (FR-5 / AC-5.3). The `comment` job's `needs` list MUST add
`local-integration` so the PR comment reports its result; `local-integration` MUST appear
in the PR results table (AC-4.8).

### 5. Makefile target: `integration-test`

```make
## Build + run the containerized MCP server and run the protocol suite against it
integration-test:
	docker compose -f docker-compose.integration.yml up -d --build
	@for i in $$(seq 1 30); do \
		code=$$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/mcp || true); \
		if [ "$$code" = "405" ]; then echo "server up"; break; fi; \
		sleep 2; \
	done
	RUN_LOCAL_INTEGRATION=1 MCP_ENDPOINT=http://localhost:8000/mcp MCP_API_KEY=ci-test-key \
		pytest tests/integration/test_mcp_protocol.py -v -m local_integration; \
		status=$$?; \
		docker compose -f docker-compose.integration.yml down -v; \
		exit $$status
```

The trailing `down -v` runs whether pytest passes or fails (its exit code is captured and
re-raised), so no orphaned container is left (AC-6.2). `integration-test` is added to the
`.PHONY` line; the existing `integration` target (live Thunder-shape suite with
`RUN_INTEGRATION=1`) is untouched (AC-6.3).

## Data Model: MCP Request / Response Shapes

All requests are JSON-RPC 2.0 over `POST /mcp`:

```json
{ "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": { "name": "search_titles", "arguments": { "library_slug": "lcpl" } } }
```

Tool success is wrapped by FastMCP as:

```json
{ "jsonrpc": "2.0", "id": 1,
  "result": { "content": [ { "type": "text", "text": "{...tool JSON...}" } ] } }
```

Tool failure (handler raised `ToolError`) is surfaced as an error result
(`isError: true` content) or a JSON-RPC `error` object; tests accept either and assert the
message is human-readable. HTTP-level auth/method failures are returned by `AuthMiddleware`
as plain JSON bodies with the relevant status (401 / 405) before FastMCP is reached.

## Known-Good Test Fixtures (DD-4)

| Fixture | Value | Used by |
| --- | --- | --- |
| Title: Going Postal | `149795` | AC-3.11 (availability) |
| Library slug | `lcpl` | AC-3.7, AC-3.10, AC-3.11 |
| Library slug | `fairfax` *(needs verification — see DD-4; curl must show `totalItems > 0` before implementing AC-3.10)* | AC-3.10 (fan-out) |
| Bogus title id | e.g. `1` or `999999999` | AC-3.11 (not-owned path) |
| Bogus slug | e.g. `not-a-real-library-xyz` | AC-3.8 (human-readable error) |

Tests assert on these stable identifiers and on response structure, never on volatile
availability counts (NFR-3.1).

## Error Handling and Edge Cases

- **Server not ready in CI:** the readiness loop fails the job with a clear message rather
  than letting pytest connect-refuse flakily.
- **Thunder transient failure:** generous timeout + bounded retry on Thunder-dependent
  tests (NFR-3.2); non-Thunder tests are deterministic and un-retried.
- **FastMCP `Accept` requirement:** every request sends
  `Accept: application/json, text/event-stream`; omitting the SSE type yields a 406 from
  the transport, which the helper avoids by construction (AC-3.4).
- **Single worker:** `--workers 1` avoids multiple FastMCP instances per container and the
  run-once-per-instance error path.
- **Bad-slug ambiguity:** Thunder returns 200 for unknown slugs; the handler's
  empty-items heuristic produces the human-readable error the test asserts on (AC-3.8).

## Testing Strategy

- **Unit tests:** unchanged; continue to run mocked under the `test` job with 80%
  coverage (AC-5.1).
- **Local protocol tests (new):** black-box HTTP against the container, run on every PR
  via `local-integration` and locally via `make integration-test`.
- **Live-endpoint / Thunder-shape tests (existing):** `test_live.py` stays gated behind
  `RUN_INTEGRATION=1` and schedule/dispatch (AC-5.2, AC-5.3).
- **Lint/type:** the new test file passes `ruff check` / `ruff format --check`; mypy scope
  stays `src/` so the test file does not need strict typing (AC-5.4).

## Files Changed / Added

| Path | Change |
| --- | --- |
| `Dockerfile.integration` | **new** — uvicorn-served ASGI image (FR-1) |
| `docker-compose.integration.yml` | **new** — local test service (FR-2) |
| `tests/integration/test_mcp_protocol.py` | **new** — MCP protocol suite (FR-3) |
| `pyproject.toml` | **edit** — register `local_integration` pytest marker (AC-3.3) |
| `.github/workflows/ci.yml` | **edit** — add `local-integration` job (FR-4) |
| `README.md` (or `docs/`) | **edit** — document the branch-protection operator step naming the `Local Integration Tests` required check (AC-4.8) |
| `Makefile` | **edit** — add `integration-test` target + `.PHONY` (FR-6) |
| `.dockerignore` | **edit (if needed)** — exclude tests/infra/docs from build context (AC-1.9) |

No `src/` changes (AC-5.5).
