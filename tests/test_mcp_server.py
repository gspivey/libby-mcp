"""Tests for MCP protocol layer — JSON-RPC 2.0 handling.

Covers: initialize handshake, ping, tools/list, tools/call dispatch,
result formatting, error envelopes, notifications, unknown methods,
malformed requests.
"""

from unittest.mock import MagicMock

import pytest

mcp_server = pytest.importorskip(
    "src.mcp_server", reason="Implementation not yet written (TDD red phase)"
)


@pytest.fixture
def server():
    """Create an MCPServer instance with a mock Thunder client."""
    mock_client = MagicMock()
    return mcp_server.MCPServer(thunder_client=mock_client)


class TestInitialize:
    """Test MCP initialize handshake."""

    def test_initialize_returns_correct_result(self, server) -> None:
        """AC-6.8: initialize returns correct InitializeResult."""
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        response = server.handle_request(request)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        result = response["result"]
        assert result["protocolVersion"] == "2025-06-18", (
            "REMEDIATION: MCPServer initialize must return protocolVersion '2025-06-18'"
        )
        assert result["capabilities"] == {"tools": {}}, (
            "REMEDIATION: MCPServer initialize must return capabilities: {tools: {}}"
        )
        assert result["serverInfo"] == {"name": "libby-mcp", "version": "1.0.0"}, (
            "REMEDIATION: MCPServer initialize must return correct serverInfo"
        )


class TestPing:
    """Test MCP ping handling."""

    def test_ping_returns_empty_result(self, server) -> None:
        """AC-6.9: ping returns empty result {}, not -32601 error."""
        request = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
        response = server.handle_request(request)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 2
        assert response["result"] == {}, (
            "REMEDIATION: MCPServer.ping must return empty result {}, not method-not-found error"
        )
        assert "error" not in response


class TestToolsList:
    """Test tools/list method."""

    def test_tools_list_returns_three_tools(self, server) -> None:
        """AC-6.2: tools/list returns all three tool definitions."""
        request = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        response = server.handle_request(request)
        assert "result" in response
        tools = response["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "search_titles" in tool_names, "REMEDIATION: tools/list must include search_titles"
        assert "get_availability" in tool_names, (
            "REMEDIATION: tools/list must include get_availability"
        )
        assert "get_deep_link" in tool_names, "REMEDIATION: tools/list must include get_deep_link"

    def test_tools_have_input_schema(self, server) -> None:
        """AC-6.2: Each tool has an inputSchema."""
        request = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        response = server.handle_request(request)
        tools = response["result"]["tools"]
        for tool in tools:
            assert "inputSchema" in tool, (
                f"REMEDIATION: tool '{tool['name']}' must have inputSchema"
            )


class TestToolsCall:
    """Test tools/call dispatch and result formatting."""

    def test_tools_call_dispatches_correctly(self, server) -> None:
        """AC-6.3: tools/call dispatches to correct handler."""
        # Mock the tool handlers to verify dispatch
        request = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "get_deep_link",
                "arguments": {"title_id": "12345", "library_slug": "lcpl"},
            },
        }
        response = server.handle_request(request)
        assert "result" in response, (
            "REMEDIATION: tools/call must return a result for valid tool invocations"
        )

    def test_tool_result_format(self, server) -> None:
        """AC-6.5: Tool results use text content blocks."""
        request = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "get_deep_link",
                "arguments": {"title_id": "12345", "library_slug": "lcpl"},
            },
        }
        response = server.handle_request(request)
        result = response["result"]
        assert "content" in result, "REMEDIATION: Tool results must have 'content' key"
        assert len(result["content"]) >= 1
        assert result["content"][0]["type"] == "text", (
            "REMEDIATION: Tool result content must use type 'text'"
        )
        assert "text" in result["content"][0], (
            "REMEDIATION: Tool result content block must have 'text' field"
        )

    def test_tool_error_includes_is_error_flag(self, server) -> None:
        """Tool-execution failures include isError: true in result."""
        request = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "get_deep_link",
                "arguments": {},  # Missing required params
            },
        }
        response = server.handle_request(request)
        result = response["result"]
        assert result.get("isError") is True, (
            "REMEDIATION: Tool errors must include isError: true in result"
        )

    def test_invalid_tool_params_return_is_error(self, server) -> None:
        """Invalid tool params return result with isError: true, not JSON-RPC error."""
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "search_titles",
                "arguments": {},  # Missing required library_slug
            },
        }
        response = server.handle_request(request)
        # Should be a result with isError, not a JSON-RPC error
        assert "result" in response, (
            "REMEDIATION: Invalid tool params must use result with isError, not JSON-RPC error"
        )
        assert response["result"].get("isError") is True


class TestNotifications:
    """Test notification handling (no id in request)."""

    def test_notifications_initialized_no_response(self, server) -> None:
        """notifications/initialized is acknowledged without error."""
        request = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        # Notifications have no id — should return None (no response)
        response = server.handle_request(request)
        assert response is None, (
            "REMEDIATION: Notifications (no 'id') must return None (HTTP 202, no body)"
        )


class TestUnknownMethod:
    """Test unknown method handling."""

    def test_unknown_method_returns_error(self, server) -> None:
        """Unknown methods return JSON-RPC error -32601 (method not found)."""
        request = {"jsonrpc": "2.0", "id": 8, "method": "nonexistent/method", "params": {}}
        response = server.handle_request(request)
        assert "error" in response, "REMEDIATION: Unknown methods must return JSON-RPC error object"
        assert response["error"]["code"] == -32601, (
            "REMEDIATION: Unknown method error code must be -32601"
        )


class TestMalformedRequest:
    """Test malformed request handling."""

    def test_missing_jsonrpc_field(self, server) -> None:
        """Malformed requests return JSON-RPC error -32600."""
        request = {"id": 9, "method": "initialize"}  # Missing "jsonrpc"
        response = server.handle_request(request)
        assert "error" in response, "REMEDIATION: Malformed requests must return JSON-RPC error"
        assert response["error"]["code"] == -32600, (
            "REMEDIATION: Malformed request error code must be -32600"
        )

    def test_missing_method_field(self, server) -> None:
        """Missing method returns JSON-RPC error -32600."""
        request = {"jsonrpc": "2.0", "id": 10}  # Missing "method"
        response = server.handle_request(request)
        assert "error" in response
        assert response["error"]["code"] == -32600
