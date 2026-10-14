# Tasks: Local Integration Test Harness (Docker + CI)

## Workflow Policy

This feature is a **single ROADMAP item / single PR**: "Local integration test harness
(Docker + CI)". It depends on ROADMAP item 8 (v2 Group C content enrichment) being
complete, so the v2 enrichment fields exist in `search_titles` responses and the protocol
test assertions (AC-3.7) can cover them.

Branch from `development`, implement all tasks below in one branch, and squash-merge back
once CI is green. The existing unit (`test`) job must stay green and the existing
`integration` job must stay gated — this feature is additive.

**Red-Commit / Green-PR:** The new protocol test file may be committed before the
Dockerfile/compose exist (it skips cleanly because it is `-m local_integration` and not
collected by the unit job). All checks must pass before merge.

Each task lists the acceptance criteria it satisfies (from `requirements.md`) and the
sections it implements (from `design.md`). Do not modify `src/` application code.

---

## Task 1: Build the integration Docker image

**Requirements:** FR-1 (AC-1.1 .. AC-1.9), NFR-1, NFR-2
**Design:** Components §1 (`Dockerfile.integration`), §Why This Shape

**Description:** Create a repo-root `Dockerfile.integration` that serves the production
ASGI app factory (`src.handler:_build_app`) under uvicorn with no AWS/Lambda dependency.

**Steps:**
1. Create `Dockerfile.integration` at the repo root based on `python:3.12-slim`.
2. Copy `requirements.txt` (and `pyproject.toml` if using editable install) first, then
   `pip install --no-cache-dir -r requirements.txt` for layer caching.
3. Install the test server: `pip install --no-cache-dir "uvicorn[standard]>=0.30"`.
4. Copy `src/` into the image. Make `src` importable either via `ENV PYTHONPATH=/app`
   (with `WORKDIR /app`) or `pip install -e .`.
5. Set `ENV LIBBY_MCP_API_KEY=ci-test-key`.
6. `EXPOSE 8000`.
7. Set `CMD` to
   `uvicorn src.handler:_build_app --factory --host 0.0.0.0 --port 8000 --workers 1`.
8. Review the existing root `.dockerignore`; add `tests/`, `infra/`, `.github/`, `docs/`,
   `__pycache__/`, and local venvs if missing. Do not remove entries the CDK/Lambda
   packaging relies on.

**Acceptance Criteria:**
- `Dockerfile.integration` exists at the repo root (AC-1.1).
- Based on Python 3.12 (AC-1.2); installs `requirements.txt` + uvicorn (AC-1.3).
- Copies `src/` and makes `src.handler` importable (AC-1.4).
- Sets `LIBBY_MCP_API_KEY=ci-test-key` (AC-1.5).
- Default command runs the uvicorn factory on `0.0.0.0:8000` (AC-1.6); exposes 8000
  (AC-1.7).
- `docker build -f Dockerfile.integration .` succeeds on a clean Linux host with no AWS
  creds / no secrets (AC-1.8).
- `.dockerignore` excludes non-runtime files without breaking existing packaging (AC-1.9).

**Verification:**
```bash
docker build -f Dockerfile.integration -t libby-mcp-integration:local .
docker run -d --name libby-it -p 8000:8000 libby-mcp-integration:local
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/mcp   # expect 405
docker rm -f libby-it
```

---

## Task 2: Add the docker-compose service

**Requirements:** FR-2 (AC-2.1 .. AC-2.6), NFR-4
**Design:** Components §2 (`docker-compose.integration.yml`)

**Description:** Define a one-command local server using the integration image.

**Steps:**
1. Create `docker-compose.integration.yml` at the repo root.
2. Define a single service (e.g., `libby-mcp`) with
   `build: {context: ., dockerfile: Dockerfile.integration}`.
3. Map `8000:8000`.
4. Set `environment: {LIBBY_MCP_API_KEY: ci-test-key}` to match the image default.
5. Add a healthcheck that treats `GET /mcp` → HTTP 405 as healthy (proof-of-life for the
   POST-only, auth-gated endpoint). Since `python:3.12-slim` has no `curl` or `wget`,
   use a `CMD` form with inline Python using stdlib `urllib`:
   `test: ["CMD", "python", "-c", "import sys,urllib.request as u,urllib.error as e\ntry: u.urlopen(u.Request('http://localhost:8000/mcp'),timeout=3)\nexcept e.HTTPError as ex: sys.exit(0 if ex.code==405 else 1)\nexcept: sys.exit(1)\nsys.exit(1)"]`

**Acceptance Criteria:**
- `docker-compose.integration.yml` exists at the repo root (AC-2.1).
- Single service builds from `Dockerfile.integration` (AC-2.2); maps port 8000 (AC-2.3).
- Sets `LIBBY_MCP_API_KEY=ci-test-key` consistent with the image (AC-2.4).
- Defines a 405-based healthcheck / documented readiness probe (AC-2.5).
- `docker compose -f docker-compose.integration.yml up` serves `http://localhost:8000/mcp`
  (AC-2.6).

**Verification:**
```bash
docker compose -f docker-compose.integration.yml up -d --build
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/mcp   # expect 405
docker compose -f docker-compose.integration.yml down -v
```

---

## Task 3: Write the MCP protocol test suite

**Requirements:** FR-3 (AC-3.1 .. AC-3.16), NFR-3, NFR-5
**Design:** Components §3 (`test_mcp_protocol.py`), §Data Model, §Known-Good Fixtures

**Description:** Create `tests/integration/test_mcp_protocol.py` — a black-box HTTP suite
(httpx only, no `src` import) that drives a running endpoint. Configurable via
`MCP_ENDPOINT` / `MCP_API_KEY`, marked `@pytest.mark.local_integration`.

**Precheck (DD-4 — before writing the multi-library test):** Verify the `fairfax` slug
resolves to a real library returning `totalItems > 0`:
```bash
curl -s "https://thunder.api.overdrive.com/v2/libraries/fairfax/media?query=postal&perPage=1" \
  | python -c "import sys,json; print(json.load(sys.stdin).get('totalItems'))"   # expect > 0
```
If `fairfax` 404s or returns `0`, substitute a verified slug (e.g., `nypl`) and update
AC-3.10, the design test map, and the multi-library bullet below before implementing.

**Steps:**
1. Create `tests/integration/test_mcp_protocol.py` with a module-level marker and
   environment guard:
   `pytestmark = [pytest.mark.local_integration, pytest.mark.skipif(os.environ.get("RUN_LOCAL_INTEGRATION") != "1", reason="set RUN_LOCAL_INTEGRATION=1 to run local integration tests")]`.
   The `skipif` guard ensures the suite does not accidentally run under the existing
   live-Thunder job or `make integration` target even if `-m local_integration` is
   somehow selected without a running container.
1a. Register the `local_integration` marker in `pyproject.toml` under
   `[tool.pytest.ini_options].markers` (e.g.,
   `"local_integration: MCP protocol tests against a running container (gated by RUN_LOCAL_INTEGRATION=1)"`)
   so pytest does not emit an unknown-marker warning.
2. Read `MCP_ENDPOINT` (default `http://localhost:8000/mcp`), `MCP_API_KEY` (default
   `ci-test-key`), `MCP_TIMEOUT` (default `20`) from the environment.
3. Add an `mcp_call(method, params, token, _id)` helper that POSTs JSON-RPC 2.0 with
   headers `Content-Type: application/json`,
   `Accept: application/json, text/event-stream`, and (when a token is given)
   `Authorization: Bearer <token>`. Add a separate `notify(method, params, token)` helper
   that sends JSON-RPC notifications (no `id` field) and expects no response body.
4. Add a session fixture that runs `initialize` (via `mcp_call`) with params including
   `protocolVersion` (`"2025-03-26"`), `capabilities` (`{}`), and `clientInfo`
   (`{"name": "libby-mcp-it", "version": "0"}`), then sends
   `notifications/initialized` (via `notify()`, which omits the `id` field), carrying
   any `Mcp-Session-Id` header if the server emits one (defensive; stateless mode needs
   none).
5. Add a `tool_payload(resp)` helper that extracts `result.content[0].text` and
   `json.loads` it, returning the JSON-RPC `error` object when present. The helper MUST
   check for `isError` before calling `json.loads`: if `result.get("isError")` is true,
   return `{"_is_error": True, "text": content[0]["text"]}` instead of attempting to
   parse it as JSON (error text is human-readable, not JSON).
6. Add a bounded retry helper (up to 3 attempts, 3s delay) that retries
   Thunder-dependent HTTP calls when:
   - An `httpx.TimeoutException` or HTTP 5xx status is received, OR
   - The tool response has `_is_error` True and the `.text` contains "temporarily
     unavailable" or "service error" (since `ThunderClient` turns timeouts/5xx into tool
     errors returned as HTTP 200 + `isError: true`), OR
   - The response is a grouped fan-out payload and any `libraries[*].error` string
     contains a transient phrase ("temporarily unavailable" or "service error"). A
     non-transient per-library `error` (e.g., unknown-slug) is NOT retried.
   If the project opts to add `pytest-rerunfailures`, those tests may instead use
   `@pytest.mark.flaky(reruns=2, reruns_delay=3)` (see Task 6).
7. Implement the tests (one or more per AC):
   - `test_initialize_returns_server_info` — `serverInfo.name == "libby-mcp"`,
     `capabilities.tools` present (AC-3.5).
   - `test_tools_list_has_two_tools` — names == {`search_titles`, `get_availability`}
     (AC-3.6, DD-6).
   - `test_search_titles_basic` — `library_slug=lcpl` → ≥1 title; each has `title` and a
     `link` matching `https://lcpl.overdrive.com/media/<id>`; assert all v2 enrichment
     keys are present: `holds_per_copy`, `estimated_wait_days`, `series_name`,
     `series_position`, `narrator` (list), `publisher`, `edition`, `is_abridged`, `hours`
     (AC-3.7).
   - `test_search_titles_bad_slug` — bogus slug → human-readable error naming the library,
     not an opaque 404 (AC-3.8).
   - `test_search_titles_multi_library` — `library_slugs=["lcpl","fairfax"]` →
     `payload["libraries"]` has length 2; the `library_slug` values across blocks are
     exactly `["lcpl","fairfax"]`; each block has a `titles` key and no `error` key
     (both succeeded) (AC-3.10). Note: `fairfax` must be verified to return
     `totalItems > 0` before implementing this test (see DD-4 / the Precheck above).
   - `test_get_availability_partial_batch` — `[149795, <bogus>]` → valid id has counts,
     bogus id reported not-owned/not-found, request still succeeds (AC-3.11).
   - `test_auth_missing_bearer` — no `Authorization` → HTTP 401 (AC-3.13).
   - `test_auth_wrong_token` — `Bearer wrong-token` → HTTP 401 (AC-3.14).
   - `test_get_mcp_returns_405` — `GET /mcp` → HTTP 405 with `Allow: POST` (AC-3.15).
8. Give each assertion a message that explains what failed and how to fix it.
9. Ensure `ruff check` and `ruff format --check` pass for the file.

**Acceptance Criteria:**
- File exists and is collected only under `-m local_integration` / not by the unit job,
  and is skipped unless `RUN_LOCAL_INTEGRATION=1` is set (AC-3.1, AC-3.3).
- The `local_integration` marker is registered in `pyproject.toml`
  `[tool.pytest.ini_options].markers` (no unknown-marker warning).
- Endpoint/token are environment-configurable with the documented defaults (AC-3.2).
- Requests use JSON-RPC 2.0 with the SSE-inclusive `Accept` header (AC-3.4).
- All nine mapped behaviors (AC-3.5 .. AC-3.15, excluding removed AC-3.9 and AC-3.12)
  have passing tests.
- Thunder-dependent tests tolerate a transient upstream failure (AC-3.16, NFR-3).

**Verification:** With a server running (Task 1/2):
```bash
RUN_LOCAL_INTEGRATION=1 MCP_ENDPOINT=http://localhost:8000/mcp MCP_API_KEY=ci-test-key \
  pytest tests/integration/test_mcp_protocol.py -v -m local_integration
ruff check tests/integration/test_mcp_protocol.py
ruff format --check tests/integration/test_mcp_protocol.py
# Confirm the marker is registered (no unknown-marker warning):
pytest tests/integration/test_mcp_protocol.py --collect-only -m local_integration -q
```

---

## Task 4: Add the `local-integration` CI job

**Requirements:** FR-4 (AC-4.1 .. AC-4.8), FR-5 (AC-5.1 .. AC-5.4), NFR-1, NFR-2
**Design:** Components §4 (CI job)

**Description:** Extend `.github/workflows/ci.yml` with a secret-free job that builds the
image, runs the container, waits for readiness, and runs the protocol suite — on every PR.

**Steps:**
1. Add job `local-integration` (`runs-on: ubuntu-latest`, `needs: test`,
   `timeout-minutes: 10`). No `if:` event gate, no `secrets.*`.
2. Steps: checkout → `docker compose -f docker-compose.integration.yml build` →
   `... up -d`.
3. Readiness loop: poll `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/mcp`
   until it returns `405` (bounded retries), else fail with a clear message.
4. `actions/setup-python@v5` (3.12, pip cache) → `pip install -r requirements-dev.txt`.
5. Run tests with `RUN_LOCAL_INTEGRATION=1`, `MCP_ENDPOINT=http://localhost:8000/mcp`,
   `MCP_API_KEY=ci-test-key` set in the step `env:`
   (`RUN_LOCAL_INTEGRATION=1` is required so the module-level `skipif` guard does not
   skip the suite): `pytest tests/integration/test_mcp_protocol.py -v -m local_integration`.
6. On failure, dump container logs (`docker compose ... logs`, `if: failure()`).
7. Always tear down (`docker compose ... down -v`, `if: always()`).
8. Leave the existing `lint`, `typecheck`, `test`, and `integration` jobs unchanged. The
   `integration` job keeps its schedule/dispatch `if:` gate and `RUN_INTEGRATION=1`.
   Add `local-integration` to the `comment` job's `needs` list and results table
   (required, not optional — AC-4.8).
9. Document the branch-protection operator step (AC-4.8): add a short section to
   `README.md` (or a `docs/` page) instructing the operator to add the required status
   check named **`Local Integration Tests`** (the `name:` of the `local-integration`
   job) to the `development` branch protection rule, alongside the existing required
   checks (`lint`, `typecheck`, `test`). Note that applying the GitHub setting is a
   manual operator step; the spec/code only documents it.
10. Confirm the workflow is valid YAML.

**Acceptance Criteria:**
- `local-integration` job exists in `ci.yml` (AC-4.1), runs on every PR with no secret
  gate (AC-4.2) and no AWS creds/secrets (AC-4.3).
- Builds the image, starts the container, waits for readiness (AC-4.4).
- Installs test deps and runs the protocol suite against the container with the test token
  (AC-4.5).
- Has a bounded timeout (AC-4.6); dumps logs and tears down (AC-4.7).
- Behaves as a blocking PR check, and the branch-protection update is documented in
  `README.md` (or `docs/`) naming the required check **`Local Integration Tests`** as an
  operator step (AC-4.8).
- Existing `test` job unchanged (AC-5.1); `test_live.py` unchanged (AC-5.2); `integration`
  job stays gated (AC-5.3); new test file passes lint/format and does not affect
  `mypy src/` (AC-5.4).

**Verification:**
```bash
python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('valid')"
# Full exercise happens on PR; locally mirror via Task 5.
```

---

## Task 5: Add the `make integration-test` target

**Requirements:** FR-6 (AC-6.1 .. AC-6.3), NFR-4
**Design:** Components §5 (Makefile target)

**Description:** One-command local flow: build/start the container, wait for readiness, run
the protocol suite, tear down (even on failure).

**Steps:**
1. Add `integration-test` to the `.PHONY` declaration.
2. Implement the target: `docker compose -f docker-compose.integration.yml up -d --build`,
   a 405-based readiness loop, then
   `RUN_LOCAL_INTEGRATION=1 MCP_ENDPOINT=... MCP_API_KEY=ci-test-key
   pytest tests/integration/test_mcp_protocol.py -v -m local_integration`
   (`RUN_LOCAL_INTEGRATION=1` is required so the module-level `skipif` guard does not
   skip the suite).
3. Capture pytest's exit code, run `docker compose ... down -v` unconditionally, then exit
   with the captured status.
4. Do not change the existing `integration` target (live Thunder-shape suite with
   `RUN_INTEGRATION=1`).

**Acceptance Criteria:**
- `make integration-test` exists (AC-6.1).
- It builds/starts the service, waits, runs the protocol suite, and tears down on both
  success and failure (AC-6.2).
- Declared in `.PHONY`; existing `integration` target untouched (AC-6.3).

**Verification:**
```bash
make integration-test     # exits 0 when the suite passes; container removed afterward
docker ps -a | grep libby-mcp-integration || echo "no orphaned container"
```

---

## Task 6: (Optional) Flaky-test dependency and dev docs

**Requirements:** AC-3.16, NFR-3
**Design:** Components §3 (flakiness handling)

**Description:** Only if Task 3 chooses the `@pytest.mark.flaky` approach over a hand-rolled
retry helper: add the dependency and register the marker. Prefer the retry helper to avoid
a new dependency; this task exists to keep the choice explicit.

**Steps:**
1. If using `pytest.mark.flaky`: add `pytest-rerunfailures` to `requirements-dev.txt`
   (pinned) and to the `[project.optional-dependencies].dev` list in `pyproject.toml`.
2. Register the `flaky` marker under `[tool.pytest.ini_options].markers` to avoid
   unknown-marker warnings (or rely on `pytest-rerunfailures` registering it).
3. If using the retry helper instead, skip steps 1–2 and ensure the helper is covered by
   the suite's generous timeout.

**Acceptance Criteria:**
- Either `pytest-rerunfailures` is pinned in dev deps and the `flaky` marker is registered,
  OR the suite uses a self-contained retry helper and no new dependency is added.
- No unknown-marker warnings are emitted by `pytest`.

**Verification:**
```bash
pytest tests/integration/test_mcp_protocol.py --collect-only -m local_integration -q
```

---

## Task 7: Full preflight and ROADMAP tick

**Requirements:** All; FR-5 (regression guard)
**Design:** §Testing Strategy, §Files Changed

**Description:** Validate the whole feature end to end and record completion.

**Steps:**
1. Run `make lint` and `make typecheck` — both green (new test file passes lint/format;
   mypy scope is `src/`, unaffected).
2. Run `make test` — unit suite + 80% coverage unchanged and green (AC-5.1).
3. Run `make integration-test` — containerized protocol suite green locally.
4. Confirm `tests/integration/test_live.py` and the `integration` CI job are unchanged and
   still gated (AC-5.2, AC-5.3).
5. Confirm no `src/` files changed (AC-5.5).
6. Tick this feature's ROADMAP item ("Local integration test harness (Docker + CI)") with
   the PR number and check off the boxes in this `tasks.md`.

**Acceptance Criteria:**
- `make lint`, `make typecheck`, `make test` all pass.
- `make integration-test` passes against the local container.
- The existing live-endpoint job and unit job are provably unchanged (git diff shows only
  additive changes to `ci.yml` and `Makefile`, plus the new files).
- No changes under `src/`.
- ROADMAP item and task checkboxes updated after CI is green.

**Verification:**
```bash
make lint && make typecheck && make test
make integration-test
git diff --name-only development...HEAD   # only expected files changed
```

---

## Summary of Deliverables

| Deliverable | Task | Requirement |
| --- | --- | --- |
| `Dockerfile.integration` | 1 | FR-1 |
| `.dockerignore` (extend if needed) | 1 | AC-1.9 |
| `docker-compose.integration.yml` | 2 | FR-2 |
| `tests/integration/test_mcp_protocol.py` | 3 | FR-3 |
| `pyproject.toml` (register `local_integration` marker) | 3 | AC-3.3, HIGH-2 |
| `.github/workflows/ci.yml` (`local-integration` job) | 4 | FR-4, FR-5 |
| `README.md` (or `docs/`) — branch-protection operator note (`Local Integration Tests`) | 4 | AC-4.8 |
| `Makefile` (`integration-test` target) | 5 | FR-6 |
| Flaky dep / marker (optional) | 6 | AC-3.16 |
| Preflight + ROADMAP update | 7 | All |
