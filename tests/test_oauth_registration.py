"""Tests for src/oauth/registration.py — RFC 7591 public-client DCR (FR-F2, HIGH-8)."""

from __future__ import annotations

import hashlib

import pytest

registration = pytest.importorskip(
    "src.oauth.registration", reason="PR-F1 not yet implemented (TDD red phase)"
)
jwt = pytest.importorskip("src.oauth.jwt")

BASE = "https://mcp.example.test"
KEY = hashlib.sha256(b"z" * 40).digest()


class TestRedirectUriValidation:
    """AC-F9.2/F9.3: https any host; http loopback only; custom schemes ok."""

    def test_https_any_host_ok(self) -> None:
        assert registration.is_valid_redirect_uri("https://claude.ai/api/mcp/auth_callback")

    def test_http_loopback_ok(self) -> None:
        assert registration.is_valid_redirect_uri("http://127.0.0.1:8000/cb")
        assert registration.is_valid_redirect_uri("http://localhost/cb")

    def test_http_non_loopback_rejected(self) -> None:
        assert not registration.is_valid_redirect_uri("http://evil.test/cb")

    def test_custom_scheme_ok(self) -> None:
        assert registration.is_valid_redirect_uri("cursor://anysphere.cursor-mcp/oauth/callback")

    def test_empty_rejected(self) -> None:
        assert not registration.is_valid_redirect_uri("")
        assert not registration.is_valid_redirect_uri("   ")


class TestRegister:
    """AC-F2.3 / HIGH-8: 201 public-client body, no client_secret; 400 on bad redirect."""

    def test_success_201_public_client_body(self) -> None:
        status, body = registration.register(
            {"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}, BASE
        )
        assert status == 201
        assert set(body) == {
            "client_id",
            "client_id_issued_at",
            "redirect_uris",
            "grant_types",
            "response_types",
            "token_endpoint_auth_method",
        }
        assert body["token_endpoint_auth_method"] == "none"
        assert body["grant_types"] == ["authorization_code", "refresh_token"]
        assert body["response_types"] == ["code"]
        assert body["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]
        assert isinstance(body["client_id_issued_at"], int)

    def test_no_client_secret(self) -> None:
        _, body = registration.register(
            {"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}, BASE
        )
        assert "client_secret" not in body
        assert "client_secret_expires_at" not in body

    def test_client_id_is_signed_and_embeds_redirects(self) -> None:
        _, body = registration.register(
            {
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "client_name": "Claude",
            },
            BASE,
        )
        # The client_id validates as a typ:"client" token and carries the redirects.
        claims = jwt.verify(
            body["client_id"], expected_typ="client", expected_iss=BASE, key=jwt_key()
        )
        assert claims["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]
        assert claims["client_name"] == "Claude"

    def test_missing_redirect_uris_400(self) -> None:
        status, body = registration.register({}, BASE)
        assert status == 400
        assert body == {"error": "invalid_redirect_uri"}

    def test_empty_redirect_uris_400(self) -> None:
        status, body = registration.register({"redirect_uris": []}, BASE)
        assert status == 400
        assert body["error"] == "invalid_redirect_uri"

    def test_invalid_scheme_redirect_400(self) -> None:
        status, body = registration.register({"redirect_uris": ["http://evil.test/cb"]}, BASE)
        assert status == 400
        assert body["error"] == "invalid_redirect_uri"


def jwt_key() -> bytes:
    """Resolve the signing key the same way registration/mint does (via env)."""
    return jwt.signing_key()


@pytest.fixture(autouse=True)
def _signing_key_env(monkeypatch):
    monkeypatch.setenv(jwt.SIGNING_KEY_ENV, "u" * 40)
