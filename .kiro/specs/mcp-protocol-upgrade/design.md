# MCP Protocol Upgrade: Design

## Overview

This document describes the architecture for migrating libby-mcp from a custom JSON-RPC handler to FastMCP 2.x with Streamable HTTP transport. The migration replaces `src/handler.py` and `src/mcp_server.py`, extends `src/auth.py` with ASGI middleware, updates `requirements.txt`, and adds a GET route to the CDK stack.

**Why this fixes the Claude.ai connection problem**: libby-mcp v1 is a plain JSON-RPC POST handler; it does not implement the MCP Streamable HTTP protocol. Claude.ai requires Streamable HTTP. FastMCP 2.x implements this natively via `mcp.http_app()`. Bearer token authentication continues to work: Claude.ai supports static `Authorization` request headers in the connector configuration UI ("Request headers" → `authorization: Bearer <key>`).

**Stateless mode + per-invocation app construction**: FastMCP's Streamable HTTP mode uses a session manager that can only be initialized once per instance. Lambda containers can serve multiple warm requests, but also handle requests on different containers, making in-memory session state unreliable. The solution is to construct the ASGI app fresh on each invocation (inside `lambda_handler`). `mcp.http_app()` is cheap (no I/O) so this adds negligible overhead. Mangum is called with `lifespan="on"` so startup failures are surfaced rather than silently skipped.

---

## Architecture

### v1 Architecture (current)

```
API Gateway HTTP API (POST /mcp only)
  └─> Lambda: src/handler.py::lambda_handler
        ├─> src/auth.py: validate_api_key()
        ├─> src/mcp_server.py: MCPServer.handle_request()  ← custom JSON-RPC
        └─> src/tools/{search_titles,availability,deep_link}.py
```

### v2 Architecture (this spec)

```
API Gateway HTTP API (GET /mcp → 405, POST /mcp → processed)
  └─> Lambda: src/handler.py::lambda_handler
        └─> Per-invocation app construction:
              AuthMiddleware (ASGI)  ← validate_api_key() on every HTTP request
                └─> FastMCP HTTP ASGI app (stateless_http=True, json_response=True)
                      └─> @mcp.tool() functions (sync, called inline or threadpool)
                            └─> src/tools/{search_titles,availability,deep_link}.py
                                  └─> src/thunder_client.py: ThunderClient
```

---

## Component Design

### 1. `src/handler.py` (rewritten)

**Key design decisions:**

1. **Module-level** `ThunderClient()` for warm-start reuse.
2. **Module-level** `FastMCP("libby-mcp")` with `@mcp.tool()` registrations — these are stable and safe to share.
3. **Per-invocation** ASGI app construction via `_build_app()` to avoid the session manager being called more than once per instance.
4. `Mangum(lifespan="on")` — runs ASGI startup/shutdown correctly and surfaces failures.
5. Tool error propagation via `ToolError` (from `fastmcp.exceptions`).
6. A GET method guard in `AuthMiddleware` returns 405 with `Allow: POST` for all non-POST requests, ensuring spec-compliant behavior regardless of how FastMCP handles GET in different SDK versions.

```python
# src/handler.py (authoritative structure — abbreviated, see Task 4 for full code)
from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mangum import Mangum
from pydantic import Field

from src.auth import AuthMiddleware
from src.thunder_client import ThunderClient
from src.tools import availability as availability_impl
from src.tools import deep_link as deep_link_impl
from src.tools import search_titles as search_titles_impl

# Module-level — reused across warm invocations
_thunder_client = ThunderClient()

mcp = FastMCP("libby-mcp")


@mcp.tool(description=(
    "Search a library's OverDrive catalog via the Thunder API. Returns titles "
    "with availability info, deep links, and copy/hold counts. Also returns facets "
    "(including valid subject IDs for the library)."
))
def search_titles(
    library_slug: Annotated[str, Field(description="Library identifier (e.g., lcpl, fairfaxcounty, nypl)")],
    # ... all optional params with Annotated[T | None, Field(description="...")] = None ...
) -> dict:
    args: dict = {"library_slug": library_slug}
    # ... build args dict, skipping None values ...
    result = search_titles_impl.handle(args, _thunder_client)
    if "error" in result:
        raise ToolError(result["error"])
    return result


# ... get_availability and get_deep_link defined the same way ...


def _build_app():
    """Build a fresh ASGI app instance for each invocation.

    FastMCP's session manager can only run() once per instance, so
    building fresh ensures each Lambda invocation gets a clean lifecycle.
    """
    fastmcp_app = mcp.http_app(
        path="/mcp",
        transport="http",
        stateless_http=True,
        json_response=True,
    )
    return AuthMiddleware(fastmcp_app)


def lambda_handler(event, context):
    """AWS Lambda entry point."""
    return Mangum(_build_app(), lifespan="on")(event, context)
```

**`mcp.http_app()` parameters:**
- `path="/mcp"` — must match the API Gateway route
- `transport="http"` — Streamable HTTP transport
- `stateless_http=True` — no in-memory session state
- `json_response=True` — return JSON instead of SSE streams

**`ToolError` import**: `from fastmcp.exceptions import ToolError` (NOT `from mcp.server.fastmcp import ToolError`).

**Tool descriptions**: Use `@mcp.tool(description="<exact v1 string>")` with the exact single-line descriptions from v1 `TOOL_DEFINITIONS` to ensure match. Per-parameter descriptions use `Annotated[T, Field(description="...")]`.

---

### 2. `src/auth.py` (extended — existing function unchanged)

The existing `validate_api_key(headers: dict[str, str]) -> bool` function is **preserved exactly as-is**. A new `AuthMiddleware` ASGI class is added below it.

**Added responsibilities vs. the original design:**
- Rejects any non-POST HTTP method with `405 Method Not Allowed` + `Allow: POST` before auth check. This provides consistent, spec-compliant behavior for GET requests regardless of FastMCP SDK version.
- Returns `WWW-Authenticate: Bearer` on 401 responses.
- Decodes headers with `latin-1` to prevent `UnicodeDecodeError` on non-UTF-8 bytes.
- Non-HTTP scopes (`lifespan`) pass through without auth or method check.

```python
class AuthMiddleware:
    """ASGI middleware: method guard (POST only) + mandatory Bearer token auth.

    Enforced order:
    1. Non-HTTP scopes (lifespan): pass through.
    2. Non-POST HTTP methods: 405 Method Not Allowed.
    3. POST with invalid/absent auth: 401.
    4. POST with valid auth: delegate to wrapped app.

    Claude.ai supports static Bearer headers via connector UI
    (Request headers → authorization: Bearer <key>).
    """
    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        method = scope.get("method", "").upper()
        if method != "POST":
            await _send_simple(send, 405, {"error": "Method not allowed"},
                               extra_headers=[[b"allow", b"POST"]])
            return
        headers = {k.decode("latin-1"): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if not validate_api_key(headers):
            await _send_simple(send, 401, {"error": "Invalid or missing API key"},
                               extra_headers=[[b"www-authenticate", b"Bearer"]])
            return
        await self._app(scope, receive, send)


async def _send_simple(send, status, body_dict, extra_headers=None):
    import json
    body = json.dumps(body_dict).encode()
    headers = [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
```

---

### 3. `src/mcp_server.py` (deleted)

`MCPServer` is replaced by FastMCP. Delete the file.

---

### 4. `requirements.txt` (updated)

```
httpx>=0.28.1,<0.29
fastmcp>=2.10,<3
mangum>=0.19.0,<1
```

- `httpx`: updated from `0.27.2` because `fastmcp` 2.x requires `>=0.28.1`.
- `fastmcp`: floor raised to `2.10` because 2.0–2.2 have no Streamable HTTP and early 2.3.x had a different `http_app` signature. `<3` prevents silent breakage on major version bump.
- `mangum`: `>=0.19` avoids the deprecated `asyncio.get_event_loop()` call in 0.17.x that raises a DeprecationWarning on Python 3.12.

---

### 5. CDK stack: `infra/lib/libby-mcp-stack.ts` (GET route added)

A `GET /mcp` route is added so that the API Gateway routes GET requests to the Lambda instead of returning 404. The `AuthMiddleware` rejects all non-POST methods with 405 before checking auth — so both authenticated and unauthenticated GET requests receive 405 Method Not Allowed. This is spec-compliant: the MCP server correctly indicates the method is not supported.

```typescript
httpApi.addRoutes({
  path: "/mcp",
  methods: [apigatewayv2.HttpMethod.GET],
  integration: lambdaIntegration,
});
```

---

### 6. `.dockerignore` (reviewed and fixed)

Review the repo's `.dockerignore` and remove any patterns that exclude `src/`, `requirements.txt`, or other files needed for PythonFunction bundling. Common problematic patterns: `src`, `src/`, `*.py`, `requirements*.txt`. Patterns that should stay: `infra/`, `infra/node_modules`, `cdk.out`, `.git/`, `__pycache__`.

---

## Authentication Design

### Decision: Mandatory Bearer Token (unchanged from v1)

| Caller | Authorization header | Method | Result |
|--------|---------------------|--------|--------|
| Claude.ai / Claude mobile (configured with Bearer key) | `Bearer <valid>` | POST | Allowed |
| Kiro / direct API caller | `Bearer <valid>` | POST | Allowed |
| Any caller | Any or absent | POST, invalid auth | HTTP 401 |
| Any caller | Any or absent | GET (or non-POST) | HTTP 405 (method check runs before auth) |

---

## FastMCP Tool Registration

FastMCP 2.x derives tool `inputSchema` from Python type annotations and `Field(description=...)`. Tool names come from the function name. Tool descriptions come from `@mcp.tool(description=...)`.

**Important**: snapshot v1 `TOOL_DEFINITIONS` tool names, descriptions, and inputSchema fields into a test fixture file before deleting `src/mcp_server.py`. Use this fixture in a `tools/list` comparison test.

### Schema compatibility approach

Use `@mcp.tool(description="<exact v1 string>")` and `Annotated[T, Field(description="...")]` per parameter. FastMCP/Pydantic generates semantically equivalent schemas (same field names, required list, types); the exact JSON structure (anyOf vs oneOf) will differ but is functionally equivalent for MCP clients.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Non-POST HTTP method (e.g., GET — any auth state) | HTTP 405, `{"error": "Method not allowed"}`, `Allow: POST` |
| Absent `Authorization` header | HTTP 401, `{"error": "Invalid or missing API key"}`, `WWW-Authenticate: Bearer` |
| Invalid Bearer token | HTTP 401, `{"error": "Invalid or missing API key"}`, `WWW-Authenticate: Bearer` |
| Valid Bearer token, POST | Request proceeds to FastMCP |
| Tool returns `{"error": "..."}` | `ToolError` raised → FastMCP returns `isError: true` content block |
| Thunder API timeout | `src/tools/` returns `{"error": "..."}` → `ToolError` → `isError: true` |
| Malformed JSON body | FastMCP / Mangum returns HTTP 400 |
| Lambda timeout (30s) | API Gateway returns HTTP 504 |
| `LIBBY_MCP_API_KEY` env var unset | `validate_api_key()` returns False → HTTP 401 |

---

## Test Design

Tests use `httpx.AsyncClient` with `asgi_lifespan.LifespanManager` to correctly run ASGI startup/shutdown (required for FastMCP's session manager). `pytest-asyncio` with `asyncio_mode = auto` is required. `asgi-lifespan` is added to dev dependencies.

All requests send `Accept: application/json, text/event-stream` to ensure compatibility across FastMCP SDK versions.

A sync warm-start test calls `lambda_handler` directly twice with synthetic API Gateway v2 events to verify AC-1.2.4 and AC-5.1.3.

---

## Project Structure After Migration

```
libby-mcp/
├── src/
│   ├── handler.py        ← REWRITTEN (FastMCP + Mangum + AuthMiddleware)
│   ├── auth.py           ← EXTENDED (add AuthMiddleware + _send_simple; validate_api_key unchanged)
│   ├── mcp_server.py     ← DELETED
│   ├── thunder_client.py ← UNCHANGED
│   └── tools/            ← UNCHANGED
│       ├── __init__.py
│       ├── search_titles.py
│       ├── availability.py
│       └── deep_link.py
├── requirements.txt      ← UPDATED
├── .dockerignore         ← REVIEWED/FIXED
├── infra/
│   └── lib/
│       └── libby-mcp-stack.ts  ← UPDATED (add GET /mcp route)
└── tests/
    ├── test_auth.py           ← EXTENDED (AuthMiddleware + method guard tests)
    ├── test_handler.py        ← REWRITTEN (ASGI + warm-start tests)
    ├── test_mcp_server.py     ← DELETED (if exists)
    ├── test_tools.py          ← UNCHANGED
    └── test_thunder_client.py ← UNCHANGED
```

---

## Migration Compatibility Notes

| Client | v1 | v2 |
|--------|----|----|
| Claude.ai custom connector | ❌ (transport mismatch) | ✅ |
| Claude mobile | ❌ (transport mismatch) | ✅ |
| Kiro MCP client | ✅ | ✅ |
| Raw JSON-RPC POST | ✅ | ❌ (breaking) |
