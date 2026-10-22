"""OAuth path router mounted in front of the FastMCP app (FR-F2).

An ASGI app that handles the OAuth endpoints and delegates every other path to
the wrapped FastMCP app. Mounted by ``_build_app()`` only when OAuth is enabled;
when disabled the FastMCP app is used directly and the stack is identical to v2.

PR-F1 handles the metadata documents and ``POST /register``. ``GET/POST
/authorize`` and ``POST /token`` are recognized OAuth-public paths (so
``AuthMiddleware`` lets them through) but their flow handlers land in PR-F2; until
then they respond ``501 not_implemented``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from src.oauth import config, metadata, registration

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Paths the OAuth router owns (everything else delegates to the FastMCP app).
WELL_KNOWN_PRM = "/.well-known/oauth-protected-resource"
WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"
REGISTER = "/register"
AUTHORIZE = "/authorize"
TOKEN = "/token"

OAUTH_PATHS = frozenset({WELL_KNOWN_PRM, WELL_KNOWN_AS, REGISTER, AUTHORIZE, TOKEN})


def is_oauth_path(path: str) -> bool:
    """True when ``path`` is an OAuth endpoint owned by this router.

    Used by ``AuthMiddleware`` to let OAuth-public paths bypass the ``/mcp`` auth
    gate (they are reached by unauthenticated clients and browsers).
    """
    return path.rstrip("/") in OAUTH_PATHS or path in OAUTH_PATHS


class OAuthRouter:
    """ASGI wrapper that dispatches OAuth paths and delegates the rest."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = str(scope.get("path", "")).rstrip("/") or "/"
        if path not in OAUTH_PATHS:
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        try:
            base = config.resolve_base_url(
                host=_header(scope, "host"),
                scheme=_request_scheme(scope),
            )
        except config.OAuthNotConfigured:
            await _send_json(send, 500, {"error": "oauth_not_configured"})
            return

        if path == WELL_KNOWN_PRM and method == "GET":
            await _send_json(send, 200, metadata.protected_resource_document(base))
            return
        if path == WELL_KNOWN_AS and method == "GET":
            await _send_json(send, 200, metadata.authorization_server_document(base))
            return
        if path == REGISTER and method == "POST":
            body = await _read_json_body(receive)
            status, payload = registration.register(body, base)
            await _send_json(send, status, payload)
            return
        if path in (AUTHORIZE, TOKEN):
            # Flow handlers land in PR-F2; the paths are reserved here so the
            # auth gate already treats them as public (is_oauth_path).
            await _send_json(send, 501, {"error": "not_implemented"})
            return

        await _send_json(send, 405, {"error": "Method not allowed"})


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return str(value.decode("latin-1"))
    return None


def _request_scheme(scope: Scope) -> str:
    # Honor a forwarded scheme behind a TLS proxy (uvicorn --proxy-headers sets
    # scope["scheme"]); default to the raw ASGI scheme, else http.
    forwarded = _header(scope, "x-forwarded-proto")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return str(scope.get("scheme") or "http")


async def _read_json_body(receive: Receive) -> dict[str, Any]:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body", False):
                break
        else:  # pragma: no cover - disconnect mid-body
            break
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _send_json(send: Send, status: int, body_dict: dict[str, Any]) -> None:
    body = json.dumps(body_dict).encode()
    headers = [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
