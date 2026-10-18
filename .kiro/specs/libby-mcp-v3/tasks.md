# Tasks: Libby MCP Server v3

## Workflow Policy

**Red-Commit / Green-PR Rule:** Tests-first and implementation land in the SAME
PR. Red (failing tests committed before implementation) is allowed at commit
granularity, never at merge. Every PR must be green before merge — CI enforces
this via required status checks (lint, typecheck, test with 80% coverage).

**Session Contract (see `prompts/agent-router.md`):** branch from `development`,
one ROADMAP item per PR, tick checkboxes before merge, squash-merge to
`development`. Run `make preflight` before every push.

**TDD red-phase convention:** new tests for behavior not yet implemented must not
break collection. Use `pytest.importorskip` for not-yet-existing modules
(`src/oauth/*`, `src/slug_directory.py`, `src/tools/lookup_library_slug.py`); for
new behavior on existing modules, guard with skips or write the test to fail
cleanly (not error on import).

## Dependency Map

- **Group D** (bug fixes) is independent of E and F and can land first. Within D:
  - D1, D3 share `availability.py`; do them together. **D1 makes the single
    `handle()` async and lands the async Thunder helpers
    (`get_availability_async`, `get_library_async`) in PR-D1 so the single path
    compiles under mypy-strict on its own (HIGH-3).**
  - D7 (multi-library `get_availability`) depends on D1 + D3 (the per-title
    shaping and the shared always-async `classify_and_fetch` it fans out) and
    reuses the `search_titles` slug-list helpers. **D7 adds no new Thunder method
    — it reuses D1's async helpers.**
  - D2 (slug normalization) touches the `handler.py` tool wrappers and is a
    prerequisite for D7's normalized dedupe — do D2 before D7.
  - D4 (`is_abridged`) and D6 (`exact`) are isolated to `search_titles.py`.
  - D5 (`lookup_library_slug`) is self-contained (new modules) — can land any time.
- **Group E** (Docker) depends on nothing in D/F for its core (E1 auto-detect, E2
  auth, E3 deliverables), but the `docker-compose.yml` env wiring for OAuth
  (F) is additive and may be finalized with F. Do E after D so the Docker image
  ships the bug-fixed tools.
- **Group F** (OAuth) depends on E1 (the single-codebase ASGI app it mounts
  routes onto) and on the v2 `AuthMiddleware`. Do F last. `jwt.py` (sign + verify)
  is built in **PR-F1 / Task F2**, not PR-F2, because the OAuth-mode auth check
  (F1) and the metadata/registration tests (F2) both depend on token sign/verify
  (HIGH-11); F3's `/authorize` + `/token` then reuse it.

Suggested PR order (serial — each builds on the prior):

1. **PR-D1** — Tasks D1, D2, D3 (availability + slug normalization fixes)
2. **PR-D2** — Tasks D4, D6 (`search_titles` honesty + exact mode)
3. **PR-D3** — Task D5 (`lookup_library_slug` + slug directory cache)
4. **PR-D4** — Task D7 (multi-library `get_availability` fan-out)
5. **PR-E** — Tasks E1, E2, E3, E4 (Docker standalone)
6. **PR-F1** — Tasks F1 + F2, **including `jwt.py` (sign + verify)** (auth modes /
   middleware order, OAuth metadata + `/register`, and the stateless signed-token
   core). `jwt.py` lands here, not in PR-F2, because F1's OAuth-mode auth and F2's
   tests both need token sign/verify (HIGH-11).
7. **PR-F2** — Tasks F3 + F4 (`/authorize` + `/token` flow with owner gate, PKCE,
   refresh rotation, per-client redirect URIs). Task F5 (README + Docker/Lambda
   OAuth wiring) is the final documentation/wiring step and merges with PR-F2.

Each PR is sized ~300–500 lines incl. tests. PR-F1/PR-F2 may be split further if
the OAuth surface grows; keep each a single reviewable unit.

---

## Group D — Bug Fixes

### Task D1: `not_owned` Status in `get_availability` — [x] Done (PR #21)

**Requirements:** FR-D1 (AC-D1.1–D1.9)

**Steps (TDD):**
0. **Root-cause verification first (HIGH-3).** Before writing the classification
   rule, record Thunder's **actual HTTP status and response body** for each of
   these cases against the live/reference API, and note them in the PR:
   - (a) a well-formed-but-unknown numeric `titleId` (expected: `200 OK`, the ID
     simply absent from `items[]`);
   - (b) a **non-numeric** `titleId` such as `bad-title-id` (record whether
     Thunder 4xx's the batch or ignores it);
   - (c) a bad **library slug** on the availability endpoint, and the matching
     `GET /v2/libraries/{slug}` response (to confirm the slug-probe branch).
   Capture (b) and (c) as fixtures so the distinction is pinned by a test, not a
   guess.
1. Extend `tests/fixtures/availability_response.json`: add a **partial `items`
   array** simulating Thunder omitting a not-owned title (request two IDs, `items`
   returns only the owned one — this is the D1 root cause, HIGH-3). Keep an owned
   title and ensure a requested ID **absent** from `items[]` is exercised (the
   not-owned case). Add a case where the only requested ID is not owned. Add the
   **HIGH-3/HIGH-5 status fixtures** in `tests/fixtures/error_responses.json`: a
   **`400` and a `404` from the bulk-availability endpoint** (probe-triggering), a
   **`429` / `401` / `403` / `408`** availability error (non-probed, top-level),
   a **`4xx` from `/v2/libraries/{slug}`** (bad-slug probe), and a **valid**
   `/v2/libraries/{slug}` response (slug-OK branch).
2. Write `tests/test_get_availability.py` cases: an absent ID →
   `{"title_id": tid, "status": "not_owned"}`; a batch of only-absent IDs →
   successful result (no MCP error / no 500); a **non-numeric `title_id`
   (`bad-title-id`)** → `not_owned` **without any Thunder call** (assert the client
   is not invoked for that ID, AC-D1.6); owned IDs still return full data; a
   top-level client error (`5xx`/timeout) still surfaces as `error`; a **400 or
   404 on availability with a valid slug** → IDs classified `not_owned` (AC-D1.7);
   a **400/404 on availability with an invalid slug** (`/v2/libraries/{slug}` also
   4xx) → top-level `error` (`unknown library slug`); **HIGH-5 probe-scope cases:**
   a `429`, `401`, `403`, or `408` on availability is a **top-level `error`
   WITHOUT any slug probe** (assert `get_library_async` is **not** called);
   **HIGH-5 multi-ID retry case:** a `400` on a valid-slug batch of several IDs
   where one is owned and one is unknown → the owned title still returns full data
   and only the unknown ID is `not_owned` (assert owned titles are **not** masked,
   via per-ID retry); **HIGH-4 normalization cases:** an **integer** `title_id`
   (`123`) and a **padded** `" 123 "` are each coerced via `str(tid).strip()`
   before the numeric check (no `AttributeError`) and treated as the numeric ID
   `"123"`; a blank/whitespace-only ID is dropped and never sent to Thunder; **an
   empty `title_ids` list → validation error `title_ids is required (non-empty)`
   on BOTH the single and multi paths** (AC-D1.9). **HIGH-A end-to-end int-ID test
   through the FastMCP app:** add a test that drives the registered
   `get_availability` tool **through the FastMCP app** (a `tools/call` over the
   ASGI app / FastMCP client, as a real client does — not a direct `handle()`
   call) with `title_ids` carrying a **JSON integer** (e.g. `9876543`), and assert
   (a) the request is accepted by the tool schema (the `list[str | int]` parameter
   type does not reject the integer with a validation error) and (b) the integer is
   coerced to the string `"9876543"` in the wrapper before validation, so the
   Thunder call / response uses the string ID. This proves the coercion happens at
   the FastMCP tool boundary, not only inside `handle()`.
3. **Thunder client changes (HIGH-3) — these async helpers land in THIS task
   (D1/PR-D1), not D7.** In `src/thunder_client.py`: (a) make the
   `HTTPStatusError` branch of `_execute_get` and `_execute_get_async` include the
   numeric HTTP `status` in the returned error dict (e.g.
   `{"error": ..., "status": 400}`) so callers can distinguish 400/404 from
   429/401/403/408 and 5xx (timeouts keep no `status`); (b) add
   **`get_availability_async(slug, title_ids, async_client)`** (the single path's
   `handle()` is async and awaits it — HIGH-3) **and** `get_library_async(slug,
   async_client)` (the slug probe), both calling the async client; a sync
   `get_library(slug)` mirror is optional and unused by the availability layer.
4. **Declare the MCP tool parameter as `title_ids: list[str | int]` (HIGH-A).**
   The registered `get_availability` FastMCP tool wrapper declares `title_ids:
   list[str | int]` (not `list[str]`), so a client sending JSON integer IDs is
   accepted by the FastMCP/Pydantic schema. The int→str coercion
   (`str(tid).strip()`) MUST run **inside the FastMCP tool wrapper as its first
   step** — the wrapper calls `_normalize_title_ids(title_ids)` at the top, before
   any one-of/slug validation and before dispatch to `handle()`/`handle_multi()` —
   so an integer ID is already a stripped numeric string by the time any validation
   or Thunder call sees it. Then implement the classification in a **shared,
   always-async `classify_and_fetch(slug, title_ids, client, async_client)`** (see
   Task D7 / HIGH-4) used by both the single and multi paths of
   `src/tools/availability.py`.
   Make the single-library **`handle()` an `async def`** that creates a short-lived
   `httpx.AsyncClient` and `await`s `classify_and_fetch` (HIGH-3), and make the
   registered `get_availability` tool wrapper `async def` from this PR. The
   classifier **re-applies the same `_normalize_title_ids` defensively** (so a
   direct `handle()` call is still safe), but the FastMCP wrapper is the first and
   authoritative coercion point. The classifier: **first normalizes each ID with
   `str(tid).strip()` before any `isinstance`/`isdigit` check and drops
   blank-after-strip entries (HIGH-4), rejecting an empty-after-normalize
   `title_ids` with `title_ids is required (non-empty)`**; then filters non-numeric
   IDs (`str(tid).strip().isdigit()`) out
   of the `titleIds` query and emits them as `not_owned` up front (AC-D1.6) — if no
   numeric IDs remain, make **no** Thunder call and return all IDs as `not_owned`;
   maps absent-from-`items` IDs to `{"title_id": tid, "status": "not_owned"}`; on a
   **`400`/`404` only** (`error` dict whose `status` is 400 or 404) from the
   availability call, `await`s `get_library_async(slug)` to split bad-slug (→
   top-level `error`) from valid-slug — on a valid slug with a **single** ID →
   `not_owned`, with **multiple** IDs → **retry each ID individually**
   (`asyncio.gather`) so owned titles survive (HIGH-5); keeps
   `429`/`401`/`403`/`408`, `5xx`, and timeout (status not in {400,404}) as a
   top-level `error` **without probing**; removes the legacy
   `{"error": "not_found"}` per-title shape.
5. Run `make test`; green. Confirm PR-D1 type-checks under mypy-strict with the
   async `handle()`/helpers self-contained (no import of anything D7 adds — HIGH-3).

**Acceptance:** AC-D1.1–D1.9.

### Task D2: Normalize Library Slugs on Input — [x] Done (PR #21)

**Requirements:** FR-D2 (AC-D2.1–D2.5)

**Steps (TDD):**
1. Write tests: `search_titles('LCPL', ...)` → `library_slug == "lcpl"` and link
   host lowercase; `' lcpl '` → `'lcpl'`; `['LCPL','lcpl']` dedupes to one
   `lcpl`; empty-after-normalize slug hits the existing validation message.
2. Implement a `normalize_slug(slug) -> slug.strip().lower()` helper (e.g.
   `src/tools/_slugs.py`) and apply it in the `handler.py` tool wrappers before
   building `args` and **before** dedupe/validation; apply to each entry of
   `library_slugs` and to the `get_availability` slug(s) and `lookup_library_slug`.
3. Confirm `_normalize_item` link host is lowercase as a consequence (no change
   needed if the slug is normalized upstream).
4. Run `make test`; green. Confirm `normalize_slug` has no per-slug special-casing
   (NFR-6.1).

**Acceptance:** AC-D2.1–D2.5.

### Task D3: `estimated_wait_days == 0` When Available — [x] Done (PR #21)

**Requirements:** FR-D3 (AC-D3.1–D3.4)

**Steps (TDD):**
1. Add an availability fixture item with `availableCopies > 0` **and** a nonzero
   raw `estimatedWaitDays` (to prove the correction) plus one with
   `availableCopies == 0` and a nonzero wait (pass-through).
2. Write tests: available title → `estimated_wait_days == 0`; not-available →
   raw value; in `search_titles` fold-in, available → `0`, not-available →
   raw-or-`None` (v2 unknown-is-`None` preserved).
3. Implement the `0 if available > 0 else raw` rule in `availability.handle`
   (per-title) and in `search_titles._normalize_item` (fold-in), keying on
   `copies_available` / `available_copies`.
4. Run `make test`; green.

**Acceptance:** AC-D3.1–D3.4.

**PR-D1 exit:** `make preflight` green; checkboxes ticked; squash-merge.

### Task D4: Honest `is_abridged` — [x] Done (PR #23)

**Requirements:** FR-D4 (AC-D4.1–D4.4)

**Steps (TDD):**
1. **Verify first:** check `docs/references/thunder_api_reference.md`,
   `thunder_openapi_v1.json`, and (if available) a captured live `/media` item for
   a reliable abridged signal. Record the finding in the PR.
2. Add `search_response.json` fixture items: one with an edition string containing
   "Unabridged", one with "Abridged", one with neither / no edition, and (if the
   source exists) a format-entry `isAbridged`.
3. Write tests: "Unabridged" → `False`; "Abridged" → `True`; absent → `None`
   (never `False`); assert `is_abridged` is not a constant across the fixture set.
   If the drop-the-field branch is taken, assert `is_abridged` is absent from the
   title schema instead.
4. Implement `_derive_abridged(primary_fmt, edition)` (edition-else-`None`) in
   `_normalize_item`, OR remove the field per AC-D4.3 — apply consistently to
   single + multi responses.
5. Run `make test`; green.

**Acceptance:** AC-D4.1–D4.4.

### Task D6: Exact-Title Search Mode — [x] Done (PR #23)

**Requirements:** FR-D6 (AC-D6.1–D6.6)

**Steps (TDD):**
1. Add fixture items where several titles partially match a query and one matches
   exactly (case-insensitively), **including a case where the exact match only
   appears on page 2** of the Thunder results (HIGH-12).
2. Write tests: `exact=true` + `query` returns only the exact-match title (single
   and multi-library); `exact=true` without `query` → ToolError `exact requires
   query`; `exact=false`/omitted preserves v2 fuzzy behavior;
   `search_titles('The Mom Test', exact=true)` returns only "The Mom Test"; an
   exact title that only appears on page 2 is still returned (AC-D6.7).
3. Implement: add `exact: bool = False` to the `search_titles` tool signature in
   `handler.py`; validate `exact requires query` in the wrapper; when
   `exact=true`, request `perPage=100` + `sort=relevance` (drop `availableFirst`)
   and apply the `title.strip().lower() == query.strip().lower()` filter in
   `_build_result_block`; if the first page yields no match while `total_items > 0`,
   fetch further pages up to a 3-page / 300-item cap and re-filter (HIGH-12). The
   filter covers both single and multi-library paths.
4. Run `make test`; green.

**Acceptance:** AC-D6.1–D6.7.

**PR-D2 exit:** `make preflight` green; checkboxes ticked; squash-merge.

### Task D5: `lookup_library_slug` Tool + Slug Directory Cache — [x] Done (PR #24)

**Requirements:** FR-D5 (AC-D5.1–D5.8), NFR-2.2, NFR-3.3, NFR-5.1

**Steps (TDD):**
1. Add `tests/fixtures/libraries_response.json` modeling a paginated
   `/v2/libraries` response (`items` of `LibraryResponse`-shaped rows with
   `preferredKey`/`id`, `name`, and `links.last`).
2. Write `tests/test_lookup_library_slug.py` (importorskip the new modules):
   `lookup_library_slug('Fairfax')` returns matches whose slugs include `fairfax`;
   partial/case-insensitive match on name and slug; no-match → empty `matches`
   list (not an error); cache hit → a second lookup within TTL does not re-fetch
   (assert the client is called once); concurrent callers during a cold window
   trigger a single in-flight fetch (AC-D5.9); directory-fetch failure with no
   cache → serves the bundled `fallback_slugs.json` with a `warning` (AC-D5.10);
   with a stale cache → serves the stale rows.
3. Implement `ThunderClient.list_libraries(page, per_page)`; the directory loader
   fetches page 1 first (read `totalItems`), computes the page count, then fetches
   the remaining pages concurrently with `asyncio.gather` (HIGH-7);
   `src/slug_directory.py` (`SlugDirectory` with 7-day TTL, module-level reuse, a
   module-level `asyncio.Lock` so concurrent callers share one in-flight fetch,
   serve-stale-on-failure, and cold-start fallback to a bundled
   `src/fallback_slugs.json` with a `warning`); `src/tools/lookup_library_slug.py`
   (`async handle` with case-insensitive substring match, raw data only). Add a
   `src/fallback_slugs.json` fixture of the top-100 libraries. Assert in a test
   (or `make integration`) that the live fetch completes in under 10s (AC-D5.5)
   and that concurrent callers trigger a single fetch (AC-D5.9).
4. Register `lookup_library_slug` in `handler.py` (normalize the `name` input;
   module-level `SlugDirectory`); add a handler test that it appears in the tool
   set and the other two tools still work.
5. Run `make test`; green.

**Acceptance:** AC-D5.1–D5.10.

**PR-D3 exit:** `make preflight` green; README tool list updated to three tools;
checkboxes ticked; squash-merge.

### Task D7: Multi-Library `get_availability` Fan-Out — [x] Done (PR #25)

**Requirements:** FR-D7 (AC-D7.1–D7.7), NFR-2.1, NFR-3.1, NFR-7.1

**Steps (TDD):**
1. Write tests: one-of validation (both → error; neither → error) with the same
   messages as `search_titles`; `library_slugs` validation reuse (empty, blank
   entry, dedupe after normalization, >10); `get_availability([ids],
   ['lcpl','fcpl'])` → `{libraries: [block, block]}` each tagged with its
   `library_slug`; per-library failure (one slug 404) isolates to that block;
   single `library_slug` keeps the flat v2 shape (+ D1/D3); **a multi-library
   request whose `title_ids` includes a non-numeric ID (e.g. `bad-title-id`)
   classifies that ID `not_owned` in each library's block without a Thunder call
   for it (HIGH-4) — the same behavior as the single-library path**; **a
   multi-library request with an integer / padded `title_id` is normalized via
   `str(tid).strip()` identically to the single path, and an empty `title_ids`
   list is rejected (`title_ids is required (non-empty)`) before fan-out (HIGH-4,
   AC-D1.8/D1.9)**; **a multi-library valid-slug `400` on a multi-ID batch retries
   per ID so owned titles survive in each block (HIGH-5)**; an end-to-end test
   driving the async tool through the FastMCP app (running event loop, as Mangum
   does), not just `handle_multi` in isolation.
2. Reuse the **D1** async helpers — `ThunderClient.get_availability_async` and
   `get_library_async` already shipped in Task D1 (HIGH-3); D7 adds **no** new
   Thunder method. Route BOTH the single and multi paths through the shared,
   always-async **`classify_and_fetch(slug, title_ids, client, async_client)`**
   helper (HIGH-4) so the non-numeric filtering, no-call-when-empty, the
   `400/404`-only slug probe, and the multi-ID per-ID retry (HIGH-5) cannot
   diverge between paths; `availability.handle_multi` wraps `_availability_one`
   (which calls `classify_and_fetch`) in an `asyncio.gather` fan-out creating the
   `AsyncClient` per fan-out (bound to the current loop) and catching/normalizing
   per-library failures.
3. Extend the already-async (from D1) `get_availability` tool wrapper in
   `handler.py`: make `library_slug: Optional[str] = None`, add `library_slugs:
   Optional[list[str]] = None`; normalize slugs; run one-of + slug-list validation
   in the wrapper before dispatch (reuse `validate_library_slugs` /
   `dedupe_library_slugs` from `search_titles` or the shared `_slugs` util);
   `await handle_multi(...)` for multi, `await handle(...)` (now async per HIGH-3)
   for single.
4. Run `make test`; green.

**Acceptance:** AC-D7.1–D7.7.

**PR-D4 exit:** `make preflight` green; checkboxes ticked; squash-merge.

---

## Group E — Docker Standalone (PR-E)

### Task E1: Single-Codebase Auto-Detect Entry Point — [x] Done (PR #26)

**Requirements:** FR-E1 (AC-E1.1–E1.5), FR-E4 (AC-E4.3)

**Steps (TDD):**
1. Write tests: `AWS_LAMBDA_RUNTIME_API` present → `lambda_handler` uses the
   Mangum path (mock Mangum / assert the branch); absent → `_build_app()` returns
   an ASGI app usable by a test ASGI client against `/mcp`; `_build_app` and
   `lambda_handler` remain importable (the local-integration harness depends on
   `_build_app`).
2. Implement `_IS_LAMBDA = "AWS_LAMBDA_RUNTIME_API" in os.environ` in `handler.py`;
   keep `_build_app()` and `lambda_handler` signatures unchanged; do NOT create a
   second handler module (assert single-handler in compliance).
3. Run `make test`; green.

**Acceptance:** AC-E1.1–E1.5, AC-E4.3.

### Task E2: Docker Auth Behavior — [x] Done (PR #26)

**Requirements:** FR-E2 (AC-E2.1–E2.5)

**Steps (TDD):**
1. Write tests (ASGI client, no container needed): key set → `POST /mcp` without
   Bearer → HTTP 401, with correct Bearer → HTTP 200 MCP initialize; `GET /mcp`
   → HTTP 405 regardless of auth; key unset (Docker, no `AWS_LAMBDA_RUNTIME_API`)
   → `POST /mcp` served unauthenticated. The `initialize` request body MUST carry
   valid `InitializeRequestParams` (`protocolVersion`, `capabilities`,
   `clientInfo`) and the request MUST send `Accept: application/json,
   text/event-stream` (HIGH-2) — assert that `params:{}` returns JSON-RPC -32602.
   These exercise the existing `AuthMiddleware` with no Docker-specific fork.
   (No-auth is Docker-only; the Lambda fail-closed case is tested in Task F1 —
   HIGH-13 / AC-F1.1-lambda.)
2. No new code if `AuthMiddleware` already covers it; otherwise adjust so the no-
   auth (unset key) path is reachable (it already is in the three-mode design, but
   confirm the static path is unchanged).
3. Run `make test`; green.

**Acceptance:** AC-E2.1–E2.5.

### Task E3: Docker Deliverables — [x] Done (PR #26)

**Requirements:** FR-E3 (AC-E3.1–E3.7)

**Steps:**
1. Add a root `Dockerfile`: multi-stage (`builder` venv + `runtime`), non-root
   `appuser`, **`python:3.12-slim`** (matches the Lambda runtime and
   `Dockerfile.integration`, HIGH-11), installs `requirements.txt` +
   `uvicorn[standard]`. The `CMD` MUST use **exec form** (JSON array), e.g.
   `CMD ["uvicorn", "src.handler:_build_app", "--factory", "--host", "0.0.0.0",
   "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips",
   "*"]` (proxy flags for HIGH-10). Any explanatory comment MUST sit on its own
   line **above** the `CMD` instruction, never inline on the `CMD` line (HIGH-1).
2. Add a root `docker-compose.yml`: builds the `Dockerfile`, binds **loopback
   only** `127.0.0.1:8000:8000` by default (HIGH-6 — auto-approve must never be
   reachable on a published port), passes `LIBBY_MCP_API_KEY` (+ OAuth env
   placeholders) **without passing empty strings for unset optional vars** (use
   per-key defaults / conditional inclusion, not `${VAR:-}` which injects `""`;
   see HIGH-2), stdlib-urllib healthcheck accepting 401/405 as "up".
3. Update `.dockerignore` to exclude `.git`, `infra`, `.venv`, `__pycache__`,
   `*.pyc`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `cdk.out`,
   `node_modules`, `tests`, `docs`.
4. Add a README Docker section: `docker compose up` quickstart; Claude Desktop +
   Cursor static-header connector config examples pointing at
   `http://localhost:8000/mcp`.
5. Add a Docker/local-integration test (or extend the existing containerized MCP
   protocol suite) asserting (HIGH-1/HIGH-2): `POST /mcp` with a valid key and a
   valid `initialize` body (`protocolVersion`/`capabilities`/`clientInfo`) +
   `Accept: application/json, text/event-stream` → HTTP 200 initialize; `POST
   /mcp` without a key when a key is set → HTTP 401; `GET /mcp` → HTTP 405
   regardless of auth; and <30s readiness. Use the exact AC-E2.3 curl
   (valid params + Accept header), not a bare `params:{}`.
6. **Empty-string ≡ unset test (HIGH-2 / NFR-8.3).** Add a test that builds the
   app in Docker mode (`AWS_LAMBDA_RUNTIME_API` absent) **with every optional env
   var explicitly set to the empty string** (`LIBBY_MCP_API_KEY=""`,
   `LIBBY_MCP_OAUTH_ENABLED=""`, `LIBBY_MCP_OAUTH_SIGNING_KEY=""`,
   `LIBBY_MCP_OAUTH_OWNER_SECRET=""`, `LIBBY_MCP_PUBLIC_BASE_URL=""`,
   `LIBBY_MCP_SLUG_TTL_SECONDS=""`, the three TTL vars `""`) and asserts it
   behaves **identically to the all-unset case**: `_build_app()` succeeds (no
   signing-key/owner-secret startup refusal because OAuth reads as disabled),
   `POST /mcp` is served unauthenticated, and the slug TTL falls back to its
   7-day default. This pins the `_env()` normalization (F0) so an empty string
   can never silently enable or misconfigure a mode. Also assert the companion
   case: with the optional vars **unset** the behavior is the same.
7. Run `make test` (+ `make integration-test` if Docker available locally); green.

**Acceptance:** AC-E3.1–E3.7, AC-E4.3 (uvicorn Docker-only), NFR-8.1–NFR-8.3 (HIGH-2).

### Task E4: Lambda Deploy Unchanged — [x] Done (PR #26)

**Requirements:** FR-E4 (AC-E4.1–E4.4), NFR-6.2, NFR-6.3

**Steps:**
1. Verify (and test where feasible) that `infra/libby-mcp-stack.ts` is unchanged
   in behavior and that `cd infra && npx tsc --noEmit` passes.
2. Add any OAuth env vars to the stack as **optional** parameters defaulting to
   disabled (so a no-OAuth deploy is identical to v2); keep the stack cloud-
   agnostic (no owner-specific account IDs/ARNs/regions).
3. **Add the OAuth API Gateway routes (CRITICAL-1):** when
   `LIBBY_MCP_OAUTH_ENABLED=true`, add HTTP API routes — all bound to the same
   Lambda integration as `/mcp` — for `GET /.well-known/oauth-protected-resource`,
   `GET /.well-known/oauth-authorization-server`, `POST /register`,
   `GET /authorize`, `POST /authorize`, and `POST /token`. When OAuth is disabled,
   add none of them (stack identical to v2). Add an `infra/` test (or
   `cdk synth` assertion) that these six routes are present when OAuth is enabled
   and absent when disabled. **The `cdk synth` route assertions are owned by this
   task (E4); the live `GET /.well-known/oauth-protected-resource` → HTTP 200
   check (AC-E4.6) is NOT ticked here** — it requires a deployed, OAuth-enabled
   Lambda and is owned by Task F5 / PR-F2 as an operator-pending check (HIGH-8),
   so a Group-E PR's CI never has to stand up a live OAuth deploy to go green.
4. **Set `LIBBY_MCP_PUBLIC_BASE_URL` on the Lambda (HIGH-10).** Nothing in v2 sets
   this env var, so with OAuth enabled every OAuth route would hit the
   non-loopback guard and return HTTP 500 `oauth_not_configured`. When OAuth is
   enabled, the CDK stack MUST set the Lambda's `LIBBY_MCP_PUBLIC_BASE_URL`
   environment variable to the deployed base URL — by default
   `httpApi.apiEndpoint` (the `https://<id>.execute-api.<region>.amazonaws.com`
   origin; the `/mcp` path is **not** included in the base URL), with an optional
   custom-domain override read from a CDK **context** variable (e.g.
   `-c publicBaseUrl=https://mcp.example.com`, falling back to
   `httpApi.apiEndpoint` when the context value is absent). Also provision the
   OAuth signing key in Secrets Manager with explicit, non-ambiguous settings
   (HIGH-9): `generateSecretString: { passwordLength: 64, excludePunctuation: true,
   requireEachIncludedType: false }`, and inject it as
   `LIBBY_MCP_OAUTH_SIGNING_KEY`. Add a **`cdk synth` check step**: assert the
   synthesized template sets `LIBBY_MCP_PUBLIC_BASE_URL` on the Lambda when OAuth
   is enabled (and does not when disabled), and that the custom-domain context
   override is honored.
5. Confirm no new mandatory Lambda dependency entered `requirements.txt` (OAuth
   JWT is stdlib-only, HIGH-4).
6. **Keep the Lambda runtime on Python 3.12 (HIGH-11).** The real CDK stack
   (`infra/lib/libby-mcp-stack.ts`) already uses `lambda.Runtime.PYTHON_3_12`;
   do **not** downgrade it (a downgrade would be a breaking change contradicting
   AC-E4.1 "no behavioral change" and NFR-6.3). NFR-6.4 is updated to "minimum
   3.11, runtime 3.12". No step pins the runtime to 3.11.
7. Grep `src/` and `infra/` for hardcoded `lcpl`/`fairfax`/`fcpl` behavioral
   branches (NFR-6.1) and owner-specific constants (NFR-6.2).

**Acceptance:** AC-E4.1–E4.5, AC-E4.7 (the `cdk synth` route + base-URL assertions). **AC-E4.6 is owned by Task F5 / PR-F2 (operator-pending live-deploy check), not here (HIGH-8).**

**PR-E exit:** `make preflight` green; `npx tsc --noEmit` in `infra/` green;
checkboxes ticked; squash-merge.

---

## Group F — OAuth 2.1 (PR-F1, PR-F2)

> Embedded minimal, **stateless** authorization server is the default (no Auth0
> dependency, no server-side store). Implement the exact endpoint shapes and
> redirect URIs from the brief / requirements. OAuth JWT is implemented with the
> Python standard library only (`hmac`, `hashlib`, `base64`, `json`) — **no new
> package** is added to `requirements.txt` (HIGH-4).

### Task F1: Three Auth Modes + Mode Selection

**Requirements:** FR-F1 (AC-F1.1–F1.5, AC-F1.1-lambda), FR-F3 (AC-F3.1)

**Steps (TDD):**
1. Write tests: no-auth mode (both env unset, Docker) → `/mcp` passes through;
   static-Bearer accept/reject; OAuth mode → valid JWT accepted, invalid/absent →
   401; static+OAuth coexist (static key OR JWT accepted); OAuth-mode 401 carries
   `WWW-Authenticate: Bearer resource_metadata="…"`, other modes do not;
   **Lambda fail-closed (HIGH-13):** `AWS_LAMBDA_RUNTIME_API` set + no key + no
   OAuth → `/mcp` returns HTTP 500 `authentication_not_configured` on first
   request (not at import).
2. Implement the mode-selection + composed `_authorized` in `src/auth.py`
   (constant-time static compare first, then JWT if OAuth enabled); keep the
   POST-only guard and `/mcp` scoping; let OAuth public paths bypass the gate;
   add the Lambda fail-closed check (first-request, logged).
3. Document the middleware order (HIGH-1): (1) non-http (lifespan) passes through;
   (2) OAuth-public paths bypass auth entirely when OAuth is on; (3) non-POST on
   `/mcp` → 405; (4) auth check on `/mcp`.
4. Run `make test`; green.

**Acceptance:** AC-F1.1–F1.5, AC-F1.1-lambda, AC-F3.1. (AC-F1.6 and AC-F1.8/F1.9
— the `/authorize` owner-consent gate and the no-silent-auto-approve startup
rules — land in Task F2/F3; AC-F1.7 — the README mode table — lands in Task F5.
PR-F1 does not claim ACs whose implementation ships in a later task.)

### Task F2: OAuth Metadata + Endpoint Scaffolding

**Requirements:** FR-F1 (AC-F1.8), FR-F2 (AC-F2.1–F2.3, F2.7–F2.10), FR-F3 (AC-F3.2–F3.3), FR-F5 (AC-F5.1), FR-F6 (AC-F6.1–F6.6, AC-CRITICAL-2), FR-F10 (AC-F10.1–F10.2), FR-F11 (AC-F11.1–F11.2, and the `aud` definition used by F11.5/F11.6)

**Steps (TDD):**
1. Write tests (importorskip `src/oauth/*`): `GET /.well-known/oauth-protected-
   resource` returns an RFC 9728 doc whose `authorization_servers[0]` equals the
   AS `issuer` **and whose `resource` equals the canonical `<base>/mcp`
   (HIGH-7 / AC-F11.1)**; a `_canonical_resource` / `_resolve_resource` unit test
   covers trailing-slash normalization (`<base>/mcp` == `<base>/mcp/`, AC-F11.2);
   `GET /.well-known/oauth-authorization-server` returns metadata
   with `code_challenge_methods_supported == ["S256"]`, grants
   `["authorization_code","refresh_token"]` (no `client_credentials`),
   `token_endpoint_auth_methods_supported == ["none"]`; `POST /register` returns
   **HTTP 201** with a public-client body (`client_id`, `client_id_issued_at`,
   `redirect_uris`, `grant_types`, `response_types`,
   `token_endpoint_auth_method: "none"`, **no `client_secret`** — HIGH-8), the
   `client_id` embedding the redirect URI(s), and a bad `redirect_uri` → HTTP 400
   `invalid_redirect_uri`; both metadata URLs are built from
   `LIBBY_MCP_PUBLIC_BASE_URL`; non-loopback base URL
   must be `https` (else `oauth_not_configured`), loopback exempt; **jwt.py
   (HIGH-5 / HIGH-11):** sign + verify round-trips (stdlib HS256), a token signed
   with one key fails verification under a different key (CRITICAL-2);
   `verify(token, expected_typ)` **rejects a token whose `typ` does not match**
   (a `refresh`/`code` token fails `expected_typ="access"`, and vice-versa), a
   token whose `iss` does not match is rejected, and a token whose header `alg` is
   `none` or not `HS256` is rejected before the HMAC is computed; all comparisons
   use `hmac.compare_digest`; and `_build_app()` refuses to start when OAuth is
   enabled but `LIBBY_MCP_OAUTH_SIGNING_KEY` is unset (AC-F6.2) **or** the
   effective owner secret is unset/empty (AC-F1.8, CRITICAL-1). TTLs are read from
   `LIBBY_MCP_OAUTH_{ACCESS,REFRESH,CODE}_TTL_SECONDS` (defaults 3600/2592000/60)
   and a test asserts each default and that overriding the env changes `exp`
   (HIGH-6). **Signing key (HIGH-9):** the HMAC key is
   `hashlib.sha256(secret.encode()).digest()` (one decode path, no hex/base64
   rule); a test asserts the server refuses to start when the secret is under 32
   bytes and that two secrets differing only past the SHA-256 input produce
   different keys.
2. Implement `src/oauth/metadata.py` (including the canonical-resource helpers
   `_canonical_resource(base) -> f"{base.rstrip('/')}/mcp"` and
   `_resolve_resource(sent, base)` with trailing-slash normalization — HIGH-7; the
   PRM `resource` field is the canonical value), `registration.py` (signed
   stateless **public** `client_id`, no `client_secret`, no store — HIGH-8),
   `jwt.py` (stdlib HS256 **sign + verify** — `hmac`/`hashlib`/`base64`/`json`
   only, no PyJWT; every artifact carries `typ` + `iss`; `verify(token,
   expected_typ, expected_iss)` pins `alg=HS256`, rejects wrong `typ`/`iss`, uses
   `hmac.compare_digest`; access/refresh tokens carry `aud == canonical_resource`
   and verification on `/mcp` checks it — HIGH-7; TTLs from env with
   3600/2592000/60 defaults — HIGH-5 / HIGH-6), and `router.py`; mount the router
   in `_build_app()` when
   `LIBBY_MCP_OAUTH_ENABLED=true` and wire **both** the signing-key startup check
   (AC-F6.2) and the owner-secret startup check (AC-F1.8, CRITICAL-1). **No
   `store.py`** — the AS is stateless (CRITICAL-2).
3. Confirm **no new package** is added to `requirements.txt` or `pyproject.toml`
   for OAuth JWT (stdlib-only, HIGH-4 / AC-E4.3 / AC-F6.4).
4. **Trusted base URL (HIGH-10):** build all generated URLs (both `/.well-known`,
   `issuer`, `resource`, `resource_metadata`) from `LIBBY_MCP_PUBLIC_BASE_URL`;
   use the Host header only when the resolved base is loopback; when the base is
   unset and the Host is non-loopback, the OAuth routes return HTTP 500
   `oauth_not_configured`. Add tests for AC-F10.1/F10.2.
5. Run `make test`; green.

**Acceptance:** AC-F2.1–F2.3, F2.7, **F2.8** and the **signed-token/TTL-default halves** of **F2.9 + F2.10** (JWT structure, `typ`, bound `resource`, the three TTL defaults — the `/token` exchange/rotation halves land in F3, HIGH-8), F3.2–F3.3, F5.1, F6.1–F6.4, **F6.6**, the **jwt.py verify half of F6.5** (reject wrong `typ`/`iss`, pin `alg=HS256`), F10.1–F10.2, **F11.1–F11.2** and the canonical `aud` definition (HIGH-7; the `/authorize`+`/token` `invalid_target` enforcement — F11.3/F11.4 — lands in F3/F4), AC-CRITICAL-2, AC-F1.8.

**PR-F1 exit:** `make preflight` green; checkboxes ticked; squash-merge.

### Task F3: Authorize + Token (PKCE, Refresh Rotation)

**Requirements:** FR-F1 (AC-F1.6, AC-F1.9, AC-F1.10, AC-F1.11), FR-F2 (AC-F2.4–F2.6, AC-F2.11, AC-F2.12), FR-F5 (AC-F5.2–F5.3), FR-F6 (AC-F6.5–F6.6), FR-F11 (AC-F11.3–F11.6 — `invalid_target` on non-canonical `resource`, missing-defaults-canonical, canonical `aud` on issued access+refresh tokens, `/mcp` `aud` check)

**Steps (TDD):**
1. Write tests: `/authorize` rejects missing `code_challenge` or
   `code_challenge_method != S256`; **owner gate (CRITICAL-3):** `POST /authorize`
   with no/invalid owner secret → HTTP 401 before any code is issued (AC-F1.6),
   correct secret → mints a code; **no-silent-auto-approve (CRITICAL-1, AC-F1.9):**
   `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` is honored only when NOT on Lambda
   (`AWS_LAMBDA_RUNTIME_API` absent) AND `LIBBY_MCP_PUBLIC_BASE_URL` is
   **explicitly set to a loopback origin** (`127.0.0.1`/`localhost`/`::1`) —
   test that the flag is refused when `AWS_LAMBDA_RUNTIME_API` is set, refused when
   the base URL is non-loopback, and allowed (code minted without the secret, with
   a warning) only when both conditions hold; **HIGH-6 refuse-to-start:** the flag
   set with `LIBBY_MCP_PUBLIC_BASE_URL` **unset/empty** → `_build_app()` refuses to
   start naming the base URL as required (AC-F1.10), i.e. no implicit loopback
   default enables auto-approve; **HIGH-6 published-port doc:** assert the shipped
   `docker-compose.yml` binds `127.0.0.1:8000:8000` and the README documents that
   auto-approve must never run behind a published port (AC-F1.11); **redirect URI (HIGH-9):** unregistered/mismatched
   `redirect_uri` → HTTP 400 error page, not a redirect (AC-F9.1); valid request
   redirects with a `code` (+ `state`) to the exact registered redirect URI;
   `/token` `authorization_code` exchange verifies `S256(verifier) == challenge`
   (challenge read from the signed 60s code JWT), issues a JWT access token +
   refresh token, rejects a mismatched verifier (`invalid_grant`); `/token`
   `grant_type=refresh_token` rotates the refresh JWT and issues a new access
   token; expired/unknown refresh token → `invalid_grant`; **canonical resource
   (HIGH-7, AC-F11.3–F11.6):** `/authorize` and `/token` with a `resource` equal to
   the canonical `<base>/mcp` (or with a single trailing slash) are accepted; a
   **missing** `resource` defaults to canonical (not an error); a **non-canonical**
   `resource` → `invalid_target` (`400` on `/token`; `400`/`invalid_target` on
   `/authorize`, before any code/token is issued); the issued access **and**
   refresh tokens carry `aud == <base>/mcp` regardless of the client-sent value;
   `/mcp` rejects (401) a token whose `aud` ≠ canonical; `/mcp` JWT validation
   checks signature/exp/`aud`/`typ`; **token-swap rejections (HIGH-5):** a
   refresh token and an authorization code are each rejected when presented as a
   Bearer on `/mcp` (401), and an access token is rejected when replayed at
   `/token` as a refresh/code grant (`invalid_grant`); a token with `alg: none` or
   a non-`HS256` alg is rejected; **stateless (CRITICAL-2):** a token signed with
   a key validates under a second verifier built with the same key and fails under
   a different key; the authorization code expires after its configured TTL (60s
   default); **consent-page XSS escaping (HIGH-C, AC-F2.11):** register a client
   via `POST /register` whose `client_name` is `<script>alert(1)</script>` (the
   attacker-controlled DCR field, embedded in the signed `client_id`), drive
   `GET /authorize` with that `client_id`, and assert the rendered consent HTML
   contains the **escaped** form (`&lt;script&gt;`) and **no** live `<script>` tag
   — the attacker-controlled name is rendered inert, not executed; also assert
   `state` and `redirect_uri` reflected into the hidden inputs are HTML-escaped;
   **consent-page security headers (HIGH-C, AC-F2.12):** assert the `GET /authorize`
   response carries `Content-Security-Policy: frame-ancestors 'none'` and
   `Cache-Control: no-store`.
2. Implement `src/oauth/authorize.py` (owner consent gate, exact redirect-URI
   match, `_resolve_resource(sent, base)` so a non-canonical `resource` →
   `invalid_target` and a missing one defaults to canonical — HIGH-7 — before a
   signed `typ:"code"` code at the configured code TTL binds the **canonical**
   `resource`) and `token.py` (re-validate any `/token` `resource` the same way,
   and set `aud` on the access **and** refresh tokens to the canonical resource —
   AC-F11.5)
   (stateless code/refresh verification via `verify(..., expected_typ=...)` so a
   swapped artifact is rejected — HIGH-5 — and refresh rotation), **reusing the
   `jwt.py` sign/verify helpers built in PR-F1 / Task F2** (HIGH-11 — no
   per-request introspection). **Render the consent page safely (HIGH-C):** pass
   every reflected value (`state`, `client_name`, `redirect_uri`, `client_id`,
   `code_challenge`, `resource`, and any other request/registration-sourced field)
   through `html.escape(value, quote=True)` before interpolating it into element
   text or a hidden-input `value="…"` attribute — never concatenate a reflected
   value raw — and set `Content-Security-Policy: frame-ancestors 'none'` and
   `Cache-Control: no-store` on the `GET /authorize` HTML response (and on any
   `POST /authorize` HTML error page). `client_name` is attacker-controlled (public
   DCR), so this is the primary XSS sink. The owner secret is compared with
   `hmac.compare_digest` and resolves to `LIBBY_MCP_OAUTH_OWNER_SECRET` else
   `LIBBY_MCP_API_KEY`; the dev auto-approve path is reachable **only** when
   `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` **and** not on Lambda **and**
   `LIBBY_MCP_PUBLIC_BASE_URL` is **explicitly set to a loopback origin** (HIGH-6,
   emits a startup warning); when the flag is set but the base URL is unset/empty,
   `_build_app()` **refuses to start** (AC-F1.10) rather than defaulting to
   loopback; otherwise the owner secret is required (startup already failed if
   unset, AC-F1.8).
3. Run `make test`; green.

**Acceptance:** AC-F1.6, AC-F1.9, AC-F1.10, AC-F1.11, AC-F2.4–F2.6, **AC-F2.11 (consent-page HTML-escaping / `<script>` client_name test)**, **AC-F2.12 (consent-page `Content-Security-Policy: frame-ancestors 'none'` + `Cache-Control: no-store` headers)**, the **`/token` code-exchange / refresh-rotation halves of AC-F2.9 + AC-F2.10** (consuming a code within its TTL and issuing/rotating refresh+access tokens — the signed-token *structure* + TTL defaults are owned by F2, HIGH-8), the **`/token`-grant token-swap half of AC-F6.5** (reject an access token replayed as a refresh/code grant, and a code/refresh presented as a `/mcp` Bearer — the jwt.py verify half is owned by F2), AC-F9.1, **AC-F11.3–F11.6** (`invalid_target` enforcement + missing-defaults-canonical + canonical `aud` on issued tokens + `/mcp` `aud` check, HIGH-7), AC-CRITICAL-2, F5.2–F5.3. (AC-F6.6 — constant-time comparisons — is owned by F2; F3 reuses `hmac.compare_digest` but does not own that AC.)

### Task F4: Per-Client Compatibility

**Requirements:** FR-F4 (AC-F4.1–F4.5), FR-F9 (AC-F9.2–F9.3) — AC-F9.1 (the `/authorize` exact-match / 400-not-redirect rule) is owned by Task F3; F4 owns the registration-time scheme rules.

**Steps (TDD):**
1. Write tests: DCR accepts Claude.ai (`https://claude.ai/api/mcp/auth_callback`),
   ChatGPT (`https://chatgpt.com/connector_platform_oauth_redirect`), and Cursor
   (`cursor://anysphere.cursor-mcp/oauth/callback`) redirect URIs via the **same
   DCR path** (no CIMD); **public-client DCR (HIGH-8):** `/register` returns
   **HTTP 201** with a body containing `client_id`, `client_id_issued_at`,
   `redirect_uris`, `grant_types`, `response_types`, and
   `token_endpoint_auth_method: "none"`, and asserts **no `client_secret`** field
   is present; a missing/empty/invalid `redirect_uris` → HTTP 400
   `invalid_redirect_uri`; `/authorize` and `/token` handle an optional
   `resource=<url>` parameter (ChatGPT) per FR-F11 — a canonical value is accepted,
   a missing one defaults to canonical, a non-canonical one → `invalid_target`
   (HIGH-7); the issued token's `aud` is always the canonical `<base>/mcp`;
   `client_credentials` is neither advertised nor accepted; Cursor static-header
   path still works (no OAuth); **redirect-URI rules (HIGH-9):** a non-loopback
   `http://` redirect is rejected at registration (400 `invalid_redirect_uri`), a
   loopback `http://` is accepted, a `cursor://` custom scheme is accepted
   (AC-F9.2/F9.3). (The `/authorize` unregistered/mismatched `redirect_uri` → HTTP
   400 case is AC-F9.1, owned/tested by Task F3; F4 may re-exercise it but does not
   own it.)
2. Implement redirect-URI acceptance (scheme rules a/b/c, exact-match at
   `/authorize`) + the optional `resource` parameter handling in
   `registration.py` / `authorize.py` / `token.py`; the `cursor://` custom scheme
   is allowed (not HTTPS-gated like a web origin).
3. Run `make test`; green.

**Acceptance:** AC-F4.1–F4.5, AC-F9.2–F9.3.

### Task F5: README + Docker/Lambda OAuth Wiring

**Requirements:** FR-F1 (AC-F1.7), FR-E3 (compose env), FR-E4 (**AC-E4.6**, optional stack params)

**Steps:**
1. README: add the three-mode env table (no-auth / static Bearer / OAuth / both),
   per-client connector instructions (Claude.ai, ChatGPT, Cursor OAuth; Cursor and
   Claude Desktop static), and the HTTPS/localhost rule.
2. Finalize `docker-compose.yml` OAuth env (`LIBBY_MCP_OAUTH_ENABLED`,
   `LIBBY_MCP_OAUTH_SIGNING_KEY` (required when OAuth on, CRITICAL-2),
   `LIBBY_MCP_OAUTH_OWNER_SECRET` (CRITICAL-3), `LIBBY_MCP_PUBLIC_BASE_URL`
   (required non-loopback, HIGH-10), optional `LIBBY_MCP_OAUTH_PROVIDER=auth0`)
   and the optional CDK stack params (default disabled). Document in the README
   that non-loopback deployments must set `LIBBY_MCP_PUBLIC_BASE_URL` to the
   public HTTPS URL and run uvicorn with `--proxy-headers
   --forwarded-allow-ips='*'` behind a TLS proxy (AC-F10.3).
3. **Operator-pending live-deploy check (AC-E4.6, HIGH-8).** After an operator
   deploys the OAuth-enabled stack, confirm `GET /.well-known/oauth-protected-resource`
   on the live Lambda endpoint returns **HTTP 200** (the metadata route reaches the
   Lambda through API Gateway, not a 404/403). This verifies the E4 `cdk synth`
   routes end-to-end against a real deploy and is the **sole owning row for
   AC-E4.6** — it is marked operator-pending (not gated by Group-D/E merge CI,
   which does not stand up a live OAuth deploy). Document the exact `curl` and the
   expected 200 in the README OAuth section.
4. Run `make preflight`; green.

**Acceptance:** AC-F1.7, AC-F10.3, **AC-E4.6 (operator-pending, HIGH-8)**; OAuth works in both Docker and Lambda (AC-F1.5).

**PR-F2 exit:** `make preflight` green; checkboxes ticked; squash-merge.

---

## Cross-Cutting Verification

**Requirements:** NFR-1, NFR-4, NFR-6

Across all PRs:
- All existing v1/v2 tests continue to pass (NFR-1.1, NFR-4.1).
- New tests cover every v3 behavior (NFR-4.2) and the three auth modes, Docker
  auto-detect, and each OAuth endpoint.
- A compliance test / `scripts/check_compliance.py` assertion enforces the no-
  hardcoded-slug rule (NFR-6.1) and the single-handler rule (AC-E1.1).
- The live Lambda endpoint keeps working; a no-OAuth deploy is identical to v2
  (NFR-6.3, AC-E4.1/E4.2).
- No new mandatory Lambda runtime dependency (AC-E4.3 / NFR-6.4); uvicorn is
  Docker-only, and OAuth JWT is implemented with the Python standard library (no
  new package at all, HIGH-4 / AC-F6.4).
- `make preflight` (validate + compliance + ci) is green, including the 80%
  coverage threshold (NFR-4.4).
- Update `README.md` (three tools, Docker section, OAuth modes) and the ROADMAP
  with the new items as they merge.

## Requirements Traceability

Each FR/AC is listed against the **single task that owns it** (so a PR's exit
criteria never reference an AC built in a later task — the HIGH-12 fix). A few ACs
describe two separable concerns (a stateless signed-token *definition* vs. its
`/token` runtime *exchange*); for those, each **half** has exactly one owning row
and the row names which half it owns (HIGH-8), so no AC's work is claimed by two
PRs. Specifically (HIGH-8):

- **AC-F2.9 / AC-F2.10:** the signed code/refresh JWT *structure* + the three TTL
  **defaults** are owned by **F2 (PR-F1)**; the `/token` code-exchange and
  refresh-**rotation** at runtime are owned by **F3 (PR-F2)**.
- **AC-F6.5:** the `jwt.py` `verify(..., expected_typ, expected_iss)` /
  `alg=HS256` pinning is owned by **F2 (PR-F1)**; the `/token`-grant token-swap
  rejection (access token replayed as a refresh/code grant; code/refresh as a
  `/mcp` Bearer) is owned by **F3 (PR-F2)**.
- **AC-E4.6:** the deployed-Lambda `GET /.well-known/oauth-protected-resource` →
  200 check is owned **only** by **F5 (PR-F2)** as an operator-pending live-deploy
  step. The E4 row explicitly **disclaims** it (E4 owns only the `cdk synth` route
  assertions), so a Group-E PR's CI never needs a live OAuth deploy.
- **AC-F9.1** is owned by **F3** (the `/authorize` exact-match rule); **AC-F9.2 /
  F9.3** (registration-time scheme rules) are owned by **F4**.

| Task (PR) | Requirements / ACs implemented here |
|-----------|-------------------------------------|
| D1 (PR-D1) | FR-D1 (AC-D1.1–D1.9); async `handle()` + `get_availability_async`/`get_library_async` land here (HIGH-3), `status`-in-error with 400/404-only slug probe + multi-ID per-ID retry (HIGH-5), `title_id` `str().strip()` normalization + non-empty rule (HIGH-4) |
| D2 (PR-D1) | FR-D2 (AC-D2.1–D2.5), NFR-6.1 |
| D3 (PR-D1) | FR-D3 (AC-D3.1–D3.4) |
| D4 (PR-D2) | FR-D4 (AC-D4.1–D4.4) |
| D6 (PR-D2) | FR-D6 (AC-D6.1–D6.7) |
| D5 (PR-D3) | FR-D5 (AC-D5.1–D5.10), NFR-2.2, NFR-3.3, NFR-5.1 |
| D7 (PR-D4) | FR-D7 (AC-D7.1–D7.7), NFR-2.1, NFR-3.1, NFR-7.1; shared `classify_and_fetch` single+multi (HIGH-4) |
| E1 (PR-E) | FR-E1 (AC-E1.1–E1.5), AC-E4.3 |
| E2 (PR-E) | FR-E2 (AC-E2.1–E2.5) — curl uses valid `initialize` params + `Accept` header, GET→405 (HIGH-1/HIGH-2) |
| E3 (PR-E) | FR-E3 (AC-E3.1–E3.7); root `Dockerfile` on `python:3.12-slim` (HIGH-11) |
| E4 (PR-E) | FR-E4 (AC-E4.1–E4.5, **AC-E4.7**), NFR-6.2, NFR-6.3, NFR-6.4; OAuth API GW routes (CRITICAL-1), `LIBBY_MCP_PUBLIC_BASE_URL` env + signing-key secret (HIGH-9/HIGH-10), Lambda stays `PYTHON_3_12` (HIGH-11). **AC-E4.6 is NOT owned here** — it needs a live deploy and is owned by F5/PR-F2 as operator-pending (HIGH-8). |
| F1 (PR-F1) | FR-F1 (AC-F1.1–F1.5, AC-F1.1-lambda), FR-F3 (AC-F3.1) |
| F2 (PR-F1) | FR-F2 (AC-F2.1–F2.3, **AC-F2.7**, **AC-F2.8**, and the **signed-token / TTL-default halves** of AC-F2.9 + AC-F2.10 — the code/refresh JWT *structure*, `typ`, bound `resource`, and the three TTL defaults), FR-F3 (AC-F3.2–F3.3), FR-F5 (AC-F5.1), FR-F6 (AC-F6.1–F6.4, **AC-F6.6**, and the **jwt.py verify half** of AC-F6.5 — `verify(token, expected_typ, expected_iss)` rejecting wrong `typ`/`iss` and pinning `alg=HS256`, AC-CRITICAL-2), FR-F10 (AC-F10.1–F10.2), FR-F11 (AC-F11.1–F11.2 + canonical `aud` definition, HIGH-7), **AC-F1.8** (`_build_app` owner-secret + signing-key startup refusal); public-client 201 DCR (HIGH-8), typed/`HS256`-pinned artifacts (HIGH-5), SHA-256 signing key (HIGH-9), env TTLs (HIGH-6) |
| F3 (PR-F2) | FR-F1 (**AC-F1.6**, **AC-F1.9**, **AC-F1.10**, **AC-F1.11**), FR-F2 (AC-F2.4–F2.6, and the **`/token` code-exchange / refresh-rotation halves** of AC-F2.9 + AC-F2.10 — consuming a code at `/token` within its TTL and issuing/rotating refresh+access tokens at runtime), FR-F5 (AC-F5.2–F5.3), FR-F6 (the **`/token`-grant token-swap half** of AC-F6.5 — rejecting an access token replayed as a refresh/code grant and a code/refresh presented as a `/mcp` Bearer); FR-F9 (AC-F9.1), FR-F11 (AC-F11.3–F11.6 — `invalid_target` enforcement + canonical `aud`, HIGH-7); owner-consent gate + no-silent-auto-approve + loopback-base-URL-required-for-auto-approve / never-on-published-port (CRITICAL-1 / HIGH-6), token-swap rejection (HIGH-5) |
| F4 (PR-F2) | FR-F4 (AC-F4.1–F4.5), FR-F9 (AC-F9.2–F9.3); 400 `invalid_redirect_uri` (HIGH-8) |
| F5 (PR-F2) | FR-F1 (**AC-F1.7**, README mode table), FR-F10 (AC-F10.3), FR-E3 (compose OAuth env), FR-E4 (optional stack params); **AC-E4.6 (operator-pending, HIGH-8)** — the deployed-Lambda `GET /.well-known/oauth-protected-resource` → HTTP 200 check runs against a live OAuth-enabled deploy, so it is ticked after an operator deploys, not by the merge CI of any Group-D/E PR. |
| Cross-cutting | NFR-1, NFR-4 (incl. NFR-4.3 405 rule, HIGH-1), NFR-5, NFR-6, NFR-7, NFR-8 (HIGH-2 env semantics) |

**PR-exit-criteria sanity (HIGH-12):** PR-F1 (Tasks F1+F2) implements and may
tick AC-F1.1–F1.5, AC-F1.1-lambda, **AC-F1.8**, and the F2 metadata/registration/
jwt ACs — it does **not** tick AC-F1.6 (owner gate, in F3), AC-F1.7 (README, in
F5), or AC-F1.9/F1.10/F1.11 (dev auto-approve conditions + loopback-base-URL
requirement + never-on-published-port, in F3). PR-F2 (Tasks F3+F4+F5) ticks
AC-F1.6, AC-F1.7, AC-F1.9, AC-F1.10, AC-F1.11 and the remaining
FR-F2/F4/F5/F9/F10 ACs.

## ROADMAP Items (to append after this spec is approved)

These map 1:1 to the PR plan above (one ROADMAP item per PR, in order). The
ROADMAP currently ends at item 9 (local integration harness), so v3 continues
from **item 10**. Append these to `ROADMAP.md` in the shown format.

### 10. libby-mcp v3 — Group D availability + slug fixes (PR-D1)

Read `.kiro/specs/libby-mcp-v3/requirements.md`, `design.md`, and `tasks.md` in
full, then implement tasks `D1`, `D2`, `D3` (`not_owned` per-title status, slug
normalization, `estimated_wait_days==0`-when-available).

- Spec: `.kiro/specs/libby-mcp-v3/` · tasks `D1`, `D2`, `D3`
- [x] Complete · PR: #21
- Depends-on: item 9 (local integration harness merged)

### 11. libby-mcp v3 — Group D search honesty + exact mode (PR-D2)

Read `.kiro/specs/libby-mcp-v3/requirements.md`, `design.md`, and `tasks.md` in
full, then implement tasks `D4`, `D6` (honest `is_abridged`, exact-title search).

- Spec: `.kiro/specs/libby-mcp-v3/` · tasks `D4`, `D6`
- [ ] Complete
- Depends-on: item 10 (PR-D1 merged)

### 12. libby-mcp v3 — Group D `lookup_library_slug` + directory cache (PR-D3)

Read `.kiro/specs/libby-mcp-v3/requirements.md`, `design.md`, and `tasks.md` in
full, then implement task `D5` (`lookup_library_slug` tool + slug directory
cache with concurrent paging and shared in-flight lock).

- Spec: `.kiro/specs/libby-mcp-v3/` · tasks `D5`
- [ ] Complete
- Depends-on: item 11 (PR-D2 merged)

### 13. libby-mcp v3 — Group D multi-library `get_availability` (PR-D4)

Read `.kiro/specs/libby-mcp-v3/requirements.md`, `design.md`, and `tasks.md` in
full, then implement task `D7` (multi-library `get_availability` fan-out).

- Spec: `.kiro/specs/libby-mcp-v3/` · tasks `D7`
- [ ] Complete
- Depends-on: item 12 (PR-D3 merged)

### 14. libby-mcp v3 — Group E Docker standalone (PR-E)

Read `.kiro/specs/libby-mcp-v3/requirements.md`, `design.md`, and `tasks.md` in
full, then implement tasks `E1`, `E2`, `E3`, `E4` (single-codebase auto-detect
entry point, Docker auth, Docker deliverables, Lambda deploy + OAuth routes).

- Spec: `.kiro/specs/libby-mcp-v3/` · tasks `E1`, `E2`, `E3`, `E4`
- [ ] Complete
- Depends-on: item 13 (PR-D4 merged)

### 15. libby-mcp v3 — Group F auth modes + OAuth metadata + jwt (PR-F1)

Read `.kiro/specs/libby-mcp-v3/requirements.md`, `design.md`, and `tasks.md` in
full, then implement tasks `F1`, `F2` (three auth modes + middleware order, OAuth
metadata/`/register`, and `jwt.py` sign/verify — the stateless signed-token core).

- Spec: `.kiro/specs/libby-mcp-v3/` · tasks `F1`, `F2`
- [ ] Complete
- Depends-on: item 14 (PR-E merged — single-codebase ASGI app)

### 16. libby-mcp v3 — Group F OAuth flow + per-client + wiring (PR-F2)

Read `.kiro/specs/libby-mcp-v3/requirements.md`, `design.md`, and `tasks.md` in
full, then implement tasks `F3`, `F4`, `F5` (authorize + token with owner gate,
PKCE, refresh rotation; per-client redirect URIs; README + Docker/Lambda OAuth
wiring).

- Spec: `.kiro/specs/libby-mcp-v3/` · tasks `F3`, `F4`, `F5`
- [ ] Complete
- Depends-on: item 15 (PR-F1 merged — `jwt.py` + metadata + auth modes)
