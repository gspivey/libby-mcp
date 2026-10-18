"""Tests for auth module — MCP API key validation.

Covers: valid Bearer token, invalid token, missing header, malformed header,
case-insensitive header lookup (API Gateway lowercases headers).
"""

import os
from unittest.mock import patch

import pytest

auth = pytest.importorskip("src.auth", reason="Implementation not yet written (TDD red phase)")


@pytest.fixture
def _api_key_env():
    """Set a known API key in the environment for testing."""
    with patch.dict(os.environ, {"LIBBY_MCP_API_KEY": "test-secret-key-12345"}):
        yield


class TestValidBearerToken:
    """Test that valid Bearer tokens pass validation."""

    def test_valid_token_passes(self, _api_key_env) -> None:
        """AC-5.1: Valid Bearer token passes validation."""
        headers = {"authorization": "Bearer test-secret-key-12345"}
        assert auth.validate_api_key(headers) is True, (
            "REMEDIATION: validate_api_key must return True for valid Bearer token"
        )

    def test_valid_token_mixed_case_header(self, _api_key_env) -> None:
        """AC-5.1: Header lookup is case-insensitive (API Gateway lowercases)."""
        headers = {"Authorization": "Bearer test-secret-key-12345"}
        assert auth.validate_api_key(headers) is True, (
            "REMEDIATION: validate_api_key must do case-insensitive header lookup"
        )


class TestInvalidToken:
    """Test that invalid tokens fail validation."""

    def test_wrong_token_fails(self, _api_key_env) -> None:
        """AC-5.2: Invalid token is rejected."""
        headers = {"authorization": "Bearer wrong-key"}
        assert auth.validate_api_key(headers) is False, (
            "REMEDIATION: validate_api_key must return False for invalid Bearer token"
        )


class TestMissingHeader:
    """Test that missing Authorization header fails."""

    def test_missing_header_fails(self, _api_key_env) -> None:
        """AC-5.2: Missing Authorization header is rejected."""
        headers = {}
        assert auth.validate_api_key(headers) is False, (
            "REMEDIATION: validate_api_key must return False when Authorization header is missing"
        )


class TestMalformedHeader:
    """Test that malformed headers fail."""

    def test_no_bearer_prefix_fails(self, _api_key_env) -> None:
        """AC-5.2: Header without 'Bearer ' prefix fails."""
        headers = {"authorization": "test-secret-key-12345"}
        assert auth.validate_api_key(headers) is False, (
            "REMEDIATION: validate_api_key must require 'Bearer ' prefix"
        )

    def test_basic_auth_fails(self, _api_key_env) -> None:
        """AC-5.2: Basic auth scheme fails."""
        headers = {"authorization": "Basic dXNlcjpwYXNz"}
        assert auth.validate_api_key(headers) is False, (
            "REMEDIATION: validate_api_key must reject non-Bearer auth schemes"
        )

    def test_empty_bearer_fails(self, _api_key_env) -> None:
        """AC-5.2: Empty Bearer value fails."""
        headers = {"authorization": "Bearer "}
        assert auth.validate_api_key(headers) is False, (
            "REMEDIATION: validate_api_key must reject empty Bearer value"
        )


class TestCaseInsensitiveHeader:
    """Test that header name lookup is case-insensitive."""

    def test_lowercase_authorization(self, _api_key_env) -> None:
        """API Gateway HTTP API lowercases all headers."""
        headers = {"authorization": "Bearer test-secret-key-12345"}
        assert auth.validate_api_key(headers) is True

    def test_uppercase_authorization(self, _api_key_env) -> None:
        """Standard HTTP capitalization also works."""
        headers = {"AUTHORIZATION": "Bearer test-secret-key-12345"}
        assert auth.validate_api_key(headers) is True, (
            "REMEDIATION: validate_api_key must normalize header keys to lowercase"
        )


from src.auth import AuthMiddleware  # noqa: E402


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

    async def fake_app(scope, receive, send):
        calls.append("called")

    middleware = AuthMiddleware(fake_app)
    await middleware({"type": "lifespan"}, None, None)
    assert calls == ["called"]


async def test_get_returns_405(set_key):
    calls: list[str] = []

    async def fake_app(scope, receive, send):
        calls.append("called")

    middleware = AuthMiddleware(fake_app)
    responses: list[dict] = []

    async def fake_send(msg):
        responses.append(msg)

    await middleware(_http_scope(method="GET"), None, fake_send)
    assert responses[0]["status"] == 405
    assert not calls


async def test_middleware_missing_header_returns_401(set_key):
    calls: list[str] = []

    async def fake_app(scope, receive, send):
        calls.append("called")

    middleware = AuthMiddleware(fake_app)
    responses: list[dict] = []

    async def fake_send(msg):
        responses.append(msg)

    await middleware(_http_scope(None), None, fake_send)
    assert responses[0]["status"] == 401
    assert not calls


async def test_middleware_invalid_token_returns_401(set_key):
    calls: list[str] = []

    async def fake_app(scope, receive, send):
        calls.append("called")

    middleware = AuthMiddleware(fake_app)
    responses: list[dict] = []

    async def fake_send(msg):
        responses.append(msg)

    await middleware(_http_scope("Bearer wrong"), None, fake_send)
    assert responses[0]["status"] == 401
    assert not calls


async def test_middleware_valid_token_passes_through(set_key):
    calls: list[str] = []

    async def fake_app(scope, receive, send):
        calls.append("called")

    middleware = AuthMiddleware(fake_app)

    async def noop_send(msg):
        pass

    await middleware(_http_scope("Bearer secret"), None, noop_send)
    assert calls == ["called"]
