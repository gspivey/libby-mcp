# MCP Protocol Upgrade: Tasks

## Overview

Implementation tasks for migrating libby-mcp from a custom JSON-RPC handler to FastMCP 2.x with Streamable HTTP transport.

**PR Boundary:**
- **PR A** (Tasks 1-6): Backend migration — requirements, .dockerignore, auth middleware, handler rewrite, delete mcp_server.py, update tests.
- **PR B** (Task 7): CDK GET route + deploy + verification.

---

## Task 1: Check and fix `.dockerignore`

**Implements**: FR-6.1 (AC-6.1.1, AC-6.1.2)

Read the current `.dockerignore` at the repo root. Remove any lines that exclude `src/` or `requirements.txt` from PythonFunction bundling. Problematic patterns include: `src`, `src/`, `*.py`, `requirements*.txt`, `**/*.py`.

Patterns to preserve: `infra/`, `infra/node_modules`, `cdk.out/`, `.git/`, `__pycache__/`, `*.pyc`.

After editing, verify no src/requirements exclusions remain:
```bash
grep -E "^(src|src/|\*.py|requirements)" .dockerignore
# should return nothing
```

Also verify that relaxing `.dockerignore` doesn't pull in large directories — inspect the CDK asset after synth:
```bash
cdk synth && ls -lh cdk.out/asset.*/
# confirm infra/node_modules and cdk.out are not included
```

---

## Task 2: Update `requirements.txt`

**Implements**: FR-5.1 (AC-5.1.1), NFR-1.1 (AC-NFR-1.1.1)

Replace the contents of `requirements.txt`:

```
httpx>=0.28.1,<0.29
fastmcp>=2.10,<3
mangum>=0.19.0,<1
```

Key changes:
- `httpx` updated from `0.27.2` — fastmcp 2.x requires `>=0.28.1`. This is a pin-breaking change; run all `test_thunder_client.py` tests after installing.
- `fastmcp` floor is `2.10` — versions 2.0–2.2 lack Streamable HTTP; early 2.3.x has an incompatible `http_app` signature.
- `mangum>=0.19.0` — avoids deprecated `asyncio.get_event_loop()` on Python 3.12.

Verify:
```bash
pip install -r requirements.txt
# must complete with no dependency conflicts
```

---

## Task 3: Add `AuthMiddleware` to `src/auth.py`

**Implements**: FR-3.1, FR-3.2, FR-4.3 (all auth and method-guard ACs)

Add imports at the top of `src/auth.py` if not present:
```python
import json
from typing import Any, Callable
```

Add the following **below** the existing `validate_api_key` function. Do NOT modify anything above it.

```python
class AuthMiddleware:
    """ASGI middleware: POST-only guard + mandatory Bearer token auth.

    Enforced order:
    1. Non-HTTP scopes (lifespan): pass through.
    2. Non-POST HTTP methods: HTTP 405 Method Not Allowed.
    3. POST with invalid/absent Authorization header: HTTP 401.
    4. POST with valid Bearer token: delegate to wrapped app.

    Claude.ai supports static Bearer headers via its connector UI
    ("Request headers" → "authorization: Bearer <key>").
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method != "POST":
            await _send_simple(
                send, 405, {"error": "Method not allowed"},
                extra_headers=[[b"allow", b"POST"]],
            )
            return

        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        if not validate_api_key(headers):
            await _send_simple(
                send, 401, {"error": "Invalid or missing API key"},
                extra_headers=[[b"www-authenticate", b"Bearer"]],
            )
            return

        await self._app(scope, receive, send)


async def _send_simple(
    send: Callable,
    status: int,
    body_dict: dict,
    extra_headers: list | None = None,
) -> None:
    """Send a simple JSON HTTP response."""
    body = json.dumps(body_dict).encode()
    headers: list = [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
```

---

## Task 4: Rewrite `src/handler.py`

**Implements**: FR-1.2, FR-2.1, FR-3.1, FR-4.2, FR-5.1, FR-5.2 (all relevant ACs)

Before starting: copy the exact `name`, `description`, and `inputSchema` fields from all three entries in `TOOL_DEFINITIONS` in `src/mcp_server.py` into a test fixture file `tests/fixtures/v1_tool_definitions.json` — needed in Task 6c before `src/mcp_server.py` is deleted.

Delete the entire existing content of `src/handler.py` and replace with:

```python
"""Lambda handler — FastMCP Streamable HTTP transport over Mangum ASGI adapter.

Uses stateless mode (no in-memory session state per invocation) for Lambda
compatibility. Builds a fresh ASGI app per invocation to avoid FastMCP's
session manager being called more than once on warm containers.
Bearer token auth is enforced via AuthMiddleware on every request.
"""
from __future__ import annotations

import json
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

# Module-level — reused across warm Lambda invocations
_thunder_client = ThunderClient()

mcp = FastMCP("libby-mcp")


@mcp.tool(description=(
    "Search a library's OverDrive catalog via the Thunder API. Returns titles "
    "with availability info, deep links, and copy/hold counts. Also returns facets "
    "(including valid subject IDs for the library)."
))
def search_titles(
    library_slug: Annotated[str, Field(description="Library identifier (e.g., lcpl, fairfaxcounty, nypl)")],
    subjects: Annotated[list[str | int] | None, Field(description="Subject IDs to filter by")] = None,
    format: Annotated[Literal["audiobook", "ebook"] | None, Field(description="Media format filter")] = None,
    available: Annotated[bool | None, Field(description="Only show currently borrowable titles")] = None,
    creator: Annotated[str | None, Field(description="Author or narrator name search")] = None,
    bisac: Annotated[str | None, Field(description="BISAC category code to filter by")] = None,
    series_id: Annotated[int | None, Field(description="Filter to a specific series by ID")] = None,
    under_hours: Annotated[float | None, Field(description="Maximum audiobook length in hours (client-side filter)")] = None,
    per_page: Annotated[int | None, Field(description="Results per page (max 100)")] = None,
    page: Annotated[int | None, Field(description="Page number for pagination")] = None,
    maturity_level: Annotated[
        Literal["general", "juvenile", "youngadult", "adultonly"] | None,
        Field(description="Content maturity filter"),
    ] = None,
    sort_by: Annotated[str | None, Field(description="Sort order")] = None,
    query: Annotated[str | None, Field(description="Full-text search on title+subtitle+description")] = None,
) -> dict:
    args: dict = {"library_slug": library_slug}
    for k, v in {
        "subjects": subjects,
        "format": format,  # noqa: A002  (shadows builtin — intentional, matches v1 schema)
        "available": available,
        "creator": creator,
        "bisac": bisac,
        "series_id": series_id,
        "under_hours": under_hours,
        "per_page": per_page,
        "page": page,
        "maturity_level": maturity_level,
        "sort_by": sort_by,
        "query": query,
    }.items():
        if v is not None:
            args[k] = v
    result = search_titles_impl.handle(args, _thunder_client)
    if "error" in result:
        raise ToolError(result["error"])
    return result


@mcp.tool(description=(
    "Get raw availability data for specific titles at a library. "
    "Returns copies owned, copies available, holds count, and estimated wait days."
))
def get_availability(
    library_slug: Annotated[str, Field(description="Library identifier")],
    title_ids: Annotated[
        list[str | int],
        Field(description="Array of OverDrive title IDs to check"),
    ],
) -> dict:
    result = availability_impl.handle(
        {"library_slug": library_slug, "title_ids": title_ids}, _thunder_client
    )
    if "error" in result:
        raise ToolError(result["error"])
    return result


@mcp.tool(description=(
    "Generate a direct OverDrive link for a specific title. "
    "Format: https://{slug}.overdrive.com/media/{id}"
))
def get_deep_link(
    title_id: Annotated[str | int, Field(description="OverDrive numeric title ID")],
    library_slug: Annotated[str, Field(description="Library slug for the link context")],
) -> dict:
    result = deep_link_impl.handle({"title_id": title_id, "library_slug": library_slug})
    if "error" in result:
        raise ToolError(result["error"])
    return result


def _build_app():
    """Build a fresh ASGI app instance for each Lambda invocation.

    FastMCP's session manager calls run() exactly once per instance.
    Building fresh here ensures each invocation (including warm ones)
    gets a clean lifecycle without the second-invocation failure.
    """
    fastmcp_app = mcp.http_app(
        path="/mcp",
        transport="http",
        stateless_http=True,
        json_response=True,
    )
    return AuthMiddleware(fastmcp_app)


def lambda_handler(event, context):
    """AWS Lambda entry point for API Gateway HTTP API v2 events."""
    return Mangum(_build_app(), lifespan="on")(event, context)
```

**Note on `mcp.http_app()` signature**: If the installed `fastmcp` version does not accept `transport`, `stateless_http`, and `json_response` as keyword arguments, check `fastmcp` changelog and use the API available in the installed version `>=2.10`. The required behavior is: no SSE streaming, JSON-only responses, no cross-invocation session state. Do not fall back to the `mcp` SDK's `streamable_http_app()` — it does not accept these parameters and has different lifespan behavior.

---

## Task 5: Delete `src/mcp_server.py` and `tests/test_mcp_server.py`

**Implements**: Design (MCPServer removed)

Ensure Task 4's fixture copy step is complete before this task.

Delete `src/mcp_server.py`. Also delete `tests/test_mcp_server.py` if it exists.

Verify no remaining imports:
```bash
grep -r "from src.mcp_server\|import mcp_server" src/ tests/
# must return nothing
```

---

## Task 6: Update tests

**Implements**: NFR-2.1 (AC-NFR-2.1.1–2.1.5)

### 6a. Add dev dependencies

Add to `requirements-dev.txt` (or equivalent):
```
pytest-asyncio>=0.23.0
asgi-lifespan>=2.1.0
```

In `setup.cfg`, `pytest.ini`, OR `pyproject.toml` (use whichever is active — check existing config):
```ini
[tool:pytest]
asyncio_mode = auto
```

### 6b. Rewrite `tests/test_handler.py`

```python
"""Tests for src/handler.py — FastMCP ASGI handler and Lambda warm-start."""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from asgi_lifespan import LifespanManager

from src.handler import _build_app, lambda_handler

VALID_TOKEN = "test-api-key"
BASE_HEADERS = {
    "Authorization": f"Bearer {VALID_TOKEN}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.fixture(autouse=True)
def set_api_key(monkeypatch):
    monkeypatch.setenv("LIBBY_MCP_API_KEY", VALID_TOKEN)


@pytest.fixture
async def client():
    """AsyncClient with ASGI transport, including lifespan management."""
    app = _build_app()
    async with LifespanManager(app) as manager:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://testserver",
        ) as c:
            yield c


async def test_missing_auth_returns_401(client):
    resp = await client.post(
        "/mcp",
        json=INIT_PAYLOAD,
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "Invalid or missing API key"


async def test_invalid_token_returns_401(client):
    resp = await client.post(
        "/mcp",
        json=INIT_PAYLOAD,
        headers={
            "Authorization": "Bearer wrong",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert resp.status_code == 401


async def test_get_method_returns_405(client):
    resp = await client.get(
        "/mcp",
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert resp.status_code == 405


async def test_unauthenticated_get_returns_405(client):
    """Method check runs before auth check — unauthenticated GET also returns 405."""
    resp = await client.get("/mcp")
    assert resp.status_code == 405


async def test_initialize_returns_server_info(client):
    resp = await client.post("/mcp", json=INIT_PAYLOAD, headers=BASE_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["serverInfo"]["name"] == "libby-mcp"
    assert "protocolVersion" in body["result"]


async def test_tools_list_contains_all_tools(client):
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    tool_names = [t["name"] for t in resp.json()["result"]["tools"]]
    assert "search_titles" in tool_names
    assert "get_availability" in tool_names
    assert "get_deep_link" in tool_names


async def test_tools_list_without_init_works_stateless(client):
    """Stateless mode: tools/list works without a prior initialize."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    assert "tools" in resp.json()["result"]


async def test_get_deep_link_returns_url(client):
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_deep_link",
                "arguments": {"title_id": "12345", "library_slug": "lcpl"},
            },
        },
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result.get("isError") is not True
    content_text = result["content"][0]["text"]
    assert "lcpl.overdrive.com" in content_text


async def test_search_titles_mocked(client):
    mock_result = {"items": [{"id": "1", "title": "Test Book"}], "facets": {}}
    with patch("src.handler.search_titles_impl.handle", return_value=mock_result):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_titles",
                    "arguments": {"library_slug": "lcpl"},
                },
            },
            headers=BASE_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["result"].get("isError") is not True


async def test_get_availability_mocked(client):
    mock_result = {"items": [{"titleId": "1", "copiesOwned": 3, "copiesAvailable": 1}]}
    with patch("src.handler.availability_impl.handle", return_value=mock_result):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "get_availability",
                    "arguments": {"library_slug": "lcpl", "title_ids": ["1"]},
                },
            },
            headers=BASE_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["result"].get("isError") is not True


async def test_tool_error_becomes_iserror(client):
    with patch("src.handler.search_titles_impl.handle", return_value={"error": "timeout"}):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "search_titles",
                    "arguments": {"library_slug": "lcpl"},
                },
            },
            headers=BASE_HEADERS,
        )
    assert resp.status_code == 200
    assert resp.json()["result"]["isError"] is True


def _make_apigw_v2_event(body: dict, token: str = VALID_TOKEN) -> dict:
    """Construct a complete API Gateway HTTP API v2 proxy event.

    Includes all fields Mangum's HTTPGateway reads by direct indexing,
    including requestContext.http.sourceIp.
    """
    return {
        "version": "2.0",
        "routeKey": "POST /mcp",
        "rawPath": "/mcp",
        "rawQueryString": "",
        "headers": {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "host": "testserver",
        },
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "testapi",
            "domainName": "testserver",
            "requestId": "test-request-id",
            "routeKey": "POST /mcp",
            "stage": "$default",
            "timeEpoch": 1234567890000,
            "http": {
                "method": "POST",
                "path": "/mcp",
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "pytest",
            },
        },
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def test_warm_start_two_sequential_invocations(monkeypatch):
    """Two sequential lambda_handler calls must both return 200 (AC-1.2.4, AC-5.1.3)."""
    import os
    monkeypatch.setenv("LIBBY_MCP_API_KEY", VALID_TOKEN)

    event = _make_apigw_v2_event(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    result1 = lambda_handler(event, None)
    assert result1["statusCode"] == 200, f"First invocation failed: {result1}"

    result2 = lambda_handler(event, None)
    assert result2["statusCode"] == 200, f"Second (warm) invocation failed: {result2}"
```

### 6c. Extend `tests/test_auth.py`

Add these tests below existing tests. Check that any existing `env var unset` tests still use `monkeypatch.delenv`:

```python
import pytest
from src.auth import AuthMiddleware


@pytest.fixture
def set_key(monkeypatch):
    """Non-autouse: apply only where needed."""
    monkeypatch.setenv("LIBBY_MCP_API_KEY", "secret")


def _http_scope(auth_header: str | None = None, method: str = "POST") -> dict:
    headers: list = [
        [b"content-type", b"application/json"],
    ]
    if auth_header:
        headers.append([b"authorization", auth_header.encode()])
    return {"type": "http", "headers": headers, "method": method}


async def test_lifespan_passes_through():
    calls: list[str] = []
    async def fake_app(scope, receive, send): calls.append("called")
    middleware = AuthMiddleware(fake_app)
    await middleware({"type": "lifespan"}, None, None)
    assert calls == ["called"]


async def test_get_returns_405(set_key):
    calls: list[str] = []
    async def fake_app(scope, receive, send): calls.append("called")
    middleware = AuthMiddleware(fake_app)
    responses: list[dict] = []
    async def fake_send(msg): responses.append(msg)
    await middleware(_http_scope(method="GET"), None, fake_send)
    assert responses[0]["status"] == 405
    assert not calls


async def test_missing_header_returns_401(set_key):
    calls: list[str] = []
    async def fake_app(scope, receive, send): calls.append("called")
    middleware = AuthMiddleware(fake_app)
    responses: list[dict] = []
    async def fake_send(msg): responses.append(msg)
    await middleware(_http_scope(None), None, fake_send)
    assert responses[0]["status"] == 401
    assert not calls


async def test_invalid_token_returns_401(set_key):
    calls: list[str] = []
    async def fake_app(scope, receive, send): calls.append("called")
    middleware = AuthMiddleware(fake_app)
    responses: list[dict] = []
    async def fake_send(msg): responses.append(msg)
    await middleware(_http_scope("Bearer wrong"), None, fake_send)
    assert responses[0]["status"] == 401
    assert not calls


async def test_valid_token_passes_through(set_key):
    calls: list[str] = []
    async def fake_app(scope, receive, send): calls.append("called")
    middleware = AuthMiddleware(fake_app)
    async def noop_send(msg): pass
    await middleware(_http_scope("Bearer secret"), None, noop_send)
    assert calls == ["called"]
```

---

## Task 7: CDK — Add GET route (separate PR B)

**Implements**: FR-4.3 (AC-4.3.1–4.3.3)

In `infra/lib/libby-mcp-stack.ts`, add a GET route:

```typescript
// Add after the existing POST route:
httpApi.addRoutes({
  path: "/mcp",
  methods: [apigatewayv2.HttpMethod.GET],
  integration: lambdaIntegration,
});
```

Run `cdk synth` to verify, then `cdk deploy`.

**Post-deploy verification:**

```bash
ENDPOINT="https://jmz949xvg6.execute-api.us-east-2.amazonaws.com/mcp"
API_KEY="<from Secrets Manager>"

# Unauthenticated GET → 405 (method check runs before auth)
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$ENDPOINT")
echo "Unauthenticated GET: $STATUS"  # expected: 405

# Authenticated GET → 405
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $API_KEY" "$ENDPOINT")
echo "Authenticated GET: $STATUS"  # expected: 405

# Authenticated POST tools/list → 200
curl -s -X POST "$ENDPOINT" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python3 -m json.tool
# expected: 200, tools array with search_titles, get_availability, get_deep_link
```

Then add Claude.ai connector at Settings → Integrations:
- URL: `https://jmz949xvg6.execute-api.us-east-2.amazonaws.com/mcp`
- Transport: Streamable HTTP
- Request headers: `authorization: Bearer <key>`

---

## ROADMAP Grouping

- **ROADMAP Item A** (Tasks 1-6): Backend migration. ~420 lines changed across handler.py, auth.py, requirements.txt, .dockerignore, tests. One PR.
- **ROADMAP Item B** (Task 7): CDK GET route + deploy + Claude.ai verification. ~15 lines changed. One PR.
