"""Authentication middleware — three coexisting auth modes (FR-F1, FR-F3).

The server supports three auth modes selected by environment variables, in both
Docker and Lambda, with no deployment-specific fork (AC-F1.5):

| ``LIBBY_MCP_API_KEY`` | ``LIBBY_MCP_OAUTH_ENABLED`` | Mode | ``/mcp`` behavior |
|---|---|---|---|
| unset | not ``true`` | No-auth | Docker: serve unauthenticated (AC-F1.1); Lambda: 500 ``authentication_not_configured`` (AC-F1.1-lambda) |
| set | not ``true`` | Static Bearer | require ``Bearer <key>`` (AC-F1.2) |
| unset | ``true`` | OAuth | require a valid JWT access token (AC-F1.3) |
| set | ``true`` | Static Bearer + OAuth | accept Bearer if it equals the static key **or** validates as a JWT (AC-F1.4) |

Middleware order (HIGH-1), applied in ``__call__``:

1. Non-HTTP scope (lifespan) passes straight through.
2. OAuth-public paths (``/.well-known/*``, ``/register``, ``/authorize``,
   ``/token``) bypass the auth gate entirely when OAuth is on (AC reached by
   unauthenticated clients/browsers; the owner-consent check lives inside the
   authorize handler, not here).
3. Non-POST on ``/mcp`` → HTTP 405 before any auth logic (AC-E2.3 / HIGH-1).
4. Auth check on ``POST /mcp`` per the selected mode, including the Lambda
   fail-closed rule (AC-F1.1-lambda / HIGH-13).

All secret comparisons use ``hmac.compare_digest`` (AC-F6.6).
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

# Runtime auto-detect (same signal as handler._IS_LAMBDA). Read lazily via a
# helper so a monkeypatched env in tests is honored per request.
_LAMBDA_ENV = "AWS_LAMBDA_RUNTIME_API"


def _env(name: str) -> str | None:
    """Read a config env var, normalizing unset and empty-string to None (HIGH-2).

    An **unset** variable and a variable set to an **empty / whitespace-only
    string** are treated identically — both yield ``None`` so the documented
    default (or absence-of-value behavior) applies (NFR-8.1). Every config
    call site reads through this helper so no site re-implements the check; this
    is what makes an empty ``LIBBY_MCP_API_KEY`` read as "no key" rather than
    "configured with an empty key".
    """
    v = os.environ.get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _is_lambda() -> bool:
    return _LAMBDA_ENV in os.environ


def _oauth_enabled() -> bool:
    return _env("LIBBY_MCP_OAUTH_ENABLED") == "true"


def _bearer_token(headers: dict[str, str]) -> str | None:
    """Extract the Bearer token from a (case-insensitive) Authorization header."""
    normalized = {k.lower(): v for k, v in headers.items()}
    auth_header = normalized.get("authorization", "")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer ") :]
    return token or None


def validate_api_key(headers: dict[str, str]) -> bool:
    """Validate the static Bearer API key (constant-time; AC-F1.2 / F6.6).

    Case-insensitive header lookup; ``hmac.compare_digest`` against
    ``LIBBY_MCP_API_KEY``. Returns False when no key is configured or the token
    is missing/mismatched.
    """
    expected_key = _env("LIBBY_MCP_API_KEY")
    if not expected_key:
        return False
    token = _bearer_token(headers)
    if token is None:
        logger.info("auth_failed: missing or malformed Authorization header")
        return False
    is_valid = hmac.compare_digest(token, expected_key)
    logger.info("auth_success" if is_valid else "auth_failed: invalid static token")
    return is_valid


def _validate_oauth_token(headers: dict[str, str], base: str) -> bool:
    """Validate a JWT access token on ``/mcp`` (AC-F1.3 / F5.3 / F11.6).

    Checks signature, ``exp``, ``typ == "access"``, ``iss``, and
    ``aud == canonical_resource`` (``<base>/mcp``). Returns False on any failure
    (so the caller emits a 401). Imported lazily to avoid a hard dependency on the
    OAuth package when OAuth is disabled.
    """
    from src.oauth import jwt
    from src.oauth.metadata import canonical_resource

    token = _bearer_token(headers)
    if token is None:
        return False
    try:
        claims = jwt.verify(token, expected_typ="access", expected_iss=base)
    except jwt.JWTError:
        logger.info("auth_failed: invalid JWT access token")
        return False
    if claims.get("aud") != canonical_resource(base):
        logger.info("auth_failed: JWT aud is not the canonical resource")
        return False
    return True


class AuthMiddleware:
    """ASGI middleware enforcing the three-mode auth policy (FR-F1)."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # (1) non-http (lifespan) passes through.
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        oauth_on = _oauth_enabled()

        # (2) OAuth-public paths bypass the auth gate entirely (owner-consent
        # gate lives in the authorize handler, not here).
        if oauth_on and _is_oauth_public_path(path):
            await self._app(scope, receive, send)
            return

        # (3) non-POST on /mcp → 405 before any auth logic (HIGH-1).
        if path.rstrip("/") == "/mcp" and str(scope.get("method", "")).upper() != "POST":
            await _send_simple(
                send,
                405,
                {"error": "Method not allowed"},
                extra_headers=[[b"allow", b"POST"]],
            )
            return

        # (4) auth check on POST /mcp per the selected mode.
        has_key = _env("LIBBY_MCP_API_KEY") is not None

        # No-auth mode: neither a static key nor OAuth is configured.
        if not has_key and not oauth_on:
            if _is_lambda():
                # Lambda fail-closed (HIGH-13 / AC-F1.1-lambda): a no-auth Lambda
                # would silently expose /mcp, so fail closed on the first request
                # (logged) rather than crash at import.
                logger.error("authentication_not_configured: Lambda requires an auth mechanism")
                await _send_simple(send, 500, {"error": "authentication_not_configured"})
                return
            # Docker no-auth: serve unauthenticated (AC-F1.1).
            await self._app(scope, receive, send)
            return

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}

        if _authorized(headers, has_key=has_key, oauth_on=oauth_on):
            await self._app(scope, receive, send)
            return

        await self._unauthorized(send, oauth_on=oauth_on, scope=scope)

    async def _unauthorized(self, send: Send, *, oauth_on: bool, scope: Scope) -> None:
        """Send a 401, with an RFC 9728 ``resource_metadata`` challenge in OAuth mode."""
        extra_headers: list[list[bytes]] = [[b"www-authenticate", b"Bearer"]]
        if oauth_on:
            challenge = _resource_metadata_challenge(scope)
            if challenge is not None:
                extra_headers = [[b"www-authenticate", challenge.encode("latin-1")]]
        await _send_simple(
            send,
            401,
            {"error": "Invalid or missing API key"},
            extra_headers=extra_headers,
        )


def _authorized(headers: dict[str, str], *, has_key: bool, oauth_on: bool) -> bool:
    """Decide authorization for ``POST /mcp`` per mode (AC-F1.2/F1.3/F1.4).

    Order: constant-time static-key compare first (cheap, no crypto parse); on a
    mismatch, if OAuth is enabled, validate the token as a JWT access token. A
    valid static key OR a valid JWT authorizes the request (AC-F1.4).
    """
    if has_key and validate_api_key(headers):
        return True
    if oauth_on:
        from src.oauth import config

        try:
            base = config.resolve_base_url(
                host=headers.get("host"),
                scheme=headers.get("x-forwarded-proto") or "https",
            )
        except config.OAuthNotConfigured:
            return False
        return _validate_oauth_token(headers, base)
    return False


def _is_oauth_public_path(path: str) -> bool:
    from src.oauth.router import is_oauth_path

    return is_oauth_path(path)


def _resource_metadata_challenge(scope: Scope) -> str | None:
    """Build the RFC 9728 ``WWW-Authenticate`` challenge value (AC-F3.1/F3.2).

    The ``resource_metadata`` URL is absolute and built from the trusted base URL
    (``LIBBY_MCP_PUBLIC_BASE_URL``; Host-derived only when the base is loopback).
    Returns ``None`` when the base URL cannot be resolved (so the caller falls
    back to a bare ``Bearer`` challenge rather than a spoofable URL).
    """
    from src.oauth import config

    host = None
    scheme = None
    for key, value in scope.get("headers", []):
        lk = key.lower()
        if lk == b"host":
            host = value.decode("latin-1")
        elif lk == b"x-forwarded-proto":
            scheme = value.decode("latin-1").split(",")[0].strip()
    if scheme is None:
        scheme = str(scope.get("scheme") or "https")
    try:
        base = config.resolve_base_url(host=host, scheme=scheme)
    except config.OAuthNotConfigured:
        return None
    url = f"{base}/.well-known/oauth-protected-resource"
    return f'Bearer resource_metadata="{url}"'


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
