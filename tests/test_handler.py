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
            base_url="http://localhost",
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
            "host": "localhost",
        },
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "testapi",
            "domainName": "localhost",
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
