"""Lambda handler — API Gateway event parsing, auth check, MCP dispatch.

Entry point for the Libby MCP server. Validates API key, parses the request,
routes to MCPServer, and returns properly formatted HTTP responses.
Module-level ThunderClient for warm invocation reuse.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from src.auth import validate_api_key
from src.mcp_server import MCPServer
from src.thunder_client import ThunderClient

logger = logging.getLogger(__name__)

# Module-level client — reused across warm Lambda invocations (NFR-1.2)
_thunder_client = ThunderClient()
_mcp_server = MCPServer(thunder_client=_thunder_client)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point for API Gateway HTTP API v2 events.

    Args:
        event: API Gateway HTTP API v2 event.
        context: Lambda context (unused).

    Returns:
        API Gateway response dict with statusCode, headers, body.
    """
    start = time.monotonic()
    try:
        # Extract headers and body
        headers = event.get("headers", {})
        body_str = event.get("body", "")

        # Auth check first
        if not validate_api_key(headers):
            return _response(401, {"error": "Invalid or missing API key"})

        # Parse JSON body
        try:
            body = json.loads(body_str) if body_str else {}
        except json.JSONDecodeError:
            return _response(400, {"error": "Invalid JSON in request body"})

        # Dispatch to MCP server
        result = _mcp_server.handle_request(body)

        # Notifications return None -> HTTP 202
        if result is None:
            duration = time.monotonic() - start
            logger.info(
                "request_complete",
                extra={"method": body.get("method"), "status": 202, "duration_ms": duration * 1000},
            )
            return {"statusCode": 202, "headers": _cors_headers(), "body": ""}

        # Normal JSON-RPC response -> HTTP 200
        duration = time.monotonic() - start
        logger.info(
            "request_complete",
            extra={"method": body.get("method"), "status": 200, "duration_ms": duration * 1000},
        )
        return _response(200, result)

    except Exception as e:
        duration = time.monotonic() - start
        logger.error(
            "unhandled_error",
            extra={"error": str(e), "duration_ms": duration * 1000},
            exc_info=True,
        )
        return _response(500, {"error": "Internal server error"})


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    """Build an API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": _cors_headers(),
        "body": json.dumps(body),
    }


def _cors_headers() -> dict[str, str]:
    """Standard response headers."""
    return {
        "Content-Type": "application/json",
    }
