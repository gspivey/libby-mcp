"""Tests for Lambda handler — API Gateway event parsing, auth, dispatch.

Covers: auth rejection, successful dispatch, unhandled errors.
"""

import json
import os
from unittest.mock import patch

import pytest

handler_mod = pytest.importorskip(
    "src.handler", reason="Implementation not yet written (TDD red phase)"
)


@pytest.fixture
def _api_key_env():
    """Set a known API key in the environment."""
    with patch.dict(os.environ, {"LIBBY_MCP_API_KEY": "test-key-123"}):
        yield


def _make_event(body: dict, headers: dict | None = None) -> dict:
    """Construct a minimal API Gateway HTTP API v2 event."""
    if headers is None:
        headers = {"authorization": "Bearer test-key-123"}
    return {
        "requestContext": {"http": {"method": "POST", "path": "/mcp"}},
        "headers": headers,
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


class TestAuthRejection:
    """Test that invalid auth returns 401."""

    def test_missing_auth_returns_401(self, _api_key_env) -> None:
        """AC-5.2: Missing API key rejected with 401."""
        event = _make_event(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={},
        )
        result = handler_mod.lambda_handler(event, None)
        assert (
            result["statusCode"] == 401
        ), "REMEDIATION: lambda_handler must return 401 for missing API key"

    def test_invalid_auth_returns_401(self, _api_key_env) -> None:
        """AC-5.2: Invalid API key rejected with 401."""
        event = _make_event(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"authorization": "Bearer wrong-key"},
        )
        result = handler_mod.lambda_handler(event, None)
        assert result["statusCode"] == 401


class TestSuccessfulDispatch:
    """Test successful MCP request handling."""

    def test_valid_request_returns_200(self, _api_key_env) -> None:
        """Valid key + valid MCP request returns 200."""
        event = _make_event({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        result = handler_mod.lambda_handler(event, None)
        assert (
            result["statusCode"] == 200
        ), "REMEDIATION: lambda_handler must return 200 for valid JSON-RPC requests"
        body = json.loads(result["body"])
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        assert "result" in body

    def test_notification_returns_202(self, _api_key_env) -> None:
        """Notification (no id) returns 202 Accepted."""
        event = _make_event({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        result = handler_mod.lambda_handler(event, None)
        assert (
            result["statusCode"] == 202
        ), "REMEDIATION: Notifications (no 'id') must return HTTP 202 Accepted"


class TestUnhandledErrors:
    """Test that unhandled errors return 500."""

    def test_exception_returns_500(self, _api_key_env) -> None:
        """Unhandled exception in tool returns 500 with generic message."""
        event = _make_event({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        with patch("src.mcp_server.MCPServer.handle_request") as mock_handle:
            mock_handle.side_effect = RuntimeError("unexpected crash")
            result = handler_mod.lambda_handler(event, None)
            assert (
                result["statusCode"] == 500
            ), "REMEDIATION: lambda_handler must return 500 on unhandled exceptions"
            body = json.loads(result["body"])
            # Must not leak internal details
            assert "unexpected crash" not in body.get(
                "message", body.get("error", "")
            ), "REMEDIATION: 500 response must not leak internal error details"
