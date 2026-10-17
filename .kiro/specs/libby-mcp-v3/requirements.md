# Requirements: Libby MCP Server v3

## Overview

libby-mcp v2 is deployed and working — an MCP server exposing the OverDrive
Thunder API as two fact-finding tools (`search_titles`, `get_availability`). The
tools return raw catalog data; the LLM caller does all reasoning (no
recommendations or advisory logic in the server). It runs as an AWS Lambda behind
an API Gateway HTTP API, using FastMCP Streamable HTTP over Mangum, authenticated
by a single static Bearer token.

v3 ships three concerns together as one coherent release:

- **Group D — Bug fixes** (7 issues found in live usage)
- **Group E — Docker standalone** (`git clone && docker compose up`, no AWS)
- **Group F — OAuth 2.1** (connect from Claude.ai, ChatGPT, and Cursor)

The three groups are largely independent but share one seam: the `handler.py`
entry point and the `AuthMiddleware`. Group E makes `handler.py` serve the same
ASGI app under either Lambda (Mangum) or a local server (uvicorn) **without
forking the codebase**, and Group F adds a third authentication mode alongside
the existing "no auth" and "static Bearer" modes. Suggested order: Group D, then
Group E, then Group F (F's OAuth endpoints are mounted on the same ASGI app that
E generalizes).

## Reference

- `#[[file:.kiro/specs/libby-mcp-v3/context/handler.py]]` — current Lambda handler (FastMCP + Mangum + `AuthMiddleware`, tool registrations)
- `#[[file:.kiro/specs/libby-mcp-v3/context/auth.py]]` — current `AuthMiddleware` and `validate_api_key`
- `#[[file:.kiro/specs/libby-mcp-v3/context/thunder_client.py]]` — current sync/async Thunder HTTP client
- `#[[file:.kiro/specs/libby-mcp-v3/context/availability.py]]` — current `get_availability` tool implementation
- `#[[file:.kiro/specs/libby-mcp-v3/context/search_titles.py]]` — current `search_titles` tool (`_normalize_item`, fan-out helpers)
- `#[[file:.kiro/specs/libby-mcp-v3/context/ROADMAP.md]]` — ROADMAP with the 9 completed items
- `#[[file:.kiro/specs/libby-mcp-v3/context/libby-mcp-v2-spec/]]` — prior v2 spec (requirements/design/tasks) for reference
- `#[[file:docs/references/thunder_api_reference.md]]` — endpoints, params, response shapes, known quirks
- `#[[file:docs/references/thunder_openapi_v1.json]]` — partial Thunder OpenAPI spec (121 paths)
- `#[[file:docs/references/mcp_spec_excerpt.md]]` — MCP 2025-06-18 protocol details

## Example Library Slugs

| Slug | Library |
|------|---------|
| `lcpl` | Loudoun County Public Library |
| `fairfax` | Fairfax County Public Library |
| `nypl` | New York Public Library |

These are used only as **examples in tool/parameter descriptions and error
messages**. They MUST NOT appear as hardcoded behavioral branches anywhere in
server code (see NFR-6.1).

## Scope Note

v3 preserves the v1/v2 fact-finding contract: tools return raw data, no
recommendations, rankings, or strategic advice. All existing v2 acceptance
criteria that are not explicitly changed below remain in force. All v3 title- and
response-object additions are additive (NFR-5).

---

## Group D — Bug Fixes

Priority order follows the brief.

### FR-D1: `get_availability` Must Not Crash on Not-Owned Titles

**Description:** The Thunder bulk-availability endpoint omits titles the library
does not own from its `items[]` array. The current tool maps a missing title ID
to `{"error": "not_found"}`; worse, a batch containing only not-owned IDs can
surface as a failing request. v3 returns a per-title `not_owned` status so a bad
or not-owned ID never fails the whole batch. The root cause (HIGH-3) is pinned by
distinguishing three upstream outcomes: a **non-numeric/malformed** `title_id`
(classified `not_owned` before any Thunder call), a **well-formed-but-unknown**
`title_id` (absent from an otherwise-`200` `items[]`), and a **genuine upstream
failure** (a `4xx` is disambiguated by probing `GET /v2/libraries/{slug}` to tell
a bad title from a bad slug; `5xx`/timeout is always a top-level error).

**Acceptance Criteria:**
- AC-D1.1: `get_availability` with a title ID the library does not own returns a per-title entry `{"title_id": "<id>", "status": "not_owned"}` for that ID instead of failing the request.
- AC-D1.2: `get_availability(['bad-title-id'], 'lcpl')` (a request whose only ID is not owned) returns the flat v2 single-library shape `{"library_slug": "lcpl", "titles": [{"title_id": "bad-title-id", "status": "not_owned"}]}` and a successful MCP result — **never an MCP error / HTTP 500**.
- AC-D1.3: Owned titles in the same batch still return full availability data (`copies_owned`, `copies_available`, `holds_count`, `estimated_wait_days`) alongside any `not_owned` entries.
- AC-D1.4: A genuine upstream failure (timeout, 5xx, 404 on the library slug itself) is still surfaced as an error — the `not_owned` status is used only for titles absent from an otherwise-successful availability response.
- AC-D1.5: The per-title key is `status` (value `"not_owned"`); the legacy `{"error": "not_found"}` per-title shape is removed.
- AC-D1.6: A non-numeric / malformed `title_id` (e.g. the brief's `bad-title-id`) is classified `{"title_id": tid, "status": "not_owned"}` **before** any Thunder call — the malformed ID is excluded from the `titleIds` query so it can never make the upstream request fail (this is the concrete fix for AC-D1.2's 500). The numeric check (`tid.strip().isdigit()`) is mechanical with no slug/title special-casing (NFR-6.1).
- AC-D1.7: When the bulk-availability endpoint returns a **`400` or `404`**, the server distinguishes a bad title from a bad library slug by probing `GET /v2/libraries/{slug}`: if the slug lookup also fails (4xx), the result is a top-level `error` (`unknown library slug: <slug>` / per-library `error` block); if the slug lookup succeeds (valid library) **and more than one title ID was requested, the server retries each ID individually** so an owned title is never masked by one bad ID poisoning the batch (HIGH-5) — a single requested ID is classified `not_owned` directly. A `429`, `401`, `403`, `408`, `5xx`, or timeout is **always a top-level `error`, never probed and never reclassified** (the slug probe runs only on `400`/`404`, HIGH-5).
- AC-D1.8: Every element of `title_ids` is normalized to a stripped string (`tid = str(tid).strip()`) **before** any `isinstance`/`isdigit`/numeric check, so an integer ID, a padded `" 123 "`, or a non-string value is handled without raising; blank-after-strip entries are dropped and never sent to Thunder (HIGH-4). This normalization is applied identically on the single-library and multi-library paths (the shared classifier).
- AC-D1.9: `title_ids` must be a **non-empty list** on both the single-library and multi-library paths; an empty list (or a list that is empty after dropping blank entries) is rejected with a validation error (`title_ids is required (non-empty)`) rather than returning a silent empty success (HIGH-4).

> Note: `get_availability(['bad-title-id'], 'lcpl')` in the brief is positional
> shorthand for "a request for title ID `bad-title-id` against library `lcpl`";
> the actual tool arguments are `library_slug`/`library_slugs` and `title_ids`.
> The single-library response uses the **flat v2 shape**
> `{"library_slug": "<slug>", "titles": [...]}`, with each not-owned title as a
> per-entry `{"title_id": ..., "status": "not_owned"}` inside `titles` (see the
> response shapes in design.md). The multi-library form groups these flat blocks
> under `{"libraries": [...]}`.

### FR-D2: Normalize Library Slugs on Input

**Description:** Library slugs are normalized (lowercased and trimmed) at the
input boundary so a caller passing `LCPL` or ` lcpl ` does not echo mixed-case or
padded values into responses, deep links, or error messages.

**Acceptance Criteria:**
- AC-D2.1: Every library slug accepted by `search_titles` (`library_slug`, each entry of `library_slugs`), `get_availability` (`library_slugs`), and `lookup_library_slug` is normalized by `slug.strip().lower()` before it is used in a Thunder request, a response field, a deep link, or an error message.
- AC-D2.2: `search_titles('LCPL', ...)` yields `library_slug == "lcpl"` in all response fields, and every title's `link` is `https://lcpl.overdrive.com/media/<id>` (lowercase host).
- AC-D2.3: Leading/trailing whitespace is stripped: `' lcpl '` normalizes to `'lcpl'`.
- AC-D2.4: Normalization occurs before the multi-library dedupe step, so `['LCPL', 'lcpl']` deduplicates to a single `lcpl` fan-out.
- AC-D2.5: A slug that is empty or whitespace-only after normalization is rejected with the existing validation message (`library_slugs contains invalid entry: empty slug` for the list form; `library_slug or library_slugs is required` for an empty single form).

### FR-D3: `estimated_wait_days` Is 0 When Copies Are Available

**Description:** OverDrive's `estimatedWaitDays` is noisy and can report a nonzero
wait for a title sitting on the shelf (Thunder quirk #3). When a copy is
available, the estimated wait MUST be 0.

**Acceptance Criteria:**
- AC-D3.1: In `get_availability`, when a title's `copies_available > 0`, `estimated_wait_days == 0` regardless of the raw `estimatedWaitDays` value from Thunder.
- AC-D3.2: When `copies_available == 0`, `estimated_wait_days` is the raw `estimatedWaitDays` value (passed through unchanged).
- AC-D3.3: In `search_titles` folded availability, the same rule applies: a title with `available_copies > 0` reports `estimated_wait_days == 0`; otherwise it reports the raw value or `None` when the field is absent (the v2 "unknown is `None`, not `0`" rule is preserved for the not-available case).
- AC-D3.4: The rule keys on availability (`copies_available` / `available_copies`), not on `isAvailable` alone.

### FR-D4: Honest `is_abridged`

**Description:** `is_abridged` must reflect real edition metadata or be absent.
The server must never fabricate a value (e.g. defaulting to `false`).

**Acceptance Criteria:**
- AC-D4.1: `is_abridged` is derived from an actual Thunder field (the format entry's `isAbridged`, or an abridged signal present in `edition`), and is `true`/`false` only when that source is present.
- AC-D4.2: When no abridged signal is present in the Thunder response, `is_abridged` is `null` (unknown) — never coerced to `false`.
- AC-D4.3: If, after verification against live/reference data, no reliable abridged source exists, the field is dropped from the title schema entirely (documented in the design) rather than returned with a fabricated value.
- AC-D4.4: Whichever decision is taken (derive or drop), it is applied consistently across single-library and multi-library `search_titles` responses, and a test asserts the field is never a fabricated constant.

### FR-D5: Add `lookup_library_slug` Tool

**Description:** New tool: given a library name or partial name, return matching
OverDrive slugs. This is the #1 friction point for new users. The slug directory
is cached with a 7-day TTL (it changes slowly); the load pages `/v2/libraries`
concurrently and shares one in-flight fetch across concurrent callers.

**Acceptance Criteria:**
- AC-D5.1: A new MCP tool `lookup_library_slug` is registered and appears in `tools/list`.
- AC-D5.2: `lookup_library_slug` accepts a `name` parameter (a library name or partial name, case-insensitive).
- AC-D5.3: The tool returns a list of matches, each carrying at least the `slug` and the library's display `name`; matching is case-insensitive substring matching against the library name and the slug/key.
- AC-D5.4: `lookup_library_slug('Fairfax')` returns a result set whose slugs include `fairfax` (the primary acceptance case from the brief).
- AC-D5.5: The slug directory sourced from Thunder is cached in-process with a 7-day TTL (configurable via `LIBBY_MCP_SLUG_TTL_SECONDS`); repeated lookups within the TTL do not re-fetch the directory. The directory load fetches page 1 first (to read `totalItems`), then fetches the remaining pages concurrently with `asyncio.gather`, and completes in **under 10 seconds** against the Thunder API (so it stays well inside the 30s Lambda timeout).
- AC-D5.6: A lookup with no matches returns an empty result list (not an error).
- AC-D5.7: The tool returns raw directory data only (slug, name, and any directly available directory fields) — no ranking or recommendation, consistent with the fact-finding contract.
- AC-D5.8: A failure fetching the directory from Thunder returns a human-readable error without crashing; a stale-but-present cached directory is served if available rather than failing.
- AC-D5.9: Concurrent callers during a cold (unpopulated) window share a single in-flight directory fetch via a module-level `asyncio.Lock` — they do not each trigger a full `/v2/libraries` walk.
- AC-D5.10: A bundled `src/fallback_slugs.json` (top-100 libraries) ships with the server; if the live directory fetch fails and no cache is present, `lookup_library_slug` matches against the bundled fallback and the result carries a `warning` noting the data is a bundled snapshot, not live.

### FR-D6: Exact-Title Search Mode

**Description:** Add an `exact` boolean parameter to `search_titles`. When
`exact=true`, only titles whose title matches the query exactly are returned —
no fuzzy fallback.

**Acceptance Criteria:**
- AC-D6.1: `search_titles` accepts an optional `exact: bool = False` parameter.
- AC-D6.2: When `exact=true` and `query` is provided, the result set contains only titles whose normalized title equals the normalized query (comparison is `title.strip().lower() == query.strip().lower()`).
- AC-D6.3: `search_titles('The Mom Test', exact=true)` returns only titles whose title is exactly "The Mom Test" (case-insensitive); fuzzy/partial matches are filtered out.
- AC-D6.4: When `exact=true` but no `query` is provided, the server returns a validation error (`exact requires query`), because there is no title to match against.
- AC-D6.5: When `exact=false` or omitted, search behavior is unchanged from v2 (fuzzy full-text behavior).
- AC-D6.6: Exact filtering applies consistently in both single-library and multi-library (`library_slugs`) responses.
- AC-D6.7: `search_titles` with `exact=true` for a title that only appears on page 2 of Thunder results still returns the match. When `exact=true`, the search uses `perPage=100` and `sort=relevance` (not `availableFirst`), and if the first page yields no exact match while `total_items > 0`, it fetches further pages (up to 3 pages / 300 items) and re-filters until a match is found or the cap is reached.

### FR-D7: Multi-Library `get_availability`

**Description:** `get_availability` fans out across multiple library slugs, the
same way `search_titles` already supports `library_slugs`.

**Acceptance Criteria:**
- AC-D7.1: `get_availability` accepts a `library_slugs` array parameter in addition to the existing single `library_slug`.
- AC-D7.2: Exactly one of `library_slug` / `library_slugs` must be provided, with the same one-of validation messages used by `search_titles` (`provide either library_slug or library_slugs, not both`; `library_slug or library_slugs is required`).
- AC-D7.3: `get_availability([ids], ['lcpl', 'fcpl'])` returns availability results for both libraries, grouped per library (each block tagged with its own `library_slug`).
- AC-D7.4: The `library_slugs` validation rules match `search_titles`: empty list rejected, blank/whitespace entries rejected, duplicates deduplicated (first-seen order preserved, after normalization per FR-D2), max 10 libraries. The `title_ids` normalization and non-empty rule (AC-D1.8/AC-D1.9) apply on the multi-library path too — a non-numeric or padded ID is classified `not_owned` in each library's block, and an empty `title_ids` is rejected before fan-out.
- AC-D7.5: A per-library failure (bad slug 404, timeout) does not fail the entire request; the failing library's block carries an `error` field while other libraries still return results (parallel to `search_titles` AC-B1.5).
- AC-D7.6: The single-`library_slug` response shape is unchanged from v2 (flat `{library_slug, titles}`), except for the FR-D1 (`not_owned`) and FR-D3 (`estimated_wait_days`) changes.
- AC-D7.7: Fan-out runs the per-library availability calls concurrently (`asyncio.gather`), so N libraries complete in roughly the time of the slowest one.

---

## Group E — Docker Standalone

### FR-E1: Single-Codebase Entry Point (No Fork)

**Description:** `handler.py` must serve the same ASGI app in both Lambda and
Docker without forking into two entry points. It auto-detects the runtime via the
`AWS_LAMBDA_RUNTIME_API` environment variable.

**Acceptance Criteria:**
- AC-E1.1: There is exactly one handler module (`src/handler.py`); no separate `docker_handler.py` / `lambda_handler.py` fork exists.
- AC-E1.2: When `AWS_LAMBDA_RUNTIME_API` is set (present), the module wraps the ASGI app with Mangum (Lambda mode) — unchanged from v2.
- AC-E1.3: When `AWS_LAMBDA_RUNTIME_API` is absent, the module exposes the raw ASGI app (built by the existing `_build_app` factory) to be served by uvicorn (Docker / local mode).
- AC-E1.4: Both modes serve the MCP endpoint at path `/mcp` so a connector config is portable between Lambda and Docker.
- AC-E1.5: The existing `lambda_handler(event, context)` entry point and the `_build_app()` factory both remain importable and behave as in v2 (the local-integration harness already depends on `src.handler:_build_app`).

### FR-E2: Docker Auth Behavior

**Description:** In Docker, authentication is governed by `LIBBY_MCP_API_KEY`,
with the same semantics the brief specifies.

**Acceptance Criteria:**
- AC-E2.1: When `LIBBY_MCP_API_KEY` is set, Docker mode requires `Authorization: Bearer <key>` on `/mcp` requests (static Bearer mode).
- AC-E2.2: When `LIBBY_MCP_API_KEY` is unset, **Docker** mode serves `/mcp` unauthenticated (local-dev / firewalled scenario). This no-auth fallback is Docker-only; on Lambda, no-auth fails closed (AC-F1.1-lambda).
- AC-E2.3: `curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"curl\",\"version\":\"0\"}}}" http://localhost:8000/mcp` (with a valid key when the key is set) returns a valid MCP initialize response (HTTP 200). The `params` MUST carry `protocolVersion`, `capabilities`, and `clientInfo` (an empty `params:{}` fails `InitializeRequestParams` validation and returns JSON-RPC -32602), and the `Accept` header MUST advertise both `application/json` and `text/event-stream` (Streamable HTTP requirement). The request MUST be `POST` with a JSON-RPC body — a bare `GET /mcp` is answered with HTTP 405 by the middleware, not an initialize response.
- AC-E2.4: `curl -X POST http://localhost:8000/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"curl\",\"version\":\"0\"}}}"` with no `Authorization` header, when a key is set, returns HTTP 401.
- AC-E2.5: The auth behavior is identical to the Lambda path's static-Bearer mode (same `AuthMiddleware`, no Docker-specific auth fork).

### FR-E3: Docker Deliverables

**Description:** Ship a runnable Docker setup.

**Acceptance Criteria:**
- AC-E3.1: A `Dockerfile` exists at the repo root that is multi-stage and runs the server as a non-root user.
- AC-E3.2: The container serves the MCP endpoint on port 8000 at `/mcp` via uvicorn against the `src.handler:_build_app` factory (the same factory the integration image uses).
- AC-E3.3: A `docker-compose.yml` exists that builds the `Dockerfile` and starts the service with `docker compose up`, publishing port 8000 **bound to loopback by default (`127.0.0.1:8000:8000`, HIGH-6)** and reading `LIBBY_MCP_API_KEY` (and OAuth env vars from Group F) from the environment. Optional env vars are passed **conditionally** (compose list form) so that unset host vars are not injected into the container as empty strings (HIGH-2 / NFR-8.2); any empty string that does reach the container is normalized to unset (NFR-8.1).
- AC-E3.4: `docker compose up` brings the server to a ready/healthy state in under 30 seconds.
- AC-E3.5: `.dockerignore` is updated so the build context excludes `.git`, `infra`, `.venv`, caches, `cdk.out`, `node_modules`, and tests (image contains only runtime source + deps).
- AC-E3.6: The `README` gains a Docker section with a quickstart and connector-config examples for Claude Desktop and Cursor (static-header form).
- AC-E3.7: `docker compose up` requires no AWS account, AWS credentials, or Secrets Manager access.

### FR-E4: Lambda Deploy Unchanged (+ OAuth Routes When Enabled)

**Description:** The Docker work must not disturb the existing cloud deployment.
When OAuth is enabled, the CDK stack additionally routes the OAuth endpoints to
the same Lambda (otherwise those paths never reach the ASGI app).

**Acceptance Criteria:**
- AC-E4.1: The existing CDK Lambda deploy still succeeds, and Mangum still wraps the app when `AWS_LAMBDA_RUNTIME_API` is set.
- AC-E4.2: The live Lambda endpoint (`https://jmz949xvg6.execute-api.us-east-2.amazonaws.com/mcp`) continues to work unchanged (static Bearer, `POST /mcp`).
- AC-E4.3: No new packages are added to `requirements.txt` for OAuth; JWT is implemented with the `hmac`, `hashlib`, `base64`, and `json` standard-library modules. uvicorn is confined to the Docker path (not required for Lambda bundling). The Lambda path adds zero new runtime dependencies.
- AC-E4.4: The CDK stack remains cloud-agnostic — someone deploying to their own AWS account needs zero changes to `infra/` (no account IDs, ARNs, or region constants tied to the current owner; see NFR-6.2).
- AC-E4.5: When OAuth is enabled (`LIBBY_MCP_OAUTH_ENABLED=true`), the CDK stack adds API Gateway HTTP API routes — all pointing at the same Lambda integration as `/mcp` — for `GET /.well-known/oauth-protected-resource`, `GET /.well-known/oauth-authorization-server`, `POST /register`, `GET /authorize`, `POST /authorize`, and `POST /token`. When OAuth is disabled, none of these routes are added and the stack is identical to v2.
- AC-E4.6: When OAuth is enabled, `GET /.well-known/oauth-protected-resource` returns HTTP 200 against the deployed Lambda endpoint (the OAuth metadata route reaches the Lambda through API Gateway, not a 404/403 from an unrouted path).
- AC-E4.7: When OAuth is enabled, the CDK stack sets the Lambda environment variable `LIBBY_MCP_PUBLIC_BASE_URL` to the deployed base URL — defaulting to `httpApi.apiEndpoint` (origin only, no `/mcp` path) with an optional custom-domain override from a CDK **context** variable (e.g. `-c publicBaseUrl=https://mcp.example.com`) — so the OAuth routes do not fall through to the non-loopback `oauth_not_configured` guard (HIGH-10). A `cdk synth` assertion confirms the variable is set when OAuth is enabled and absent when disabled, and that the context override is honored. When OAuth is disabled, the stack is identical to v2 and the variable is not set.

---

## Group F — OAuth 2.1

> **Ground truth from the brief — implement exactly these shapes.** The endpoint
> set, redirect URIs, PKCE method, and discovery behavior below are prescribed by
> the brief and confirmed against the MCP authorization spec (2025-06-18, which
> mandates RFC 9728 Protected Resource Metadata) and RFC 9728 / RFC 8414 / RFC
> 7591. The MCP server acts as the **OAuth 2.0 resource server** (validates
> tokens); an **embedded, self-contained authorization server** is the default so
> self-hosters need no third-party account. Auth0 MAY be wired as an optional
> backend provider but MUST NOT be a hard dependency.

### FR-F1: Three Simultaneous Auth Modes

**Description:** The server supports three authentication modes that coexist,
selected by environment variables, in both Docker and Lambda.

**Acceptance Criteria:**
- AC-F1.1: **No-auth mode** — when `LIBBY_MCP_API_KEY` is unset and `LIBBY_MCP_OAUTH_ENABLED` is not `true`, `/mcp` is served unauthenticated. This mode is permitted in **Docker only**; on Lambda it fails closed (AC-F1.1-lambda).
- AC-F1.2: **Static-Bearer mode** — when `LIBBY_MCP_API_KEY` is set, `Authorization: Bearer <key>` is required and validated by constant-time comparison (current prod behavior, must keep working).
- AC-F1.3: **OAuth mode** — when `LIBBY_MCP_OAUTH_ENABLED=true`, the OAuth endpoints (FR-F2) are activated and `/mcp` requests are authenticated by validating a JWT Bearer access token.
- AC-F1.4: Static Bearer and OAuth coexist: when both `LIBBY_MCP_API_KEY` is set and OAuth is enabled, an incoming Bearer token is accepted if it matches the static key **or** validates as a JWT access token.
- AC-F1.5: All three modes work in both Docker and Lambda deployments (same `AuthMiddleware`, no deployment-specific auth fork).
- AC-F1.6: `/authorize` with no valid owner credential in the (POST) consent form returns HTTP 401 before issuing a code — anyone reaching the URL cannot obtain a token without the owner secret (CRITICAL-3). The owner secret is compared with `hmac.compare_digest`. The effective owner secret is `LIBBY_MCP_OAUTH_OWNER_SECRET` if set-and-non-empty, else `LIBBY_MCP_API_KEY` if set-and-non-empty. There is **no silent auto-approve fallback** (CRITICAL-1): see AC-F1.8 (refuse to start) and AC-F1.9 (opt-in dev auto-approve conditions).
- AC-F1.7: The mode-selection logic is documented in the README with a table mapping each scenario to the env vars to set.
- AC-F1.8: When `LIBBY_MCP_OAUTH_ENABLED=true` and the effective owner secret is unset **or an empty string** (both `LIBBY_MCP_OAUTH_OWNER_SECRET` and `LIBBY_MCP_API_KEY` unset/empty), the server **refuses to start** with a clear configuration error (same fail-fast as AC-F6.2) — it MUST NOT auto-approve `/authorize` consent as a fallback (CRITICAL-1). **AC-F1.8 applies unconditionally, including in dev auto-approve mode:** enabling `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` does **not** waive the configured-owner-secret startup requirement. Dev auto-approve only skips the interactive consent **form UI** (so a developer does not have to paste the owner secret into a browser on every local authorize); it never skips the startup check that an owner secret is configured. A server started in OAuth mode with no effective owner secret refuses to start whether or not auto-approve is requested.
- AC-F1.9: The dev auto-approve path for `/authorize` is reachable **only** when **all three** hold: (1) `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` is explicitly set, (2) the process is **not** on Lambda (`AWS_LAMBDA_RUNTIME_API` is absent), and (3) `LIBBY_MCP_PUBLIC_BASE_URL` is **explicitly set (set-and-non-empty)** to a **loopback origin** whose host is exactly `127.0.0.1`, `localhost`, or `::1` (HIGH-6). An **unset or empty** `LIBBY_MCP_PUBLIC_BASE_URL` does **not** satisfy condition (3) — there is no implicit loopback default for the purpose of auto-approve. **A configured owner secret is a precondition in every case:** auto-approve runs *in addition to*, never *instead of*, the AC-F1.8 startup requirement that an effective owner secret is configured. The server never starts in OAuth mode without an owner secret, so there is no state in which auto-approve runs with no owner secret configured — auto-approve only suppresses the consent-form UI prompt at request time, it does not relax startup. If any of the three conditions fails, auto-approve is refused and the (already-required) owner secret must be entered on the consent form. When active, a startup warning is emitted. Auto-approve is never the state of a non-loopback or Lambda deployment (CRITICAL-1).
- AC-F1.10: When `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` is set but `LIBBY_MCP_PUBLIC_BASE_URL` is **unset or empty** (so the loopback origin cannot be verified), the server **refuses to start** with a clear configuration error (HIGH-6) rather than silently falling back to requiring the owner secret or silently defaulting the base URL to loopback. The error names `LIBBY_MCP_PUBLIC_BASE_URL` as required when auto-approve is requested. (A non-loopback, Lambda, or `https`/non-loopback base URL with the flag set is also refused, consistent with AC-F1.9; those cases fail at the flag check, this AC pins the specifically-unset case so an operator cannot enable auto-approve without declaring a loopback base URL.)
- AC-F1.11: Dev auto-approve MUST NOT be used with a **published** (non-loopback-bound) port. The documentation states this explicitly, and the shipped `docker-compose.yml` binds the port to loopback by default (`127.0.0.1:8000:8000`, AC-E3.3/HIGH-6) so a bare `docker compose up` with the auto-approve flag cannot expose an auto-approving `/authorize` beyond the host. Publishing `0.0.0.0:8000` (or any non-loopback bind) together with `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` is an unsupported misconfiguration documented as such.
- AC-F1.1-lambda: When `LIBBY_MCP_API_KEY` is unset **and** `AWS_LAMBDA_RUNTIME_API` is set (Lambda mode) **and** OAuth is not enabled, all `/mcp` requests return HTTP 500 with `error: authentication_not_configured`. No-auth mode is permitted in Docker only; on Lambda the server must have at least one auth mechanism configured (HIGH-13).

### FR-F2: Required OAuth Endpoints

**Description:** When OAuth is enabled, the server exposes the exact endpoint set
below on the same ASGI app as `/mcp`.

**Acceptance Criteria:**
- AC-F2.1: `GET /.well-known/oauth-protected-resource` returns an RFC 9728 Protected Resource Metadata document that points to the authorization server (its `authorization_servers` entry) and identifies the protected resource.
- AC-F2.2: `GET /.well-known/oauth-authorization-server` returns an RFC 8414 Authorization Server Metadata document whose `code_challenge_methods_supported` is `["S256"]` and whose advertised grant types include `authorization_code` and `refresh_token` (and NOT `client_credentials`).
- AC-F2.3: `POST /register` implements RFC 7591 Dynamic Client Registration, accepting a client's redirect URI(s) and registering **every client as a public client** (PKCE, no `client_secret` issued or stored — HIGH-8). On success it returns **HTTP 201** with a JSON body containing exactly: `client_id` (the signed stateless token), `client_id_issued_at` (epoch seconds), `redirect_uris` (the registered array, echoed back), `grant_types` (`["authorization_code", "refresh_token"]`), `response_types` (`["code"]`), and `token_endpoint_auth_method` set to `"none"`. No `client_secret` or `client_secret_expires_at` field is present. A registration request whose `redirect_uris` is missing, empty, or contains a URI that violates the scheme rules (FR-F9) is rejected with **HTTP 400 and `error: "invalid_redirect_uri"`** (RFC 7591 §3.2.2).
- AC-F2.4: `GET /authorize` is the authorization endpoint: it enforces PKCE (rejects requests without a `code_challenge` / `code_challenge_method=S256`) and drives the user consent/redirect, returning an authorization `code` to the registered redirect URI.
- AC-F2.5: `POST /token` is the token endpoint, consuming `application/x-www-form-urlencoded`, exchanging an authorization `code` + `code_verifier` for an access token (and a refresh token), and rejecting a mismatched PKCE verifier.
- AC-F2.6: `POST /token` with `grant_type=refresh_token` issues a **new** refresh token (fresh `jti`/`iat`) and a new access token for public clients. **Documented deviation from strict OAuth 2.1 (HIGH-6):** because the AS is stateless (no server-side token store), rotation is *issuance of a new refresh token without replay detection* — the previous refresh token stays valid until its own `exp`. There is no revocation list; the only way to invalidate outstanding tokens early is to rotate `LIBBY_MCP_OAUTH_SIGNING_KEY`. This trade-off is acceptable for the single-owner self-host model and is recorded in the design.
- AC-F2.7: All OAuth endpoints are served over HTTPS in non-localhost deployments; `localhost`/`127.0.0.1` redirect URIs and origins are permitted for development (AC-F5.1).
- AC-F2.8: The embedded AS is **stateless** — `client_id`s, authorization codes, access tokens, and refresh tokens are all self-contained signed tokens; the server keeps no in-memory or on-disk client/code/token table. A registered client, an issued code, and an issued token remain valid regardless of which Lambda instance (or cold start) handles the next request.
- AC-F2.9: Authorization codes are signed JWTs (`typ: "code"`) with a numeric, env-configurable TTL `LIBBY_MCP_OAUTH_CODE_TTL_SECONDS` (**default 60 seconds**), carrying the PKCE `code_challenge`, `redirect_uri`, `client_id`, and the bound **canonical** `resource` (`<base>/mcp`, per FR-F11); `/token` validates the code's signature, `typ`, and expiry rather than consulting a server-side store. **Documented deviation (HIGH-6):** because the AS keeps no server-side store, a code is replayable for the full duration of its TTL (there is no one-time-use consumption record). The replay window is bounded by the short TTL and mitigated by the mandatory PKCE `code_verifier` check (a replayed code is useless without the matching verifier).
- AC-F2.10: Refresh tokens are signed JWTs (`typ: "refresh"`, stateless) with a numeric, env-configurable TTL `LIBBY_MCP_OAUTH_REFRESH_TTL_SECONDS` (**default 2592000 seconds = 30 days**); access tokens use `LIBBY_MCP_OAUTH_ACCESS_TTL_SECONDS` (**default 3600 seconds**). Refresh rotation issues a new signed refresh JWT (fresh `jti`/`iat`). **Documented deviation (HIGH-6):** there is no replay detection — the previous refresh token remains valid until its own `exp`, and early revocation is only possible by rotating `LIBBY_MCP_OAUTH_SIGNING_KEY` (which invalidates all outstanding tokens). Tests assert the three TTL defaults and that overriding each env var changes the issued token's `exp`.
- AC-F2.11: The `/authorize` consent page **HTML-escapes every reflected value** (HIGH-C). Each request- or registration-sourced value rendered into the consent HTML — `state`, `client_name`, `redirect_uri`, `client_id`, `code_challenge`, `resource`, and any other reflected field — is passed through `html.escape(value, quote=True)` before being written into element text or an attribute (hidden-input `value="…"` attributes included); no reflected value is concatenated raw into the markup. **`client_name` is attacker-controlled** (it comes from the unauthenticated public DCR `POST /register` and is embedded in the signed `client_id`), so a `client_name` containing `<script>alert(1)</script>` MUST render as inert escaped text (`&lt;script&gt;…`) and MUST NOT execute. A test registers a client whose `client_name` contains `<script>`, drives `GET /authorize`, and asserts the rendered HTML contains the escaped form and no live `<script>` tag.
- AC-F2.12: The `GET /authorize` consent-page response (and any `POST /authorize` response that renders an HTML error page) sets the headers `Content-Security-Policy: frame-ancestors 'none'` (anti-clickjacking of the owner-secret form) and `Cache-Control: no-store` (the owner-secret form and reflected params are never cached), HIGH-C. These headers apply to the HTML consent/error responses; the JSON OAuth endpoints (`/register`, `/token`, metadata) are unaffected. A test asserts both headers are present on the `GET /authorize` response.

### FR-F6: Required OAuth Signing Key (Stateless AS)

**Description:** Because the server runs on Lambda (per-instance memory, cold
starts), the embedded AS cannot keep client/code/token state in memory. All
artifacts are signed with a single shared key so any instance validates artifacts
minted by any other instance.

**Acceptance Criteria:**
- AC-CRITICAL-2: A JWT access token issued by one Lambda instance passes validation on a different instance when both share `LIBBY_MCP_OAUTH_SIGNING_KEY` (no shared datastore required).
- AC-F6.1: `LIBBY_MCP_OAUTH_SIGNING_KEY` is **required** when `LIBBY_MCP_OAUTH_ENABLED=true`. It is a secret string of **at least 32 bytes** (measured as the UTF-8 byte length of the env-var value); the actual HMAC key used for all signed artifacts is `SHA-256(raw UTF-8 bytes of the secret)` (a fixed 32-byte key), so any sufficiently-long secret works and there is **no hex/base64 encoding requirement** (HIGH-9 — the old "hex or base64" rule was ambiguous because 64 hex chars are also valid base64). The server rejects a secret under 32 bytes at startup with a clear configuration error.
- AC-F6.2: When `LIBBY_MCP_OAUTH_ENABLED=true` and `LIBBY_MCP_OAUTH_SIGNING_KEY` is unset, the server **refuses to start** with a clear configuration error — it MUST NOT fall back to a random per-instance key.
- AC-F6.3: `client_id`s are signed tokens (HMAC-SHA256 over the public client payload — `redirect_uris`, `client_name`, `iat`, `typ: "client"`, `iss` — using `LIBBY_MCP_OAUTH_SIGNING_KEY`), so any instance validates a presented `client_id` without shared client state. **All clients are public** (PKCE); there is **no `client_secret`** in the payload or anywhere else (HIGH-8).
- AC-F6.4: JWT signing and verification use only the Python standard library (`hmac`, `hashlib`, `base64`, `json`); no third-party JWT dependency is added (HIGH-4, cross-ref AC-E4.3).
- AC-F6.5: Every signed artifact (client IDs, authorization codes, access tokens, refresh tokens) carries a `typ` claim (`client`/`code`/`access`/`refresh`) and an `iss` claim. Each verifier is invoked with the exact `typ` it expects and **rejects a token whose `typ` or `iss` does not match** — so a refresh token or an authorization code presented as a Bearer on `/mcp` is rejected, and an access token replayed at `/token` is rejected (HIGH-5). The JWT header `alg` is pinned to `HS256`: a token with `alg: none` or any other algorithm is rejected before the HMAC is computed.
- AC-F6.6: All signature and secret comparisons (artifact HMAC, owner secret, static API key) use `hmac.compare_digest` (constant-time); no non-constant-time `==` comparison is used on any secret or signature (HIGH-5).

### FR-F3: Discovery via Protected Resource Metadata (RFC 9728)

**Description:** Unauthenticated MCP requests advertise the authorization server
per RFC 9728 so clients can discover it automatically.

**Acceptance Criteria:**
- AC-F3.1: In OAuth mode, an unauthenticated (or invalid-token) `/mcp` request returns HTTP 401 with a `WWW-Authenticate: Bearer` header carrying `resource_metadata="<absolute URL of /.well-known/oauth-protected-resource>"`.
- AC-F3.2: The `resource_metadata` URL is absolute and built from `LIBBY_MCP_PUBLIC_BASE_URL` (per FR-F10) so it is correct and non-spoofable for both the Lambda endpoint and a Docker host; host-based derivation is used only when the resolved base URL is loopback.
- AC-F3.3: The protected-resource metadata document and the authorization-server metadata document are internally consistent (the PRM's `authorization_servers` entry resolves to the AS whose metadata `/.well-known/oauth-authorization-server` serves).

### FR-F4: Per-Client Compatibility

**Description:** The OAuth implementation works out of the box with the three
target clients.

**Acceptance Criteria:**
- AC-F4.1: **Claude.ai web** connects via Authorization Code + PKCE (S256) using RFC 9728 discovery and RFC 7591 Dynamic Client Registration, with the redirect URI `https://claude.ai/api/mcp/auth_callback` accepted — no manual client registration required by the user.
- AC-F4.2: **ChatGPT** connects via Authorization Code + PKCE (S256) using the same RFC 9728 discovery + RFC 7591 DCR path as the other clients (CIMD is **not** used — it is not part of the MCP 2025-06-18 spec); the server accepts the redirect URI `https://chatgpt.com/connector_platform_oauth_redirect` and handles the optional `resource=<mcp-server-url>` parameter on `/authorize` and `/token` per FR-F11 — a canonical `resource` is accepted, a missing one defaults to canonical (AC-F11.4), and a non-canonical one is rejected with `invalid_target` (AC-F11.3).
- AC-F4.3: **Cursor** connects via Authorization Code + PKCE (S256) with the redirect URI `cursor://anysphere.cursor-mcp/oauth/callback` (DCR); Cursor users who prefer static headers can still configure `mcp.json` with an `Authorization` header and no OAuth (static-Bearer mode).
- AC-F4.4: **Claude Desktop (local stdio)** is unaffected — it uses env-var credentials and no OAuth; no change is required for it.
- AC-F4.5: `client_credentials` grant is not offered (Claude.ai does not support it); only `authorization_code` and `refresh_token` are advertised and accepted.

### FR-F5: HTTPS / Transport Rules

**Acceptance Criteria:**
- AC-F5.1: In non-localhost deployments, all OAuth endpoints and the `resource_metadata` URL use `https`; `localhost` / `127.0.0.1` is exempt (allowed over `http`) for development.
- AC-F5.2: Token-endpoint error responses follow RFC 6749: an expired or unknown refresh token returns `error: "invalid_grant"`; a malformed request returns `error: "invalid_request"`; an invalid client credential returns `error: "invalid_client"`.
- AC-F5.3: Access tokens are validated on every `/mcp` request in OAuth mode (signature, expiry, and audience/resource binding to this MCP server — specifically `aud` must equal the canonical resource `<base>/mcp` per AC-F11.6).

### FR-F9: Redirect-URI Validation (HIGH-9)

**Description:** Redirect URIs are restricted by scheme, registered per client,
and exact-matched at `/authorize`.

**Acceptance Criteria:**
- AC-F9.1: `/authorize` with a missing or unregistered `redirect_uri` (not an exact string match of a value registered for the `client_id`) returns **HTTP 400** (an error page, not a redirect, per RFC 6749 §4.1.2.1).
- AC-F9.2: Allowed redirect-URI schemes are: `https://` for any host; `http://` for loopback only (`127.0.0.1`/`localhost`); and custom app schemes registered at DCR time (e.g. `cursor://`). A non-loopback `http://` redirect URI is rejected.
- AC-F9.3: Custom app schemes (e.g. `cursor://anysphere.cursor-mcp/oauth/callback`) are accepted at registration and at `/authorize` without being subject to the `https`-only rule that applies to web origins.

### FR-F10: Trusted Public Base URL (HIGH-10)

**Description:** Generated OAuth URLs must come from a configured base URL, not an
attacker-controllable Host header, in non-loopback deployments.

**Acceptance Criteria:**
- AC-F10.1: When `LIBBY_MCP_PUBLIC_BASE_URL` is unset and the request Host is not loopback, the OAuth routes return **HTTP 500 with `error: oauth_not_configured`**.
- AC-F10.2: When `LIBBY_MCP_PUBLIC_BASE_URL` is set, the server uses it verbatim for all generated URLs (both `/.well-known` URLs, the AS `issuer`, the PRM `resource`, and the `resource_metadata` challenge URL); the Host header is not used to derive these. Host-based derivation is allowed only when the resolved base URL is loopback.
- AC-F10.3: Docker/Lambda deployments document setting `LIBBY_MCP_PUBLIC_BASE_URL` to the public HTTPS URL, and (behind a TLS proxy) running uvicorn with `--proxy-headers --forwarded-allow-ips='*'`.

### FR-F11: Canonical Resource / Audience (RFC 8707) (HIGH-7)

**Description:** The MCP server defines a single canonical resource identifier
(RFC 8707 Resource Indicators). Every token's audience is pinned to it, and any
`resource` a client sends must equal it (after normalization) or be rejected — so
a token minted for this server can never be presented to, or confused with,
another resource.

**Acceptance Criteria:**
- AC-F11.1: The **canonical resource** is exactly `<base>/mcp`, where `<base>` is the resolved `LIBBY_MCP_PUBLIC_BASE_URL` origin (per FR-F10, loopback-derived only when the base is loopback). The PRM document's `resource` field equals this canonical value.
- AC-F11.2: **Trailing-slash normalization.** The canonical resource and any incoming `resource` are compared after normalizing a single optional trailing slash on the path (`<base>/mcp` and `<base>/mcp/` are equal; `<base>/mcp` is the canonical stored/advertised form). No other path, query, or fragment variation is treated as equal.
- AC-F11.3: Any request (`/authorize` or `/token`) carrying a `resource` parameter that does **not** equal the canonical resource (after AC-F11.2 normalization) is rejected with the OAuth error **`invalid_target`** (RFC 8707 §2) — a `400` on `/token`, and a `400` error page / `invalid_target` redirect error on `/authorize` (never a silently-issued token for a foreign audience).
- AC-F11.4: Every client **should** send `resource`; a **missing** `resource` on `/authorize` or `/token` **defaults to the canonical value** (not an error) so clients that omit it (and the single-resource self-host model) keep working. An explicitly-sent canonical value is accepted; an explicitly-sent non-canonical value is rejected per AC-F11.3.
- AC-F11.5: The `aud` claim of every issued token (access **and** refresh) and the `resource` bound into the authorization code are **always the canonical value**, regardless of what the client sent (a client cannot widen or redirect the audience by sending a different `resource` — it is either canonical or rejected).
- AC-F11.6: `/mcp` access-token validation checks that `aud` equals the canonical resource (in addition to signature, `exp`, and `typ`); a token whose `aud` is not the canonical value is rejected (401). This is the concrete form of the "audience/resource binding" in AC-F5.3.

---

## Non-Functional Requirements

### NFR-1: Backward Compatibility

- NFR-1.1: All v3 title-object and response additions are additive; no existing field is renamed or removed, except the deliberate FR-D1 change (per-title `{"error": "not_found"}` → `{"status": "not_owned"}`) and the FR-D4 `is_abridged` decision, both documented in the design.
- NFR-1.2: The single-library `search_titles` response shape is unchanged except for the additive `exact` filtering (FR-D6) and the FR-D3 `estimated_wait_days` correction.
- NFR-1.3: The existing static-Bearer, `POST /mcp` behavior on the live Lambda endpoint is byte-for-byte preserved (NFR-6.3, AC-E4.2).

### NFR-2: Performance

- NFR-2.1: Multi-library `get_availability` fan-out uses `asyncio.gather`, so N libraries complete in roughly the time of the slowest single library (parallel to `search_titles`).
- NFR-2.2: The slug directory for `lookup_library_slug` is fetched once per TTL window and cached in-process; concurrent callers during a cold window share one in-flight fetch via a module-level `asyncio.Lock` (not N parallel full walks); a lookup is an in-memory match within the TTL.
- NFR-2.3: OAuth token validation on `/mcp` requests must not add a network round-trip per request (the embedded AS validates the JWT locally with the in-process `LIBBY_MCP_OAUTH_SIGNING_KEY` — a stdlib HMAC check — rather than an introspection call per request).

### NFR-3: Reliability

- NFR-3.1: A single bad slug or upstream failure in a multi-library `get_availability` degrades gracefully per-library (AC-D7.5) rather than failing the whole request.
- NFR-3.2: All new derived-field logic (slug normalization, `not_owned` mapping, `estimated_wait_days` correction, `is_abridged`, exact filtering) handles missing/malformed Thunder fields without raising.
- NFR-3.3: A Thunder directory-fetch failure for `lookup_library_slug` serves a stale cached directory if present, else returns a human-readable error (AC-D5.8).

### NFR-4: Testing (Harness Engineering)

- NFR-4.1: Tests are updated/added for every v3 behavior change; all existing v1/v2 tests continue to pass.
- NFR-4.2: New unit tests cover: `not_owned` mapping (incl. partial-items, a non-numeric `title_id` classified `not_owned` without a Thunder call, and the 4xx bad-title-vs-bad-slug branch probed via `/v2/libraries/{slug}`), slug normalization, `estimated_wait_days==0`-when-available, `is_abridged` honesty, `lookup_library_slug` (match, no-match, cache hit, shared in-flight fetch, directory-fetch failure, bundled fallback), exact-title filtering (incl. page-2 match), multi-library `get_availability` (including per-library failure), the handler auto-detect branch, the three auth modes, the Lambda no-auth fail-closed case, each OAuth endpoint (PRM, AS metadata, DCR with a signed `client_id`, authorize PKCE enforcement + owner-secret gate + redirect-URI exact-match, token exchange, refresh rotation / `invalid_grant`), the stateless signed-token property (a token signed with a key validates under a second verifier sharing the key and fails under a different key), the signing-key-required startup refusal, the owner-secret-required startup refusal and the dev-auto-approve condition gating (CRITICAL-1: refused on Lambda, refused for non-loopback base URL, allowed only when the flag is set + non-Lambda + loopback), the typed-artifact / token-swap rejections (HIGH-5: a refresh token and an authorization code are each rejected as a Bearer on `/mcp`; an access token is rejected at `/token`; a token with `alg: none` or a non-`HS256` alg is rejected), the env-configurable TTL defaults (HIGH-6: access=3600s, refresh=2592000s, code=60s, and that overriding each env var changes the issued `exp`), and the `oauth_not_configured` base-URL guard.
- NFR-4.3: A Docker-mode test (or local-integration test) asserts the exact HTTP semantics (consistent with the 405 rule, HIGH-1): a **`POST /mcp` with a valid key** (and a well-formed `initialize` JSON-RPC body) returns **HTTP 200** with an MCP initialize response; a **`POST /mcp` without a key** when a key is set returns **HTTP 401**; a **`GET /mcp`** returns **HTTP 405** regardless of auth state (the method guard runs before any auth check). Startup is under 30s. A companion test (NFR-8.3) runs `_build_app()` in Docker mode with **every optional env var set to the empty string** and asserts it behaves identically to the all-unset case (no-auth `/mcp`, OAuth disabled, slug TTL default) — empty string never enables or misconfigures a mode (HIGH-2).
- NFR-4.4: `make preflight` (validate + compliance + ci) passes before merge, including the 80% coverage threshold, with ruff and mypy-strict clean.
- NFR-4.5: Test files for not-yet-implemented changes follow the repo's TDD red-phase convention (`pytest.importorskip` where applicable) so collection does not break on intermediate commits.
- NFR-4.6: `scripts/check_compliance.py` and/or a dedicated test assert the no-hardcoded-slug rule (NFR-6.1) and the single-handler rule (AC-E1.1).

### NFR-5: Fact-Finding Contract

- NFR-5.1: All v3 tools (including `lookup_library_slug`) return raw data only — no recommendations, rankings, or strategic advice.

### NFR-6: Portability & Shareability (Hard Constraints from Brief)

- NFR-6.1: No hardcoded references to `lcpl`, `fairfax`, `fcpl`, or any specific library slug as a behavioral branch in server code. Grep for these literals before shipping; they may appear only in example text in tool descriptions, error messages, and tests.
- NFR-6.2: The CDK stack stays cloud-agnostic — anyone deploying to their own AWS account needs zero changes to `infra/`; no account IDs, hostnames, or region constants tied to the current owner are embedded.
- NFR-6.3: The existing Lambda endpoint continues working unchanged after v3.
- NFR-6.4: Python version is consistent across the project: **minimum 3.11, runtime 3.12** (HIGH-11). The deployed CDK Lambda already targets `lambda.Runtime.PYTHON_3_12` and is **not** downgraded (a downgrade would break NFR-6.3 / AC-E4.1); the Docker image uses `python:3.12-slim` to match. `pyproject.toml` `requires-python` is `>=3.11`, and mypy/ruff `target-version` is 3.11 (the floor the code must stay compatible with). The embedded AS and OAuth code add **no new runtime dependency** — JWT uses the Python standard library (`hmac`/`hashlib`/`base64`/`json`); the only Docker-only dependency is uvicorn (not in the Lambda path).
- NFR-6.5: OAuth endpoints are served over HTTPS in all non-localhost deployments (AC-F5.1).

### NFR-7: Observability

- NFR-7.1: Per-library failures in multi-library `get_availability` are logged with the failing slug and status (CloudWatch), without leaking into other libraries' blocks beyond the per-library `error` field.
- NFR-7.2: OAuth events (registration, authorize, token issuance, refresh, validation failure) are logged without recording token secrets or `code_verifier` values in plaintext.

### NFR-8: Environment-Variable Configuration Semantics (HIGH-2)

- NFR-8.1: Every configuration environment variable the server reads is treated identically whether it is **unset** or set to an **empty string** (`""`). An empty-string value is normalized to "unset" and the variable's documented default (or absence-of-value behavior) applies. This holds for all config vars, including `LIBBY_MCP_API_KEY`, `LIBBY_MCP_OAUTH_ENABLED`, `LIBBY_MCP_OAUTH_SIGNING_KEY`, `LIBBY_MCP_OAUTH_OWNER_SECRET`, `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE`, `LIBBY_MCP_PUBLIC_BASE_URL`, `LIBBY_MCP_SLUG_TTL_SECONDS`, and the three OAuth TTL vars. Config is read through a single `_env(name)` helper that returns `None` for both the unset and empty-string cases, so no call site re-implements the check (this is why AC-F1.6/F1.8 say "set-and-non-empty" and "unset **or an empty string**").
- NFR-8.2: `docker-compose.yml` MUST NOT inject empty strings into the container for optional variables that are not provided by the operator. The naive `VAR: ${VAR:-}` form sets the container variable to `""` (present-but-empty), which — absent NFR-8.1 normalization — would read as "configured". The compose file therefore passes optional vars **conditionally** (only when the host env defines them) or relies on NFR-8.1 normalization treating the resulting `""` as unset. The two mechanisms together guarantee an operator who sets nothing gets exactly the documented defaults.
- NFR-8.3: A test runs `_build_app()` (Docker mode, `AWS_LAMBDA_RUNTIME_API` absent) with **every optional var explicitly set to the empty string** and asserts the server behaves identically to the all-unset case (no-auth `/mcp`, OAuth disabled, slug TTL default) — i.e. empty string never silently enables or misconfigures a mode.

---

## Acceptance Criteria (Feature-Level, from Brief)

**Bug fixes:**
- `get_availability(['bad-title-id'], 'lcpl')` returns `{"library_slug": "lcpl", "titles": [{"title_id": "bad-title-id", "status": "not_owned"}]}` (flat v2 shape), not a 500 (AC-D1.2).
- `search_titles('LCPL', ...)` → slug normalized to `lcpl` in all response fields (AC-D2.2).
- Available title → `estimated_wait_days == 0` (AC-D3.1).
- `is_abridged` either correctly derived or absent from the response schema (FR-D4).
- `lookup_library_slug('Fairfax')` returns slugs including `fairfax` (AC-D5.4).
- `search_titles('The Mom Test', exact=true)` returns only exact-match titles (AC-D6.3).
- `get_availability([ids], ['lcpl', 'fcpl'])` returns results for both libraries (AC-D7.3).

**Docker:**
- `docker compose up` starts in < 30s (AC-E3.4).
- `curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"curl\",\"version\":\"0\"}}}" http://localhost:8000/mcp` returns an MCP initialize response (HTTP 200) (AC-E2.3).
- `curl -X POST http://localhost:8000/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"curl\",\"version\":\"0\"}}}"` without the key, when a key is set, returns HTTP 401 (AC-E2.4).
- Existing CDK Lambda deploy still works; Mangum wraps when `AWS_LAMBDA_RUNTIME_API` is set (AC-E4.1).

**OAuth:**
- Claude.ai custom connector connects via OAuth DCR flow without manual client registration (AC-F4.1).
- ChatGPT MCP connector connects via the DCR + PKCE (S256) flow, tolerating the optional `resource` parameter (no CIMD) (AC-F4.2).
- Cursor connects via OAuth with just `url` in `mcp.json`, no token needed (AC-F4.3).
- Static Bearer still works alongside OAuth (AC-F1.4).
- `/.well-known/oauth-protected-resource` returns a valid RFC 9728 document (AC-F2.1).
- `/.well-known/oauth-authorization-server` returns metadata with `code_challenge_methods_supported: ["S256"]` (AC-F2.2).
- Token refresh works; server returns `invalid_grant` on an expired refresh token per RFC 6749 (AC-F5.2).

## Out of Scope

- GitHub Actions GHCR push workflow.
- Multi-arch Docker builds.
- OIDC scopes (`openid`, `profile`, `email`) — not needed for MCP tool access.
- mTLS client certificate verification (a ChatGPT feature; deferred).
- Rate limiting and caching infrastructure beyond the in-process slug-directory cache (separate spec/item).
- Multi-user / per-user library preferences.
- Any patron-authenticated OverDrive endpoints or write operations (hold placement, checkout).
- Recommendation / ranking logic inside the server (callers still reason).
- Cross-library deduplication or merged ranking of titles (results stay grouped per library).
