"""Authentication module for MCP API key validation.

Validates incoming requests against a static API key stored in the
LIBBY_MCP_API_KEY environment variable. Uses case-insensitive header
lookup (API Gateway HTTP API lowercases all headers) and constant-time
comparison to prevent timing attacks.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

# ASGI type aliases (PEP 3333 / ASGI 3.0 shapes), kept local to avoid a
# dependency on a typing-only package.
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def validate_api_key(headers: dict[str, str]) -> bool:
    """Validate the MCP API key from request headers.

    Performs case-insensitive header name lookup and constant-time
    string comparison against the expected key.

    Args:
        headers: Request headers dict (keys may be any case).

    Returns:
        True if the Bearer token matches the expected API key, False otherwise.
    """
    expected_key = os.environ.get("LIBBY_MCP_API_KEY", "")
    if not expected_key:
        logger.error("LIBBY_MCP_API_KEY environment variable not set")
        return False

    # Case-insensitive header lookup (API Gateway HTTP API lowercases headers)
    normalized_headers = {k.lower(): v for k, v in headers.items()}
    auth_header = normalized_headers.get("authorization", "")

    if not auth_header:
        logger.info("auth_failed: missing authorization header")
        return False

    # Extract Bearer token
    if not auth_header.startswith("Bearer "):
        logger.info("auth_failed: missing Bearer prefix")
        return False

    token = auth_header[len("Bearer ") :]
    if not token:
        logger.info("auth_failed: empty Bearer value")
        return False

    # Constant-time comparison to prevent timing attacks
    is_valid = hmac.compare_digest(token, expected_key)
    if is_valid:
        logger.info("auth_success")
    else:
        logger.info("auth_failed: invalid token")

    return is_valid


class AuthMiddleware:
    """ASGI middleware: POST-only guard + mandatory Bearer token auth.

    Enforced order:
    1. Non-HTTP scopes (lifespan): pass through.
    2. Non-POST HTTP methods: HTTP 405 Method Not Allowed.
    3. POST with invalid/absent Authorization header: HTTP 401.
    4. POST with valid Bearer token: delegate to wrapped app.

    Claude.ai supports static Bearer headers via its connector UI
    ("Request headers" -> "authorization: Bearer <key>").
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        if method != "POST":
            await _send_simple(
                send,
                405,
                {"error": "Method not allowed"},
                extra_headers=[[b"allow", b"POST"]],
            )
            return

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not validate_api_key(headers):
            await _send_simple(
                send,
                401,
                {"error": "Invalid or missing API key"},
                extra_headers=[[b"www-authenticate", b"Bearer"]],
            )
            return

        await self._app(scope, receive, send)


async def _send_simple(
    send: Send,
    status: int,
    body_dict: dict[str, Any],
    extra_headers: list[list[bytes]] | None = None,
) -> None:
    """Send a simple JSON HTTP response."""
    body = json.dumps(body_dict).encode()
    headers: list[list[bytes]] = [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
