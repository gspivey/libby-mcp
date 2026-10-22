"""RFC 7591 Dynamic Client Registration — public clients only (FR-F2, HIGH-8).

``POST /register`` registers every client as a **public** client (PKCE, no
``client_secret`` issued or stored). The returned ``client_id`` is itself a signed
stateless token (``typ: "client"``) embedding the registered ``redirect_uris`` and
``client_name``, so any instance validates it without a server-side client table
(AC-F2.8 / F6.3). On success it returns **HTTP 201** with the exact public-client
body (AC-F2.3); a missing/empty/invalid ``redirect_uris`` is rejected with
**HTTP 400 ``invalid_redirect_uri``** (RFC 7591 §3.2.2).
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from src.oauth import jwt

# Redirect-URI scheme rules (FR-F9 / HIGH-9). ``https`` for any host; ``http`` for
# loopback only; custom app schemes (e.g. ``cursor://``) are accepted as-is.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback_host(host: str) -> bool:
    # Strip a bracketed IPv6 literal and any :port before comparing.
    h = host.strip().lower()
    if h.startswith("[") and "]" in h:
        h = h[1 : h.index("]")]
    elif ":" in h:
        h = h.rsplit(":", 1)[0]
    return h in _LOOPBACK_HOSTS


def is_valid_redirect_uri(uri: str) -> bool:
    """True if ``uri`` satisfies the redirect-URI scheme rules (AC-F9.2/F9.3).

    - ``https://`` for any host;
    - ``http://`` for loopback hosts only (``127.0.0.1``/``localhost``/``::1``);
    - a custom app scheme (any scheme other than ``http``/``https``, e.g.
      ``cursor://``) is accepted and is not subject to the HTTPS rule.
    """
    if not isinstance(uri, str) or not uri.strip():
        return False
    parts = urlsplit(uri.strip())
    scheme = parts.scheme.lower()
    if not scheme:
        return False
    if scheme == "https":
        return bool(parts.netloc)
    if scheme == "http":
        return _is_loopback_host(parts.netloc)
    # Custom app scheme (cursor://, etc.) — accepted, not HTTPS-gated.
    return True


def mint_client_id(redirect_uris: list[str], client_name: str, base: str) -> str:
    """Mint a signed, stateless ``client_id`` (AC-F6.3 / F2.8).

    The HMAC covers only the public payload (``redirect_uris``, ``client_name``,
    ``iat``, ``typ: "client"``, ``iss``) — there is no ``client_secret`` anywhere
    (HIGH-8). The token carries no ``exp`` (client registrations do not expire).
    """
    return jwt.sign(
        {"redirect_uris": list(redirect_uris), "client_name": client_name},
        typ="client",
        iss=base,
        ttl_seconds=None,
    )


def verify_client_id(client_id: str, base: str) -> dict[str, Any]:
    """Verify a presented ``client_id`` and return its claims (raises on failure).

    Used by ``/authorize`` + ``/token`` (PR-F2) to recover the registered
    ``redirect_uris`` without a server-side store.
    """
    return jwt.verify(client_id, expected_typ="client", expected_iss=base)


def register(body: dict[str, Any], base: str) -> tuple[int, dict[str, Any]]:
    """Handle ``POST /register`` (RFC 7591) and return ``(status, json_body)``.

    On success returns ``(201, public_client_body)``; on an invalid/missing
    ``redirect_uris`` returns ``(400, {"error": "invalid_redirect_uri"})``.
    """
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return 400, {"error": "invalid_redirect_uri"}
    if not all(is_valid_redirect_uri(u) for u in redirect_uris):
        return 400, {"error": "invalid_redirect_uri"}

    client_name = body.get("client_name")
    if not isinstance(client_name, str):
        client_name = ""

    issued_at = int(time.time())
    client_id = mint_client_id(redirect_uris, client_name, base)
    return 201, {
        "client_id": client_id,
        "client_id_issued_at": issued_at,
        "redirect_uris": list(redirect_uris),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
