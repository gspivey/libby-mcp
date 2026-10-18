"""Tests for the single-codebase auto-detect entry point and Docker auth (PR-E).

Covers Group E Tasks E1 (FR-E1 single-codebase runtime auto-detect) and E2
(FR-E2 Docker auth behavior). These tests manipulate AWS_LAMBDA_RUNTIME_API and
LIBBY_MCP_API_KEY directly (rather than relying on the autouse key fixture in
tests/test_handler.py), so they live in their own module.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import httpx
import pytest
from asgi_lifespan import LifespanManager

VALID_TOKEN = "docker-test-key"
INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "curl", "version": "0"},
    },
}
ACCEPT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _reimport_handler():
    """Re-import src.handler so module-level `_IS_LAMBDA` reflects the current env.

    `_IS_LAMBDA` is evaluated at import time, so a test that toggles
    AWS_LAMBDA_RUNTIME_API must re-import the module to observe the new value.
    """
    import src.handler as handler_mod

    return importlib.reload(handler_mod)


async def _asgi_client(app):
    manager = await LifespanManager(app).__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=manager.app),
        base_url="http://testserver",
    )
    return client, manager


# ---------------------------------------------------------------------------
# E1: single-codebase runtime auto-detect (FR-E1)
# ---------------------------------------------------------------------------


def test_is_lambda_true_when_runtime_api_present(monkeypatch):
    """AC-E1.2: AWS_LAMBDA_RUNTIME_API present → Lambda mode detected."""
    monkeypatch.setenv("AWS_LAMBDA_RUNTIME_API", "127.0.0.1:9001")
    handler_mod = _reimport_handler()
    try:
        assert handler_mod._IS_LAMBDA is True
    finally:
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        _reimport_handler()


def test_is_lambda_false_when_runtime_api_absent(monkeypatch):
    """AC-E1.3: AWS_LAMBDA_RUNTIME_API absent → Docker / local mode detected."""
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
    handler_mod = _reimport_handler()
    assert handler_mod._IS_LAMBDA is False


def test_lambda_handler_uses_mangum(monkeypatch):
    """AC-E1.2: lambda_handler wraps the app with Mangum (unchanged from v2)."""
    monkeypatch.setenv("AWS_LAMBDA_RUNTIME_API", "127.0.0.1:9001")
    monkeypatch.setenv("LIBBY_MCP_API_KEY", VALID_TOKEN)
    handler_mod = _reimport_handler()
    try:
        sentinel = {"statusCode": 200}
        with patch.object(handler_mod, "Mangum") as mangum_cls:
            mangum_cls.return_value.return_value = sentinel
            result = handler_mod.lambda_handler({"some": "event"}, None)
        assert result is sentinel
        mangum_cls.assert_called_once()
        # The wrapped object is the _build_app() ASGI app (AuthMiddleware).
        assert mangum_cls.call_args.kwargs.get("lifespan") == "on"
    finally:
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        _reimport_handler()


async def test_build_app_serves_asgi_without_lambda(monkeypatch):
    """AC-E1.3/E1.5: absent AWS_LAMBDA_RUNTIME_API → _build_app() is a usable ASGI app."""
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
    monkeypatch.setenv("LIBBY_MCP_API_KEY", VALID_TOKEN)
    handler_mod = _reimport_handler()
    app = handler_mod._build_app()
    client, manager = await _asgi_client(app)
    try:
        resp = await client.post(
            "/mcp",
            json=INIT_PAYLOAD,
            headers={**ACCEPT_HEADERS, "Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["serverInfo"]["name"] == "libby-mcp"
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


def test_build_app_and_lambda_handler_importable():
    """AC-E1.5: _build_app and lambda_handler remain importable (harness depends on them)."""
    handler_mod = _reimport_handler()
    assert callable(handler_mod._build_app)
    assert callable(handler_mod.lambda_handler)


def test_single_handler_module_no_fork():
    """AC-E1.1: there is exactly one handler module (no docker_/lambda_handler fork)."""
    from pathlib import Path

    src_dir = Path(__file__).resolve().parent.parent / "src"
    handler_modules = sorted(p.name for p in src_dir.glob("*handler*.py"))
    assert handler_modules == ["handler.py"], (
        "REMEDIATION: there must be exactly one handler module (src/handler.py); "
        f"found {handler_modules}"
    )


# ---------------------------------------------------------------------------
# E2: Docker auth behavior (FR-E2)
# ---------------------------------------------------------------------------


@pytest.fixture
async def docker_client_no_key(monkeypatch):
    """Docker mode (no AWS_LAMBDA_RUNTIME_API), no API key set → no-auth."""
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
    monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
    handler_mod = _reimport_handler()
    app = handler_mod._build_app()
    client, manager = await _asgi_client(app)
    try:
        yield client
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.fixture
async def docker_client_with_key(monkeypatch):
    """Docker mode, API key set → static Bearer required."""
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
    monkeypatch.setenv("LIBBY_MCP_API_KEY", VALID_TOKEN)
    handler_mod = _reimport_handler()
    app = handler_mod._build_app()
    client, manager = await _asgi_client(app)
    try:
        yield client
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_docker_no_key_serves_unauthenticated(docker_client_no_key):
    """AC-E2.2: Docker mode with no key serves /mcp unauthenticated."""
    resp = await docker_client_no_key.post("/mcp", json=INIT_PAYLOAD, headers=ACCEPT_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "libby-mcp"


async def test_docker_key_set_requires_bearer(docker_client_with_key):
    """AC-E2.4: Docker mode with a key, no Authorization header → HTTP 401."""
    resp = await docker_client_with_key.post("/mcp", json=INIT_PAYLOAD, headers=ACCEPT_HEADERS)
    assert resp.status_code == 401


async def test_docker_key_set_accepts_valid_bearer(docker_client_with_key):
    """AC-E2.1/E2.3: Docker mode with a key, valid Bearer → HTTP 200 initialize."""
    resp = await docker_client_with_key.post(
        "/mcp",
        json=INIT_PAYLOAD,
        headers={**ACCEPT_HEADERS, "Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "libby-mcp"


async def test_docker_get_returns_405_with_key(docker_client_with_key):
    """AC-E2.3: a bare GET /mcp → HTTP 405 (method guard before auth), key set."""
    resp = await docker_client_with_key.get(
        "/mcp", headers={"Authorization": f"Bearer {VALID_TOKEN}"}
    )
    assert resp.status_code == 405


async def test_docker_get_returns_405_no_key(docker_client_no_key):
    """AC-E2.3: a bare GET /mcp → HTTP 405 regardless of auth state (no key)."""
    resp = await docker_client_no_key.get("/mcp")
    assert resp.status_code == 405


async def test_docker_empty_params_returns_invalid_params(docker_client_with_key):
    """AC-E2.3: `params:{}` fails InitializeRequestParams validation (JSON-RPC -32602)."""
    resp = await docker_client_with_key.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={**ACCEPT_HEADERS, "Authorization": f"Bearer {VALID_TOKEN}"},
    )
    # The transport accepts the HTTP request (auth + method ok) but the JSON-RPC
    # layer rejects the malformed initialize params with code -32602.
    body = resp.json()
    assert body.get("error", {}).get("code") == -32602, body


# ---------------------------------------------------------------------------
# E3 / NFR-8.3: empty-string env vars behave identically to unset (HIGH-2)
# ---------------------------------------------------------------------------

OPTIONAL_ENV_VARS = [
    "LIBBY_MCP_API_KEY",
    "LIBBY_MCP_OAUTH_ENABLED",
    "LIBBY_MCP_OAUTH_SIGNING_KEY",
    "LIBBY_MCP_OAUTH_OWNER_SECRET",
    "LIBBY_MCP_PUBLIC_BASE_URL",
    "LIBBY_MCP_SLUG_TTL_SECONDS",
    "LIBBY_MCP_OAUTH_ACCESS_TTL_SECONDS",
    "LIBBY_MCP_OAUTH_REFRESH_TTL_SECONDS",
    "LIBBY_MCP_OAUTH_CODE_TTL_SECONDS",
]


async def test_empty_string_env_behaves_like_unset(monkeypatch):
    """NFR-8.1/NFR-8.3 (HIGH-2): every optional var set to "" behaves like unset.

    In Docker mode with every optional env var explicitly set to the empty
    string, _build_app() succeeds and /mcp is served unauthenticated — identical
    to the all-unset case. An empty string must never silently enable or
    misconfigure a mode.
    """
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
    for name in OPTIONAL_ENV_VARS:
        monkeypatch.setenv(name, "")
    handler_mod = _reimport_handler()
    app = handler_mod._build_app()
    client, manager = await _asgi_client(app)
    try:
        resp = await client.post("/mcp", json=INIT_PAYLOAD, headers=ACCEPT_HEADERS)
        assert resp.status_code == 200, "empty-string key must read as no-auth"
        assert resp.json()["result"]["serverInfo"]["name"] == "libby-mcp"
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


async def test_unset_env_serves_unauthenticated(monkeypatch):
    """NFR-8.3 companion: the all-unset case is served unauthenticated in Docker."""
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
    for name in OPTIONAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    handler_mod = _reimport_handler()
    app = handler_mod._build_app()
    client, manager = await _asgi_client(app)
    try:
        resp = await client.post("/mcp", json=INIT_PAYLOAD, headers=ACCEPT_HEADERS)
        assert resp.status_code == 200
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)
