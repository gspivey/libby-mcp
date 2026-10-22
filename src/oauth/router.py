"""OAuth path router mounted in front of the FastMCP app (FR-F2).

An ASGI app that handles the OAuth endpoints and delegates every other path to
the wrapped FastMCP app. Mounted by ``_build_app()`` only when OAuth is enabled;
when disabled the FastMCP app is used directly and the stack is identical to v2.

PR-F1 handled the metadata documents and ``POST /register``. PR-F2 adds the full
``GET/POST /authorize`` consent + code flow and the ``POST /token`` exchange /
refresh rotation, all dispatched here on the same ASGI app as ``/mcp``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qs

from src.oauth import authorize, config, metadata, registration, token

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
        if path == AUTHORIZE and method == "GET":
            params = _query_params(scope)
            status, html_body, headers = authorize.authorize_page(params, base)
            await _send_html(send, status, html_body, headers)
            return
        if path == AUTHORIZE and method == "POST":
            form = await _read_form_body(receive)
            status, body_text, headers = authorize.authorize_submit(
                form, base, is_lambda=_is_lambda()
            )
            if status in (301, 302, 303, 307, 308):
                await _send_redirect(send, status, headers)
            else:
                await _send_html(send, status, body_text, headers)
            return
        if path == TOKEN and method == "POST":
            form = await _read_form_body(receive)
            status, payload = token.token(form, base)
            await _send_json(send, status, payload)
            return

        await _send_json(send, 405, {"error": "Method not allowed"})


def _is_lambda() -> bool:
    """True when running under the Lambda runtime (``AWS_LAMBDA_RUNTIME_API``)."""
    return "AWS_LAMBDA_RUNTIME_API" in os.environ


def _query_params(scope: Scope) -> dict[str, str]:
    """Parse the request query string into a flat ``{name: value}`` dict."""
    raw = scope.get("query_string", b"") or b""
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    parsed = parse_qs(raw.decode("latin-1"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


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


async def _read_form_body(receive: Receive) -> dict[str, str]:
    """Read an ``application/x-www-form-urlencoded`` body into a flat dict."""
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
    parsed = parse_qs(raw.decode("latin-1"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def _encode_headers(headers: list[tuple[str, str]]) -> list[list[bytes]]:
    return [[k.encode("latin-1"), v.encode("latin-1")] for k, v in headers]


async def _send_html(
    send: Send, status: int, body_text: str, headers: list[tuple[str, str]]
) -> None:
    """Send an HTML response with the supplied (already security-set) headers."""
    body = body_text.encode("utf-8")
    encoded = _encode_headers(headers)
    encoded.append([b"content-length", str(len(body)).encode()])
    await send({"type": "http.response.start", "status": status, "headers": encoded})
    await send({"type": "http.response.body", "body": body})


async def _send_redirect(send: Send, status: int, headers: list[tuple[str, str]]) -> None:
    """Send an empty-body redirect with the supplied ``Location`` headers."""
    encoded = _encode_headers(headers)
    encoded.append([b"content-length", b"0"])
    await send({"type": "http.response.start", "status": status, "headers": encoded})
    await send({"type": "http.response.body", "body": b""})
