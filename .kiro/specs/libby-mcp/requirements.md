# Requirements: Libby MCP Server

## Overview

An MCP server that exposes the OverDrive Thunder API as callable tools for Claude/Kiro, enabling natural language library book discovery across any OverDrive library without manual API calls.

## Reference

- #[[file:.kiro/specs/libby-mcp/context/thunder_openapi_v1.json]] — Partial OpenAPI 3.0.4 spec (121 paths)
- #[[file:.kiro/specs/libby-mcp/context/libby.py]] — Working Thunder API client
- #[[file:.kiro/specs/libby-mcp/context/libby_triage.py]] — Hold analysis and triage logic
- #[[file:.kiro/specs/libby-mcp/context/LIBBY_RECIPE.md]] — Endpoint documentation and workflow

## Functional Requirements

### FR-1: Search Titles Tool

**Description:** Provide a `search_titles` MCP tool that queries an OverDrive library's catalog via the Thunder API endpoint `GET /v2/libraries/{slug}/media`.

**Acceptance Criteria:**
- AC-1.1: Accepts `library_slug` as a required parameter identifying the OverDrive library (e.g., `lcpl`, `fairfaxcounty`, `nypl`).
- AC-1.2: Accepts optional `subjects` parameter as an array of subject IDs (strings or integers, e.g., `[24, 80]` or `["24", "80"]` for Fantasy + Sci-Fi; coerced to strings internally). Mapped to repeatable `subject` query params.
- AC-1.3: Accepts optional `format` parameter constrained to `audiobook` or `ebook`. Mapped to Thunder API `format` values `audiobook-overdrive` or `ebook-overdrive`.
- AC-1.4: Accepts optional `available` boolean parameter. When `true`, passes `showOnlyAvailable=true` to Thunder API (confirmed reliable server-side filter).
- AC-1.5: Accepts optional `creator` parameter for author/narrator name search. Mapped directly to Thunder API `creator` param.
- AC-1.6: Accepts optional `bisac` parameter for BISAC category code filtering (e.g., `FIC129000` for LitRPG). Applied using **belt-and-suspenders** strategy: sends `bisacCode` param to Thunder API server-side (works in many cases) AND filters client-side on response `bisacCodes[]` array as a safety net (guarantees correctness regardless of server-side behavior).
- AC-1.7: Accepts optional `series_id` parameter (integer) to filter to a specific series. Mapped to Thunder API `seriesId` param.
- AC-1.8: Accepts optional `under_hours` parameter for maximum audiobook length in hours. Applied as client-side filter: scans all `formats[]` entries for the first non-empty `duration` field (HH:MM:SS parsed to hours). Items without a duration in any format entry (e.g., ebooks) are INCLUDED when this filter is set — only items with a parseable duration exceeding the threshold are excluded.
- AC-1.9: Accepts optional `per_page` parameter (integer, default 24, max 100). Mapped directly to Thunder API `perPage`.
- AC-1.10: Accepts optional `page` parameter (integer, default 1) for pagination. Response includes total pages from `links.last.page`.
- AC-1.11: Accepts optional `maturity_level` parameter constrained to `general`, `juvenile`, `youngadult`, `adultonly`. Mapped to Thunder API `maturityLevel` (values: `generalcontent`, `juvenile`, `youngadult`, `adultonly`).
- AC-1.12: Accepts optional `sort_by` parameter for result ordering. Mapped to Thunder API `sortBy` (values: `mostpopular-site`, `newlyadded`, etc.).
- AC-1.13: Accepts optional `query` parameter for full-text search on title+subtitle+description. Mapped directly to Thunder API `query` param. Note: pollutes results — prefer `subjects` or `creator` for precision.
- AC-1.14: Returns an array of title objects containing: `id` (string), `title`, `creator`, `format`, `available` (boolean), `available_copies`, `owned_copies`, `holds_count`, `hours` (float, audio only), `bisac_codes`, `link` (deep link URL).
- AC-1.15: Returns pagination metadata: `total_items`, `page`, `total_pages`. Note: these reflect server-side results BEFORE client-side filtering (bisac, under_hours). A `filtered_count` field indicates how many titles remain after client-side filters.
- AC-1.16: When `available` is `false` or omitted, passes `availableFirst=true` so borrowable titles sort to the top.
- AC-1.17: Results match what the Libby app displays for equivalent searches (validated rule: `isAvailable == true` AND `availableCopies >= 1` = borrowable).
- AC-1.18: Returns `facets` object from the Thunder API response including available subject IDs and names, so callers can discover valid subject IDs for the queried library.

### FR-2: Get Availability Tool

**Description:** Provide a `get_availability` MCP tool that retrieves raw availability data for specific titles from the Thunder API's library-scoped availability endpoint (`GET /v2/libraries/{libraryKey}/media/availability?titleIds=...`). Returns factual copy/hold counts — no recommendations, rankings, or strategic advice. The LLM caller applies its own reasoning with taste profile context.

**Note:** The `/v2/media/bulk` endpoint returns title metadata (GlobalMediaResponse) but NOT dedicated availability fields. The correct endpoint is `/v2/libraries/{libraryKey}/media/availability?titleIds=...` which returns an AvailabilityResponse with `availableCopies`, `ownedCopies`, `holdsCount`, `estimatedWaitDays`.

**Acceptance Criteria:**
- AC-2.1: Accepts `library_slug` as a required parameter (used in the endpoint path).
- AC-2.2: Accepts `title_ids` as a required parameter — an array of OverDrive title IDs (strings or integers; integers are coerced to strings internally).
- AC-2.3: Fetches availability data via `GET /v2/libraries/{library_slug}/media/availability?titleIds={id1}&titleIds={id2}...`.
- AC-2.4: Returns raw availability data per title: `copies_owned`, `copies_available`, `holds_count`, `estimated_wait_days`.
- AC-2.5: Does NOT include recommendations, rankings, verdicts, or strategic advice — only factual data.
- AC-2.6: Handles titles not found gracefully (returns error indicator per title, not a full request failure).
- AC-2.7: Returns results for all requested title IDs in a single response.

### FR-3: Deep Link Generation Tool

**Description:** Provide a `get_deep_link` MCP tool that generates OverDrive web links for specific titles. These links open the correct edition in the library's OverDrive site (collision-proof via numeric ID).

**Acceptance Criteria:**
- AC-3.1: Accepts `title_id` as a required parameter (OverDrive title ID, string or integer — coerced to string internally).
- AC-3.2: Accepts `library_slug` as a required parameter for library context.
- AC-3.3: Returns a URL in the format `https://{library_slug}.overdrive.com/media/{title_id}` — this opens the exact edition, avoiding same-title collisions (e.g., two different books both called "Dragon Rider").
- AC-3.4: Missing parameters return clear validation errors.

### FR-4: Multi-Library Support

**Description:** All tools must work with any OverDrive library via the `library_slug` parameter (the Thunder API is the same for all libraries — just swap the slug).

**Acceptance Criteria:**
- AC-4.1: The Thunder API URL is constructed dynamically: `https://thunder.api.overdrive.com/v2/libraries/{library_slug}/media?{params}`.
- AC-4.2: No library-specific configuration is required — all libraries use the same API pattern and response format.
- AC-4.3: Invalid library slugs are detected using a best-effort heuristic: if Thunder returns HTTP 200 but `response.items` is empty AND `response.totalItems` is 0 AND no narrowing filters (subjects, creator, query, bisac, series_id) were applied, return an error: "Library '{slug}' not found or unavailable". Note: Thunder returns 200 for bad slugs (no explicit error code), so detection is best-effort. If narrowing filters are present, an empty result is a valid search outcome — not a library-not-found signal.
- AC-4.4: Known working slugs: `lcpl` (Loudoun County), `fairfaxcounty` (Fairfax County — needs verification), `nypl` (New York Public Library).

### FR-5: Authentication

**Description:** The MCP server validates incoming requests using a static API key. The Thunder API itself requires no authentication for catalog reads.

**Acceptance Criteria:**
- AC-5.1: The server reads the expected API key from an environment variable (sourced from AWS Secrets Manager).
- AC-5.2: Requests without a valid API key are rejected with a 401 response.
- AC-5.3: The API key is configured by the client in `~/.claude/mcp.json` or equivalent MCP configuration.
- AC-5.4: The Thunder API is called without any auth headers (public endpoint, no token needed).

### FR-6: MCP Protocol Compliance

**Description:** The server implements the MCP 2025-06-18 specification using Streamable HTTP transport.

**Acceptance Criteria:**
- AC-6.1: The server exposes a Streamable HTTP endpoint compatible with MCP 2025-06-18.
- AC-6.2: Tool definitions are discoverable via the MCP `tools/list` method.
- AC-6.3: Tool invocations are handled via the MCP `tools/call` method.
- AC-6.4: The server returns well-formed MCP JSON-RPC responses for all operations.
- AC-6.5: Tool results are returned as a JSON object in the `result.content` array using a single text content block with `type: "text"` and `text` containing the JSON-serialized tool output. This is the standard MCP tool result format.
- AC-6.6: The MVP Streamable HTTP transport implements the **request-response subset** only: (a) single `POST /mcp` endpoint accepts JSON-RPC requests and returns immediate JSON-RPC responses, (b) proper handling of `notifications/initialized` from the client after `initialize` response. No persistent SSE connections, no `Mcp-Session-Id` session management — each request is stateless and independent. HTTP response codes: `POST /mcp` with a JSON-RPC request returns HTTP 200; `POST /mcp` containing only notifications (no `id` field) returns HTTP 202 Accepted with an empty body; `GET /mcp` returns HTTP 404 Not Found (HTTP APIs do not support MOCK integrations for 405; 404 is accepted as equivalent for MVP — see NFR note below); `DELETE /mcp` returns HTTP 404 Not Found (same reason).
- AC-6.7: Full Streamable HTTP capabilities (server-initiated SSE via GET or long-lived POST, `Mcp-Session-Id` session persistence, server-to-client notifications) are deferred to a future non-Lambda iteration if needed.
- AC-6.8: The `initialize` response MUST return: `{protocolVersion: "2025-06-18", capabilities: {tools: {}}, serverInfo: {name: "libby-mcp", version: "1.0.0"}}`. This is the InitializeResult per the MCP spec.
- AC-6.9: The server MUST handle `ping` requests by returning an empty result object `{}` (NOT a -32601 method-not-found error). Ping is a standard MCP lifecycle method.

## Non-Functional Requirements

### NFR-1: Performance

- NFR-1.1: Cold start latency must be under 3 seconds.
- NFR-1.2: Warm invocation latency must be under 500ms.
- NFR-1.3: The server must handle concurrent requests without blocking.

### NFR-2: Cost Efficiency

- NFR-2.1: Infrastructure cost must remain under $1/month at personal use levels (single user, ~50 queries/day).
- NFR-2.2: Lambda function memory should be sized minimally for the workload.

### NFR-3: Rate Limiting & Respectful Usage

- NFR-3.1: The server must not perform bulk scraping against the Thunder API.
- NFR-3.2: Requests to the Thunder API should include a `User-Agent` header (e.g., `libby-mcp/1.0` — matching the pattern in libby.py).
- NFR-3.3: Rate limiting is handled exclusively by API Gateway throttling (10 req/s burst, 5 req/s sustained). No in-process rate limiting for MVP — API Gateway throttling is sufficient for single-user personal use and avoids implementation contradictions. In-process rate limiting is a future enhancement if multi-tenant support is added.
- NFR-3.4: Thunder API requests use a 10-second timeout (see NFR-6.2). The original libby.py uses 30s because it runs interactively; the MCP server uses a shorter timeout within the Lambda execution budget.

### NFR-4: Observability

- NFR-4.1: All tool invocations must be logged to CloudWatch with request/response metadata.
- NFR-4.2: Errors must be logged with sufficient context for debugging.
- NFR-4.3: Latency metrics must be emitted for Thunder API calls.

### NFR-5: Security

- NFR-5.1: API keys must never be logged or returned in responses.
- NFR-5.2: The MCP API key must be stored in AWS Secrets Manager.
- NFR-5.3: The Lambda function must follow least-privilege IAM policies.

### NFR-6: Reliability

- NFR-6.1: Thunder API failures must return graceful error messages, not stack traces.
- NFR-6.2: HTTP client timeout to the Thunder API must be 10 seconds. Lambda timeout is 30 seconds, providing a 20-second margin for graceful error handling if Thunder hangs.
- NFR-6.3: The server should handle malformed Thunder API responses without crashing.
- NFR-6.4: Client-side filtering (bisac, under_hours) must gracefully handle missing fields (e.g., ebooks have no duration — they are included, not excluded, when under_hours is set).

### NFR-7: Harness Engineering

The project follows harness engineering principles for agent-driven development. The agent validates its own work through tests, CI, and structured repo knowledge. The repo is the system of record.

#### NFR-7.1: Test-Driven Foundation

- NFR-7.1.1: Core functionality must have tests written BEFORE implementation code.
- NFR-7.1.2: Tests define acceptance criteria mechanically — not just in documentation.
- NFR-7.1.3: The agent can validate its own work by running tests locally (`pytest`).
- NFR-7.1.4: Each task produces testable output that can be verified independently.

#### NFR-7.2: CI Integration with Agent Feedback Loop

- NFR-7.2.1: A GitHub Actions workflow must run on every PR.
- NFR-7.2.2: CI must post status back to the PR (pass/fail with details).
- NFR-7.2.3: The agent-router can see CI status via the GitHub API.
- NFR-7.2.4: PRs cannot merge until CI passes (branch protection).
- NFR-7.2.5: CI runs: lint (ruff), type check (mypy), unit tests (pytest), integration tests (scheduled only).

#### NFR-7.3: Integration Testing Environment

- NFR-7.3.1: Integration tests run against the real Thunder API (rate-limited).
- NFR-7.3.2: Tests validate actual response shapes match the spec.
- NFR-7.3.3: Failures provide actionable feedback for agent iteration (remediation hints in error messages).

#### NFR-7.4: Repository Knowledge Structure

- NFR-7.4.1: `AGENTS.md` at the repo root serves as a table of contents (not an encyclopedia).
- NFR-7.4.2: A `docs/` directory with `design-docs/` (this spec) and `references/` (Thunder API docs, MCP spec excerpts).
- NFR-7.4.3: Linters enforce architecture constraints mechanically (ruff rules, import ordering).
- NFR-7.4.4: The agent can reason about the full domain from the repo alone — no external tribal knowledge required.

### NFR-8: Infrastructure as Code

- NFR-8.1: All AWS resources must be defined in AWS CDK (TypeScript).
- NFR-8.2: CDK stack must be deployable with `cdk deploy` from a clean checkout.
- NFR-8.3: Stack must be destroyable with `cdk destroy` without orphaned resources.
- NFR-8.4: Infrastructure changes must go through the same PR review process as application code.

## Out of Scope

- Taste profile storage or processing within the MCP server (the LLM caller uses NONFICTION_TASTE_PROFILE.md from its own context)
- User accounts or multi-tenant architecture
- Billing or payment processing
- Cognito authentication integration
- Actual hold placement (write operations against OverDrive)
- Checkout integration
- Reading history synchronization
- Patron-authenticated endpoints (anything under `/patrons/me/` that requires Bearer token)
- `magazine` format support (BRIEF mentions it but Thunder API `format` values are limited to `audiobook-overdrive` and `ebook-overdrive` for reliable filtering; deferred to future release pending API investigation)
- `coverUrl` field in title response (BRIEF mentions it in search_titles return shape but not prioritized for MVP; deferred to future release)
- Per-title `subjects` field in search response (BRIEF specifies subjects in the return shape, but the `facets.subjects` array already provides the library-wide subject vocabulary with IDs and names that callers need for subsequent queries; per-title subject associations are not reliably available as structured data in Thunder API item responses and would require additional API calls per title to resolve — not justified for MVP)
