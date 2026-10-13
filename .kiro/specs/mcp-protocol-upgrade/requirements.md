# MCP Protocol Upgrade: Requirements

## Overview

Replace the custom JSON-RPC handler in libby-mcp with FastMCP (official MCP Python SDK) so that the server supports Streamable HTTP transport and can be added as a custom connector from Claude.ai web and Claude mobile. The existing three tools (search_titles, get_availability, get_deep_link), Bearer token authentication, and AWS Lambda deployment are preserved. The server runs in stateless mode (no in-memory session state) so it is compatible with Lambda's single-invocation execution model.

---

## Functional Requirements

### FR-1: Streamable HTTP Transport

**FR-1.1** The server shall expose a Streamable HTTP endpoint compatible with the MCP protocol, accepting requests from Claude.ai and Claude mobile custom connectors.

**Acceptance criteria:**
- AC-1.1.1: A Claude.ai web custom connector configured with the server URL, "Streamable HTTP" transport, and `Authorization: Bearer <key>` in the "Request headers" connector settings successfully connects (displays as "Connected" in Claude settings). Claude.ai supports static Bearer token headers via the connector configuration UI.
- AC-1.1.2: A Claude mobile custom connector configured with the same server URL and Bearer token header successfully connects and lists available tools.

**FR-1.2** The server shall handle stateless MCP requests (initialization, tool listing, tool calling) over HTTP. The server shall operate in stateless mode: no session state is stored in memory between invocations. Each request is self-contained.

**Acceptance criteria:**
- AC-1.2.1: An `initialize` request returns HTTP 200 with a JSON-encoded `InitializeResult` containing `protocolVersion`, `capabilities.tools`, and `serverInfo`.
- AC-1.2.2: A `tools/list` request returns HTTP 200 with the three tools (`search_titles`, `get_availability`, `get_deep_link`) and their full input schemas.
- AC-1.2.3: A `tools/call` request for each of the three tools returns HTTP 200 with a valid MCP content block containing `type: "text"`.
- AC-1.2.4: Two sequential requests to the same Lambda invocation (simulating warm start) both return HTTP 200 without error.

---

### FR-2: Tool Preservation

**FR-2.1** The server shall preserve the three existing tools — `search_titles`, `get_availability`, `get_deep_link` — with identical names, semantically equivalent input schemas, and identical behavior to v1.

**Acceptance criteria:**
- AC-2.1.1: Each tool's name and description (as returned by `tools/list`) matches the v1 `TOOL_DEFINITIONS` in `src/mcp_server.py`. Each tool's `inputSchema` has the same field names, required list, and compatible types (Pydantic-generated schema is acceptable if semantically equivalent).
- AC-2.1.2: Calling `search_titles` with a valid `library_slug` returns a result containing at least `items` from the OverDrive catalog (no `isError: true`).
- AC-2.1.3: Calling `get_availability` with a valid `library_slug` and `title_ids` returns availability data (no `isError: true`).
- AC-2.1.4: Calling `get_deep_link` with a valid `title_id` and `library_slug` returns a URL of the form `https://{slug}.overdrive.com/media/{id}` (no `isError: true`).
- AC-2.1.5: When a tool implementation in `src/tools/` returns `{"error": "..."}`, the MCP response must have `isError: true` in the content block.

**FR-2.2** The tool implementations in `src/tools/` and `src/thunder_client.py` shall not be modified.

**Acceptance criteria:**
- AC-2.2.1: No files under `src/tools/` or `src/thunder_client.py` are changed in this PR.

---

### FR-3: Bearer Token Authentication

**FR-3.1** The server shall require a valid `Authorization: Bearer <token>` header on every request, matching the `LIBBY_MCP_API_KEY` environment variable.

**Acceptance criteria:**
- AC-3.1.1: A request with a correct Bearer token is processed and returns HTTP 200 with a valid JSON-RPC response body (for non-notification requests).
- AC-3.1.2: A request with no `Authorization` header is rejected with HTTP 401 and body `{"error": "Invalid or missing API key"}`.
- AC-3.1.3: A request with an invalid Bearer token is rejected with HTTP 401 and body `{"error": "Invalid or missing API key"}`.
- AC-3.1.4: Bearer token comparison uses constant-time comparison (`hmac.compare_digest`) to prevent timing attacks.

**FR-3.2** The existing `validate_api_key(headers: dict[str, str]) -> bool` function in `src/auth.py` shall be preserved unchanged and used by the new auth middleware.

**Acceptance criteria:**
- AC-3.2.1: The `validate_api_key` function signature, docstring, and implementation are identical to v1.
- AC-3.2.2: A Claude.ai connector configured with `Authorization: Bearer <key>` in the "Request headers" connector settings successfully authenticates.

---

### FR-4: Lambda + API Gateway Compatibility

**FR-4.1** The server shall continue to run on AWS Lambda (Python 3.12, ARM64) and API Gateway HTTP API v2 without changes to the CDK infrastructure beyond adding a GET route.

**Acceptance criteria:**
- AC-4.1.1: `cdk deploy` succeeds with the updated `src/handler.py` using the existing `PythonFunction(entry="..", index="src/handler.py", handler="lambda_handler")` CDK configuration.
- AC-4.1.2: The deployed Lambda function handles API Gateway HTTP API v2 proxy events.

**FR-4.2** The Lambda entry point shall export a `lambda_handler` function compatible with the existing CDK stack configuration.

**Acceptance criteria:**
- AC-4.2.1: `src/handler.py` exports a callable `lambda_handler(event, context)` that serves as the Lambda entry point.

**FR-4.3** The API Gateway stack shall route both GET and POST HTTP methods on the `/mcp` path to the Lambda function. The `AuthMiddleware` rejects non-POST methods with 405 before checking auth, so both authenticated and unauthenticated GET requests receive 405. This ensures spec-compliant behavior (no 404 from API Gateway).

**Acceptance criteria:**
- AC-4.3.1: `infra/lib/libby-mcp-stack.ts` adds a `GET /mcp` route alongside the existing `POST /mcp` route, using the same Lambda integration.
- AC-4.3.2: An authenticated GET to the deployed endpoint returns HTTP 405.
- AC-4.3.3: An unauthenticated GET to the deployed endpoint returns HTTP 405 (method check runs before auth check).

---

### FR-5: FastMCP + Mangum Integration

**FR-5.1** The server shall use the `fastmcp` SDK (version 2.x) as the MCP protocol implementation. The ASGI app shall be created in stateless mode and wrapped in a Mangum adapter for Lambda compatibility.

**Acceptance criteria:**
- AC-5.1.1: `requirements.txt` includes `fastmcp>=2.10,<3`, `mangum>=0.19.0,<1`, and `httpx>=0.28.1,<0.29`.
- AC-5.1.2: `src/handler.py` creates a FastMCP app in stateless/JSON-response mode and wraps the authenticated ASGI app in `Mangum` to produce `lambda_handler`.
- AC-5.1.3: Two sequential calls to `lambda_handler` with synthetic API Gateway v2 events both return `statusCode: 200` without error (warm-start compatibility test).

**FR-5.2** The ThunderClient instance shall be initialized at module level and shared across warm Lambda invocations.

**Acceptance criteria:**
- AC-5.2.1: `ThunderClient()` is instantiated at module level in `src/handler.py`.

---

### FR-6: Dependency Bundling Fix

**FR-6.1** The `.dockerignore` file at the repo root shall not exclude `src/` or `requirements.txt`, so that PythonFunction can bundle Lambda dependencies correctly.

**Acceptance criteria:**
- AC-6.1.1: After reviewing the `.dockerignore`, any lines that would exclude `src/` or `requirements.txt` are removed.
- AC-6.1.2: `cdk synth` succeeds without bundling errors.

---

## Non-Functional Requirements

### NFR-1: Dependency Compatibility

**NFR-1.1** The pinned `httpx` version in `requirements.txt` shall be compatible with the installed `fastmcp` version.

**Acceptance criteria:**
- AC-NFR-1.1.1: `pip install -r requirements.txt` succeeds with no dependency conflicts.
- AC-NFR-1.1.2: All `test_thunder_client.py` tests pass after updating `httpx` to a version compatible with `fastmcp`.

### NFR-2: Test Coverage

**NFR-2.1** All existing unit tests for `src/tools/` and `src/thunder_client.py` shall pass without modification. Tests for `src/handler.py` shall be replaced with tests for the FastMCP-based handler using ASGI transport.

**Acceptance criteria:**
- AC-NFR-2.1.1: `pytest` exits 0 with no failures after the migration.
- AC-NFR-2.1.2: Tests for auth middleware verify: missing header → 401, invalid token → 401, valid token → request proceeds.
- AC-NFR-2.1.3: At least one `tools/call` test per tool verifies the tool returns a result without `isError: true`, with the Thunder API mocked.
- AC-NFR-2.1.4: One test verifies that a tool returning `{"error": "..."}` produces `isError: true` in the response.
- AC-NFR-2.1.5: Tests use `pytest-asyncio` (added to dev dependencies) for async test functions.

### NFR-3: Dependency Bundling

**NFR-3.1** PythonFunction (CDK) shall successfully bundle the updated `requirements.txt` during `cdk deploy`.

**Acceptance criteria:**
- AC-NFR-3.1.1: `cdk synth` succeeds without errors related to dependency bundling.
