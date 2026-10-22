"""App-level OAuth flow tests — PR-F2 (Tasks F3 + F4).

Drives ``_build_app()`` through an in-process ASGI client (as Mangum / uvicorn
would) end-to-end: DCR → ``GET/POST /authorize`` → ``POST /token`` → refresh →
using the access token on ``/mcp``. Covers the full flow (AC-F2.4–F2.6), the
owner gate over HTTP (AC-F1.6), redirect-URI exact-match (AC-F9.1), per-client
redirect URIs (AC-F4.1–F4.3), the ChatGPT ``resource`` parameter (AC-F4.2 /
F11.3/F11.4), ``client_credentials`` not accepted (AC-F4.5), and consent-page
security headers over HTTP (AC-F2.12).
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from asgi_lifespan import LifespanManager

from src.handler import _build_app

SIGNING_SECRET = "s" * 40
BASE = "https://mcp.example.test"
CANONICAL = f"{BASE}/mcp"
OWNER = "owner-secret"
VERIFIER = "pkce-verifier-" + "a" * 50

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
INIT_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
    monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
    monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
    monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", OWNER)
    monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
    monkeypatch.delenv("LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)


async def _client(app):
    manager = LifespanManager(app)
    await manager.__aenter__()
    transport = httpx.ASGITransport(app=manager.app)
    return manager, httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _register(c, redirect_uri: str, client_name: str = "Test") -> str:
    resp = await c.post(
        "/register", json={"redirect_uris": [redirect_uri], "client_name": client_name}
    )
    assert resp.status_code == 201
    return resp.json()["client_id"]


def _authorize_query(client_id: str, redirect_uri: str, **extra) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": _challenge(),
        "code_challenge_method": "S256",
        "state": "state-123",
    }
    params.update(extra)
    return urlencode(params)


class TestEndToEndFlow:
    async def test_full_authorization_code_and_refresh(self) -> None:
        """AC-F2.3–F2.6: DCR → authorize → token → refresh → access on /mcp."""
        redirect = "https://claude.ai/api/mcp/auth_callback"
        mgr, c = await _client(_build_app())
        try:
            client_id = await _register(c, redirect, "Claude")

            # GET /authorize renders the consent page (200) with security headers.
            page = await c.get(f"/authorize?{_authorize_query(client_id, redirect)}")
            assert page.status_code == 200
            assert page.headers["content-security-policy"] == "frame-ancestors 'none'"
            assert page.headers["cache-control"] == "no-store"

            # POST /authorize with the owner secret → 302 redirect with a code.
            form = {
                "client_id": client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "code_challenge": _challenge(),
                "code_challenge_method": "S256",
                "resource": CANONICAL,
                "state": "state-123",
                "owner_secret": OWNER,
            }
            submit = await c.post("/authorize", data=form)
            assert submit.status_code == 302
            loc = submit.headers["location"]
            assert loc.startswith(redirect)
            q = parse_qs(urlsplit(loc).query)
            assert q["state"] == ["state-123"]
            code = q["code"][0]

            # POST /token: authorization_code exchange.
            tok = await c.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": VERIFIER,
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "resource": CANONICAL,
                },
            )
            assert tok.status_code == 200
            tokens = tok.json()
            access = tokens["access_token"]
            refresh = tokens["refresh_token"]

            # The access token works on /mcp.
            mcp = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": f"Bearer {access}"},
            )
            assert mcp.status_code == 200

            # Refresh rotation → a new access + refresh.
            refreshed = await c.post(
                "/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
            )
            assert refreshed.status_code == 200
            new_tokens = refreshed.json()
            assert new_tokens["refresh_token"] != refresh
            mcp2 = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": f"Bearer {new_tokens['access_token']}"},
            )
            assert mcp2.status_code == 200
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_post_authorize_without_owner_secret_401(self) -> None:
        """AC-F1.6: POST /authorize with no owner secret → 401, no code issued."""
        redirect = "https://claude.ai/api/mcp/auth_callback"
        mgr, c = await _client(_build_app())
        try:
            client_id = await _register(c, redirect)
            resp = await c.post(
                "/authorize",
                data={
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "code_challenge": _challenge(),
                    "code_challenge_method": "S256",
                },
            )
            assert resp.status_code == 401
            assert "location" not in resp.headers
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_authorize_unregistered_redirect_400(self) -> None:
        """AC-F9.1: an unregistered redirect_uri → 400 page, not a redirect."""
        mgr, c = await _client(_build_app())
        try:
            client_id = await _register(c, "https://claude.ai/api/mcp/auth_callback")
            resp = await c.get(
                f"/authorize?{_authorize_query(client_id, 'https://claude.ai/evil')}"
            )
            assert resp.status_code == 400
            assert "location" not in resp.headers
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_authorize_client_name_xss_escaped_over_http(self) -> None:
        """AC-F2.11: an attacker-controlled client_name renders inert on the page."""
        redirect = "https://claude.ai/api/mcp/auth_callback"
        mgr, c = await _client(_build_app())
        try:
            client_id = await _register(c, redirect, "<script>alert(1)</script>")
            page = await c.get(f"/authorize?{_authorize_query(client_id, redirect)}")
            assert page.status_code == 200
            assert "<script>alert(1)</script>" not in page.text
            assert "&lt;script&gt;" in page.text
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


class TestPerClientRedirectURIs:
    """AC-F4.1–F4.3: each target client's redirect URI registers via the same DCR."""

    @pytest.mark.parametrize(
        "redirect_uri,client_name",
        [
            ("https://claude.ai/api/mcp/auth_callback", "Claude.ai"),
            ("https://chatgpt.com/connector_platform_oauth_redirect", "ChatGPT"),
            ("cursor://anysphere.cursor-mcp/oauth/callback", "Cursor"),
        ],
    )
    async def test_client_registers_and_authorizes(self, redirect_uri, client_name) -> None:
        mgr, c = await _client(_build_app())
        try:
            client_id = await _register(c, redirect_uri, client_name)
            page = await c.get(f"/authorize?{_authorize_query(client_id, redirect_uri)}")
            assert page.status_code == 200
            submit = await c.post(
                "/authorize",
                data={
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "code_challenge": _challenge(),
                    "code_challenge_method": "S256",
                    "owner_secret": OWNER,
                },
            )
            assert submit.status_code == 302
            assert submit.headers["location"].startswith(redirect_uri)
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_non_loopback_http_redirect_rejected_at_dcr(self) -> None:
        """AC-F9.2: a non-loopback http:// redirect is rejected at registration."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post("/register", json={"redirect_uris": ["http://evil.test/cb"]})
            assert resp.status_code == 400
            assert resp.json()["error"] == "invalid_redirect_uri"
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


class TestChatGPTResourceParam:
    """AC-F4.2 / F11.3/F11.4: the optional resource parameter is handled per FR-F11."""

    async def test_canonical_resource_accepted(self) -> None:
        redirect = "https://chatgpt.com/connector_platform_oauth_redirect"
        mgr, c = await _client(_build_app())
        try:
            client_id = await _register(c, redirect, "ChatGPT")
            submit = await c.post(
                "/authorize",
                data={
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "code_challenge": _challenge(),
                    "code_challenge_method": "S256",
                    "resource": CANONICAL,
                    "owner_secret": OWNER,
                },
            )
            assert submit.status_code == 302
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_non_canonical_resource_invalid_target(self) -> None:
        redirect = "https://chatgpt.com/connector_platform_oauth_redirect"
        mgr, c = await _client(_build_app())
        try:
            client_id = await _register(c, redirect, "ChatGPT")
            page = await c.get(
                f"/authorize?{_authorize_query(client_id, redirect, resource='https://evil.test/mcp')}"
            )
            assert page.status_code == 400
            assert "invalid_target" in page.text
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


class TestClientCredentialsNotAccepted:
    async def test_client_credentials_rejected(self) -> None:
        """AC-F4.5: client_credentials is neither advertised nor accepted."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post("/token", data={"grant_type": "client_credentials"})
            assert resp.status_code == 400
            assert resp.json()["error"] == "unsupported_grant_type"
            # Also not advertised in AS metadata.
            meta = await c.get("/.well-known/oauth-authorization-server")
            assert "client_credentials" not in meta.json()["grant_types_supported"]
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)
