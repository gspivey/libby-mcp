"""MCP 2025-06-18 Streamable HTTP protocol layer.

Implements JSON-RPC 2.0 dispatching for MCP initialize, ping, tools/list,
tools/call, and notifications. Request-response subset only (no SSE, no sessions).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.thunder_client import ThunderClient
from src.tools import availability, deep_link, search_titles

logger = logging.getLogger(__name__)

# Tool definitions per design.md
TOOL_DEFINITIONS = [
    {
        "name": "search_titles",
        "description": (
            "Search a library's OverDrive catalog via the Thunder API. Returns titles "
            "with availability info, deep links, and copy/hold counts. Also returns facets "
            "(including valid subject IDs for the library)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "library_slug": {
                    "type": "string",
                    "description": "Library identifier (e.g., lcpl, fairfaxcounty, nypl)",
                },
                "subjects": {
                    "type": "array",
                    "items": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
                    "description": "Subject IDs to filter by",
                },
                "format": {
                    "type": "string",
                    "enum": ["audiobook", "ebook"],
                    "description": "Media format filter",
                },
                "available": {
                    "type": "boolean",
                    "description": "Only show currently borrowable titles",
                },
                "creator": {
                    "type": "string",
                    "description": "Author or narrator name search",
                },
                "bisac": {
                    "type": "string",
                    "description": "BISAC category code to filter by",
                },
                "series_id": {
                    "type": "integer",
                    "description": "Filter to a specific series by ID",
                },
                "under_hours": {
                    "type": "number",
                    "description": "Maximum audiobook length in hours (client-side filter)",
                },
                "per_page": {
                    "type": "integer",
                    "description": "Results per page (max 100)",
                },
                "page": {
                    "type": "integer",
                    "description": "Page number for pagination",
                },
                "maturity_level": {
                    "type": "string",
                    "enum": ["general", "juvenile", "youngadult", "adultonly"],
                    "description": "Content maturity filter",
                },
                "sort_by": {
                    "type": "string",
                    "description": "Sort order",
                },
                "query": {
                    "type": "string",
                    "description": "Full-text search on title+subtitle+description",
                },
            },
            "required": ["library_slug"],
        },
    },
    {
        "name": "get_availability",
        "description": (
            "Get raw availability data for specific titles at a library. "
            "Returns copies owned, copies available, holds count, and estimated wait days."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "library_slug": {
                    "type": "string",
                    "description": "Library identifier",
                },
                "title_ids": {
                    "type": "array",
                    "items": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
                    "description": "Array of OverDrive title IDs to check",
                },
            },
            "required": ["library_slug", "title_ids"],
        },
    },
    {
        "name": "get_deep_link",
        "description": (
            "Generate a direct OverDrive link for a specific title. "
            "Format: https://{slug}.overdrive.com/media/{id}"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title_id": {
                    "type": ["string", "integer"],
                    "description": "OverDrive numeric title ID",
                },
                "library_slug": {
                    "type": "string",
                    "description": "Library slug for the link context",
                },
            },
            "required": ["title_id", "library_slug"],
        },
    },
]


class MCPServer:
    """MCP 2025-06-18 JSON-RPC protocol handler.

    Handles initialize, ping, tools/list, tools/call, and notifications.
    Routes tool calls to the appropriate handler module.
    """

    def __init__(self, thunder_client: ThunderClient) -> None:
        self._client = thunder_client

    def handle_request(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch a JSON-RPC request to the appropriate handler.

        Args:
            body: Parsed JSON-RPC request body.

        Returns:
            JSON-RPC response dict, or None for notifications (HTTP 202).
        """
        # Validate JSON-RPC structure
        if not isinstance(body, dict):
            return self._error_response(None, -32600, "Invalid request")

        if body.get("jsonrpc") != "2.0":
            return self._error_response(body.get("id"), -32600, "Invalid request")

        method = body.get("method")
        if not method:
            return self._error_response(body.get("id"), -32600, "Invalid request")

        request_id = body.get("id")
        params = body.get("params", {})

        # Notifications have no id — return None (HTTP 202, empty body)
        if request_id is None:
            self._handle_notification(method, params)
            return None

        # Dispatch by method
        if method == "initialize":
            return self._success_response(request_id, self._handle_initialize())
        elif method == "ping":
            return self._success_response(request_id, {})
        elif method == "tools/list":
            return self._success_response(request_id, {"tools": TOOL_DEFINITIONS})
        elif method == "tools/call":
            return self._handle_tools_call(request_id, params)
        else:
            return self._error_response(request_id, -32601, f"Method not found: {method}")

    def _handle_initialize(self) -> dict[str, Any]:
        """Return InitializeResult per MCP 2025-06-18 spec."""
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "libby-mcp", "version": "1.0.0"},
        }

    def _handle_notification(self, method: str, params: Any) -> None:
        """Handle notifications (no response expected)."""
        if method == "notifications/initialized":
            logger.info("client_initialized")
        else:
            logger.debug("unknown_notification", extra={"method": method})

    def _handle_tools_call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch tools/call to the appropriate tool handler."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            result = self._dispatch_tool(tool_name, arguments)
        except Exception as e:
            logger.error("tool_error", extra={"tool": tool_name, "error": str(e)})
            return self._tool_error_response(request_id, f"Internal error in {tool_name}")

        # Check if the tool returned an error
        if isinstance(result, dict) and "error" in result:
            return self._tool_error_response(request_id, result["error"])

        # Success: wrap in MCP content block
        return self._tool_success_response(request_id, result)

    def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route to the correct tool handler."""
        if name == "search_titles":
            return search_titles.handle(arguments, self._client)
        elif name == "get_availability":
            return availability.handle(arguments, self._client)
        elif name == "get_deep_link":
            return deep_link.handle(arguments)
        else:
            return {"error": f"Unknown tool: {name}"}

    def _success_response(self, request_id: Any, result: Any) -> dict[str, Any]:
        """Build a JSON-RPC success response."""
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error_response(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        """Build a JSON-RPC error response (protocol-level fault)."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _tool_success_response(self, request_id: Any, result: Any) -> dict[str, Any]:
        """Build a successful tool result in MCP content block format."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result)}],
            },
        }

    def _tool_error_response(self, request_id: Any, error_message: str) -> dict[str, Any]:
        """Build a tool error result with isError: true."""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"error": error_message})}],
                "isError": True,
            },
        }
