"""App-level tests for Group F PR-F1 — three auth modes + OAuth endpoints.

Drives ``_build_app()`` through an in-process ASGI client (as Mangum / uvicorn
would), covering FR-F1 (AC-F1.1–F1.5, AC-F1.1-lambda), FR-F3 (AC-F3.1), the
metadata + ``/register`` endpoints (AC-F2.1–F2.3), the startup refusals
(AC-F6.2 / F1.8 / F1.10), ``oauth_not_configured`` (AC-F10.1), and the
empty-string ≡ unset env semantics (HIGH-2 / NFR-8.3).
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
from asgi_lifespan import LifespanManager

from src.handler import _build_app

SIGNING_SECRET = "s" * 40
BASE = "https://mcp.example.test"

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


async def _client(app):
    manager = LifespanManager(app)
    await manager.__aenter__()
    transport = httpx.ASGITransport(app=manager.app)
    return manager, httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _mint_access(aud: str, iss: str = BASE) -> str:
    from src.oauth import jwt

    key = hashlib.sha256(SIGNING_SECRET.encode()).digest()
    return jwt.sign({"aud": aud}, typ="access", iss=iss, ttl_seconds=60, key=key)


# ---------------------------------------------------------------------------
# FR-F1: three auth modes
# ---------------------------------------------------------------------------


class TestNoAuthMode:
    async def test_docker_no_auth_serves_mcp(self, monkeypatch) -> None:
        """AC-F1.1: Docker (no key, OAuth off) serves /mcp unauthenticated."""
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.delenv("LIBBY_MCP_OAUTH_ENABLED", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post("/mcp", json=INIT_PAYLOAD, headers=INIT_HEADERS)
            assert resp.status_code == 200
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_lambda_no_auth_fails_closed(self, monkeypatch) -> None:
        """AC-F1.1-lambda / HIGH-13: Lambda no-auth → 500 authentication_not_configured."""
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.delenv("LIBBY_MCP_OAUTH_ENABLED", raising=False)
        monkeypatch.setenv("AWS_LAMBDA_RUNTIME_API", "127.0.0.1:9001")
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post("/mcp", json=INIT_PAYLOAD, headers=INIT_HEADERS)
            assert resp.status_code == 500
            assert resp.json()["error"] == "authentication_not_configured"
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


class TestStaticBearerMode:
    async def test_valid_key_ok_invalid_401(self, monkeypatch) -> None:
        """AC-F1.2: static Bearer accept/reject; no resource_metadata challenge."""
        monkeypatch.setenv("LIBBY_MCP_API_KEY", "secret")
        monkeypatch.delenv("LIBBY_MCP_OAUTH_ENABLED", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        mgr, c = await _client(_build_app())
        try:
            ok = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": "Bearer secret"},
            )
            assert ok.status_code == 200
            bad = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": "Bearer wrong"},
            )
            assert bad.status_code == 401
            # Static-only mode: bare Bearer challenge, no resource_metadata.
            assert "resource_metadata" not in bad.headers.get("www-authenticate", "")
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


class TestOAuthMode:
    @pytest.fixture(autouse=True)
    def _oauth_env(self, monkeypatch):
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)

    async def test_valid_jwt_accepted(self) -> None:
        """AC-F1.3: a valid JWT access token (canonical aud) is accepted."""
        token = _mint_access(aud=f"{BASE}/mcp")
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_missing_token_401_with_resource_metadata(self) -> None:
        """AC-F3.1: unauthenticated /mcp in OAuth mode → 401 + resource_metadata challenge."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post("/mcp", json=INIT_PAYLOAD, headers=INIT_HEADERS)
            assert resp.status_code == 401
            www = resp.headers.get("www-authenticate", "")
            assert "resource_metadata=" in www
            assert f"{BASE}/.well-known/oauth-protected-resource" in www
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_wrong_aud_rejected(self) -> None:
        """AC-F11.6: a token whose aud is not the canonical resource → 401."""
        token = _mint_access(aud="https://other.test/mcp")
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 401
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_refresh_token_rejected_on_mcp(self) -> None:
        """HIGH-5: a refresh token presented as a Bearer on /mcp is rejected (401)."""
        from src.oauth import jwt

        key = hashlib.sha256(SIGNING_SECRET.encode()).digest()
        refresh = jwt.sign({"aud": f"{BASE}/mcp"}, typ="refresh", iss=BASE, ttl_seconds=60, key=key)
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": f"Bearer {refresh}"},
            )
            assert resp.status_code == 401
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


class TestStaticAndOAuthCoexist:
    @pytest.fixture(autouse=True)
    def _both_env(self, monkeypatch):
        monkeypatch.setenv("LIBBY_MCP_API_KEY", "static-key")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)

    async def test_static_key_accepted(self) -> None:
        """AC-F1.4: with both configured, the static key is accepted."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": "Bearer static-key"},
            )
            assert resp.status_code == 200
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_jwt_accepted(self) -> None:
        """AC-F1.4: with both configured, a valid JWT is also accepted."""
        token = _mint_access(aud=f"{BASE}/mcp")
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={**INIT_HEADERS, "Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# FR-F2: OAuth metadata + /register endpoints (public, bypass the /mcp gate)
# ---------------------------------------------------------------------------


class TestOAuthEndpoints:
    @pytest.fixture(autouse=True)
    def _oauth_env(self, monkeypatch):
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)

    async def test_prm_metadata_public(self) -> None:
        """AC-F2.1: PRM document served unauthenticated."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.get("/.well-known/oauth-protected-resource")
            assert resp.status_code == 200
            doc = resp.json()
            assert doc["resource"] == f"{BASE}/mcp"
            assert doc["authorization_servers"] == [BASE]
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_as_metadata_public(self) -> None:
        """AC-F2.2: AS metadata served unauthenticated, S256 only."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.get("/.well-known/oauth-authorization-server")
            assert resp.status_code == 200
            doc = resp.json()
            assert doc["code_challenge_methods_supported"] == ["S256"]
            assert "client_credentials" not in doc["grant_types_supported"]
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_register_public_client_201(self) -> None:
        """AC-F2.3: DCR returns 201 with a public-client body, no client_secret."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post(
                "/register",
                json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
            )
            assert resp.status_code == 201
            body = resp.json()
            assert body["token_endpoint_auth_method"] == "none"
            assert "client_secret" not in body
            assert body["client_id"]
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_register_bad_redirect_400(self) -> None:
        """AC-F2.3: a bad redirect_uri → 400 invalid_redirect_uri."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post("/register", json={"redirect_uris": ["http://evil.test/cb"]})
            assert resp.status_code == 400
            assert resp.json()["error"] == "invalid_redirect_uri"
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# FR-F10: oauth_not_configured (non-loopback host, base URL unset)
# ---------------------------------------------------------------------------


class TestOAuthNotConfigured:
    async def test_non_loopback_without_base_url_500(self, monkeypatch) -> None:
        """AC-F10.1: OAuth route, non-loopback Host, no base URL → 500 oauth_not_configured."""
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.delenv("LIBBY_MCP_PUBLIC_BASE_URL", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        mgr, c = await _client(_build_app())
        try:
            resp = await c.get(
                "/.well-known/oauth-protected-resource",
                headers={"host": "mcp.public.test"},
            )
            assert resp.status_code == 500
            assert resp.json()["error"] == "oauth_not_configured"
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    async def test_loopback_host_without_base_url_ok(self, monkeypatch) -> None:
        """AC-F10.2: loopback Host with no base URL derives the base (local dev)."""
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.delenv("LIBBY_MCP_PUBLIC_BASE_URL", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        mgr, c = await _client(_build_app())
        try:
            resp = await c.get(
                "/.well-known/oauth-protected-resource",
                headers={"host": "127.0.0.1:8000"},
            )
            assert resp.status_code == 200
            assert resp.json()["resource"] == "http://127.0.0.1:8000/mcp"
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Startup refusals (AC-F6.2 / F1.8 / F1.10)
# ---------------------------------------------------------------------------


class TestStartupRefusals:
    def test_missing_signing_key_refuses(self, monkeypatch) -> None:
        """AC-F6.2: OAuth enabled, no signing key → _build_app refuses to start."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.delenv("LIBBY_MCP_OAUTH_SIGNING_KEY", raising=False)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
        from src.oauth.config import OAuthStartupError

        with pytest.raises(OAuthStartupError):
            _build_app()

    def test_missing_owner_secret_refuses(self, monkeypatch) -> None:
        """AC-F1.8 / CRITICAL-1: OAuth enabled, no owner secret → refuse to start."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.delenv("LIBBY_MCP_OAUTH_OWNER_SECRET", raising=False)
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
        from src.oauth.config import OAuthStartupError

        with pytest.raises(OAuthStartupError):
            _build_app()

    def test_api_key_satisfies_owner_secret(self, monkeypatch) -> None:
        """AC-F1.6: the static API key serves as the effective owner secret."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.delenv("LIBBY_MCP_OAUTH_OWNER_SECRET", raising=False)
        monkeypatch.setenv("LIBBY_MCP_API_KEY", "also-the-owner")
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
        # Should not raise.
        _build_app()

    def test_auto_approve_without_base_url_refuses(self, monkeypatch) -> None:
        """AC-F1.10 / HIGH-6: auto-approve flag + no base URL → refuse to start."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE", "true")
        monkeypatch.delenv("LIBBY_MCP_PUBLIC_BASE_URL", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        from src.oauth.config import OAuthStartupError

        with pytest.raises(OAuthStartupError):
            _build_app()

    def test_auto_approve_non_loopback_base_refuses(self, monkeypatch) -> None:
        """AC-F1.10 / HIGH-6: auto-approve flag + non-loopback base URL → refuse."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE", "true")
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
        from src.oauth.config import OAuthStartupError

        with pytest.raises(OAuthStartupError):
            _build_app()

    def test_auto_approve_loopback_base_ok(self, monkeypatch) -> None:
        """AC-F1.9: auto-approve flag + loopback base URL + non-Lambda → starts."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE", "true")
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", "http://127.0.0.1:8000")
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        _build_app()


# ---------------------------------------------------------------------------
# Env semantics (HIGH-2 / NFR-8.3): empty string == unset
# ---------------------------------------------------------------------------


class TestEnvSemantics:
    async def test_all_optional_empty_equals_unset(self, monkeypatch) -> None:
        """NFR-8.3: every optional var set to '' behaves like the all-unset case."""
        for name in (
            "LIBBY_MCP_API_KEY",
            "LIBBY_MCP_OAUTH_ENABLED",
            "LIBBY_MCP_OAUTH_SIGNING_KEY",
            "LIBBY_MCP_OAUTH_OWNER_SECRET",
            "LIBBY_MCP_PUBLIC_BASE_URL",
            "LIBBY_MCP_SLUG_TTL_SECONDS",
            "LIBBY_MCP_OAUTH_ACCESS_TTL_SECONDS",
            "LIBBY_MCP_OAUTH_REFRESH_TTL_SECONDS",
            "LIBBY_MCP_OAUTH_CODE_TTL_SECONDS",
        ):
            monkeypatch.setenv(name, "")
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        # OAuth reads as disabled (so no startup refusal) and /mcp is no-auth.
        mgr, c = await _client(_build_app())
        try:
            resp = await c.post("/mcp", json=INIT_PAYLOAD, headers=INIT_HEADERS)
            assert resp.status_code == 200
            # OAuth disabled → metadata route is not mounted (delegates to MCP → 404-ish).
            meta = await c.get("/.well-known/oauth-protected-resource")
            assert meta.status_code != 200
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)

    def test_empty_oauth_enabled_does_not_require_signing_key(self, monkeypatch) -> None:
        """HIGH-2: LIBBY_MCP_OAUTH_ENABLED='' reads as disabled (no startup refusal)."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "")
        monkeypatch.delenv("LIBBY_MCP_OAUTH_SIGNING_KEY", raising=False)
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)
        _build_app()  # must not raise


class TestMethodGuardInOAuthMode:
    @pytest.fixture(autouse=True)
    def _oauth_env(self, monkeypatch):
        monkeypatch.delenv("LIBBY_MCP_API_KEY", raising=False)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_ENABLED", "true")
        monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", SIGNING_SECRET)
        monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
        monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)

    async def test_get_mcp_405_in_oauth_mode(self) -> None:
        """HIGH-1: GET /mcp → 405 even in OAuth mode (method guard precedes auth)."""
        mgr, c = await _client(_build_app())
        try:
            resp = await c.get("/mcp")
            assert resp.status_code == 405
        finally:
            await c.aclose()
            await mgr.__aexit__(None, None, None)
