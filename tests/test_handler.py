"""Tests for src/handler.py -- FastMCP ASGI handler and Lambda warm-start."""

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
    """Method check runs before auth check -- unauthenticated GET also returns 405."""
    resp = await client.get("/mcp")
    assert resp.status_code == 405


async def test_initialize_returns_server_info(client):
    resp = await client.post("/mcp", json=INIT_PAYLOAD, headers=BASE_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["serverInfo"]["name"] == "libby-mcp"
    assert "protocolVersion" in body["result"]


async def test_tools_list_contains_remaining_tools(client):
    """AC-B3.1/AC-B3.5: after v2 PR-B only the two remaining tools are registered."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    tool_names = [t["name"] for t in resp.json()["result"]["tools"]]
    assert "search_titles" in tool_names
    assert "get_availability" in tool_names


async def test_get_deep_link_not_registered(client):
    """AC-B3.1 / NFR-4.3: get_deep_link is retired and absent from tools/list."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    tool_names = [t["name"] for t in resp.json()["result"]["tools"]]
    assert (
        "get_deep_link" not in tool_names
    ), "REMEDIATION: get_deep_link must be removed from the registered tool set in v2"


async def test_tools_list_without_init_works_stateless(client):
    """Stateless mode: tools/list works without a prior initialize."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    assert "tools" in resp.json()["result"]


async def test_search_titles_multi_library_e2e(client):
    """AC-B1.6 / CRITICAL-1: multi-library fan-out runs through the real FastMCP app.

    This drives the registered async search_titles tool under a running event
    loop (as Mangum does), proving the async fan-out does not raise
    `asyncio.run() cannot be called from a running event loop`.
    """

    async def fake_search_async(slug, params, async_client):
        return {
            "items": [{"id": f"{slug}-1", "title": f"Book from {slug}"}],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }

    with patch(
        "src.thunder_client.ThunderClient.search_async",
        side_effect=fake_search_async,
    ):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_titles",
                    "arguments": {"library_slugs": ["lcpl", "fairfax"]},
                },
            },
            headers=BASE_HEADERS,
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result.get("isError") is not True, result
    payload = json.loads(result["content"][0]["text"])
    slugs = [block["library_slug"] for block in payload["libraries"]]
    assert slugs == ["lcpl", "fairfax"]


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
    mock_result = {
        "library_slug": "lcpl",
        "titles": [{"title_id": "1", "copies_owned": 3, "copies_available": 1}],
    }

    async def fake_handle(params, client):
        return mock_result

    with patch("src.handler.availability_impl.handle", side_effect=fake_handle):
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
    monkeypatch.setenv("LIBBY_MCP_API_KEY", VALID_TOKEN)

    event = _make_apigw_v2_event({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})

    result1 = lambda_handler(event, None)
    assert result1["statusCode"] == 200, f"First invocation failed: {result1}"

    result2 = lambda_handler(event, None)
    assert result2["statusCode"] == 200, f"Second (warm) invocation failed: {result2}"


async def test_search_titles_schema_uses_verified_slugs(client):
    """AC-A1.1/A1.3: search_titles schema references 'fairfax', never 'fairfaxcounty'."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    blob = json.dumps(tools["search_titles"])
    assert (
        "fairfaxcounty" not in blob
    ), "REMEDIATION: search_titles schema/description must use 'fairfax', not 'fairfaxcounty'"
    assert "fairfax" in blob


async def test_no_fairfaxcounty_in_any_tool_schema(client):
    """AC-A1.3: no registered tool description or schema references 'fairfaxcounty'."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    blob = json.dumps(resp.json()["result"]["tools"])
    assert (
        "fairfaxcounty" not in blob
    ), "REMEDIATION: no shipped tool schema may reference 'fairfaxcounty'"


def test_no_fairfaxcounty_literal_in_src():
    """AC-A1.3: no 'fairfaxcounty' literal remains anywhere under src/."""
    from pathlib import Path

    src_dir = Path(__file__).resolve().parent.parent / "src"
    offenders = [
        str(p) for p in src_dir.rglob("*.py") if "fairfaxcounty" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"REMEDIATION: remove 'fairfaxcounty' literal from: {offenders}"


async def test_search_titles_schema_has_include_facets(client):
    """AC-A4.1: search_titles exposes an include_facets boolean parameter."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    props = tools["search_titles"]["inputSchema"]["properties"]
    assert (
        "include_facets" in props
    ), "REMEDIATION: search_titles must expose an include_facets parameter"


async def test_search_titles_schema_has_library_slugs(client):
    """AC-B1.1: search_titles exposes a library_slugs array parameter."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    props = tools["search_titles"]["inputSchema"]["properties"]
    assert (
        "library_slugs" in props
    ), "REMEDIATION: search_titles must expose a library_slugs parameter"
    assert "library_slug" in props


async def _call_search(client, arguments, call_id):
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "search_titles", "arguments": arguments},
        },
        headers=BASE_HEADERS,
    )


async def test_search_titles_both_slug_forms_errors(client):
    """AC-B1.2: providing both library_slug and library_slugs is a validation error."""
    resp = await _call_search(client, {"library_slug": "lcpl", "library_slugs": ["lcpl"]}, 11)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "not both" in result["content"][0]["text"]


async def test_search_titles_neither_slug_form_errors(client):
    """AC-B1.2: providing neither library_slug nor library_slugs is a validation error."""
    resp = await _call_search(client, {}, 12)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "is required" in result["content"][0]["text"]


async def test_search_titles_empty_slugs_errors(client):
    """AC-B1.9: an empty library_slugs list is a validation error."""
    resp = await _call_search(client, {"library_slugs": []}, 13)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "must not be empty" in result["content"][0]["text"]


async def test_get_availability_integer_id_e2e(client):
    """HIGH-A / AC-D1.8: an integer title_id is accepted by the tool schema and
    coerced to a string before the Thunder call, driven through the FastMCP app.

    This proves the coercion happens at the FastMCP tool boundary (the wrapper
    declares `title_ids: list[str | int]`), not only inside handle().
    """
    captured: dict = {}

    async def fake_avail(slug, ids, async_client):
        captured["ids"] = list(ids)
        return {"items": [{"id": "9876543", "availableCopies": 2, "ownedCopies": 3}]}

    async def fake_lib(slug, async_client):
        return {"id": 1, "preferredKey": slug}

    with (
        patch("src.thunder_client.ThunderClient.get_availability_async", side_effect=fake_avail),
        patch("src.thunder_client.ThunderClient.get_library_async", side_effect=fake_lib),
    ):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "get_availability",
                    "arguments": {"library_slug": "lcpl", "title_ids": [9876543]},
                },
            },
            headers=BASE_HEADERS,
        )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result.get("isError") is not True, result
    payload = json.loads(result["content"][0]["text"])
    # The integer was coerced to the string "9876543" before the Thunder call.
    assert captured["ids"] == ["9876543"]
    assert payload["titles"][0]["title_id"] == "9876543"


async def test_get_availability_schema_accepts_int_ids(client):
    """HIGH-A: the get_availability tool schema admits integer title IDs."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    schema = tools["get_availability"]["inputSchema"]["properties"]["title_ids"]
    blob = json.dumps(schema)
    # list[str | int] serializes to an array whose items allow both types.
    assert "integer" in blob and "string" in blob


async def test_get_availability_normalizes_slug(client):
    """AC-D2.1: the get_availability wrapper normalizes the slug before dispatch."""
    captured: dict = {}

    async def fake_handle(params, client):
        captured["slug"] = params["library_slug"]
        return {"library_slug": params["library_slug"], "titles": []}

    with patch("src.handler.availability_impl.handle", side_effect=fake_handle):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {
                    "name": "get_availability",
                    "arguments": {"library_slug": " LCPL ", "title_ids": ["1"]},
                },
            },
            headers=BASE_HEADERS,
        )
    assert resp.status_code == 200
    assert captured["slug"] == "lcpl"


async def test_search_titles_normalizes_single_slug(client):
    """AC-D2.1/D2.2: the search_titles wrapper normalizes a single slug."""
    captured: dict = {}

    def fake_handle(params, client):
        captured["slug"] = params["library_slug"]
        return {"library_slug": params["library_slug"], "titles": []}

    with patch("src.handler.search_titles_impl.handle", side_effect=fake_handle):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 23,
                "method": "tools/call",
                "params": {
                    "name": "search_titles",
                    "arguments": {"library_slug": "LCPL"},
                },
            },
            headers=BASE_HEADERS,
        )
    assert resp.status_code == 200
    assert captured["slug"] == "lcpl"


async def test_search_titles_normalizes_and_dedupes_slugs(client):
    """AC-D2.4: ['LCPL','lcpl'] normalizes+dedupes to a single lcpl fan-out."""
    captured: dict = {}

    async def fake_handle_multi(params, client):
        captured["slugs"] = list(params["library_slugs"])
        return {"libraries": [{"library_slug": s, "titles": []} for s in params["library_slugs"]]}

    with patch("src.handler.search_titles_impl.handle_multi", side_effect=fake_handle_multi):
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 24,
                "method": "tools/call",
                "params": {
                    "name": "search_titles",
                    "arguments": {"library_slugs": ["LCPL", "lcpl"]},
                },
            },
            headers=BASE_HEADERS,
        )
    assert resp.status_code == 200
    assert captured["slugs"] == ["lcpl"]


async def test_search_titles_schema_has_exact(client):
    """AC-D6.1: search_titles exposes an `exact` boolean parameter."""
    resp = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 25, "method": "tools/list", "params": {}},
        headers=BASE_HEADERS,
    )
    assert resp.status_code == 200
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    props = tools["search_titles"]["inputSchema"]["properties"]
    assert "exact" in props, "REMEDIATION: search_titles must expose an `exact` parameter"


async def test_search_titles_exact_without_query_errors(client):
    """AC-D6.4: exact=True with no query is a validation error (exact requires query)."""
    resp = await _call_search(client, {"library_slug": "lcpl", "exact": True}, 26)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "exact requires query" in result["content"][0]["text"]


async def test_search_titles_exact_with_query_dispatches(client):
    """AC-D6.1: exact=True + query passes validation and reaches the impl layer."""
    captured: dict = {}

    def fake_handle(params, client):
        captured["exact"] = params.get("exact")
        captured["query"] = params.get("query")
        return {"library_slug": params["library_slug"], "titles": []}

    with patch("src.handler.search_titles_impl.handle", side_effect=fake_handle):
        resp = await _call_search(
            client, {"library_slug": "lcpl", "query": "The Mom Test", "exact": True}, 27
        )
    assert resp.status_code == 200
    assert captured["exact"] is True
    assert captured["query"] == "The Mom Test"
