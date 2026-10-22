"""OAuth metadata documents + canonical resource helpers (FR-F2, FR-F11, HIGH-7).

Serves the two discovery documents on the same ASGI app as ``/mcp``:

- RFC 9728 Protected Resource Metadata (``/.well-known/oauth-protected-resource``)
- RFC 8414 Authorization Server Metadata (``/.well-known/oauth-authorization-server``)

and defines the server's single **canonical resource** identity
(``<base>/mcp``) used as every token's ``aud`` and the one value an incoming
``resource`` parameter must equal (RFC 8707 Resource Indicators).
"""

from __future__ import annotations

from typing import Any


class OAuthError(Exception):
    """An OAuth protocol error with an RFC 6749 / RFC 8707 ``error`` code.

    Raised by the resource-indicator resolver (and reused by the flow handlers in
    PR-F2) so a non-canonical ``resource`` surfaces as ``invalid_target``.
    """

    def __init__(self, error: str, description: str | None = None) -> None:
        super().__init__(description or error)
        self.error = error
        self.description = description


def canonical_resource(base: str) -> str:
    """The canonical resource identifier: ``<base>/mcp`` (AC-F11.1).

    ``base`` is the resolved public base-URL origin (no trailing ``/mcp``); a
    trailing slash on the base is stripped so the result is exactly ``<base>/mcp``.
    """
    return f"{base.rstrip('/')}/mcp"


def normalize_resource(value: str) -> str:
    """Normalize a resource string for comparison (AC-F11.2).

    Strips exactly one optional trailing slash on the path, so ``<base>/mcp`` and
    ``<base>/mcp/`` compare equal. No other path/query/fragment variation is
    treated as equal.
    """
    return value.rstrip("/")


def resolve_resource(sent: str | None, base: str) -> str:
    """Resolve an incoming ``resource`` to the canonical value or reject it.

    - A missing / empty ``resource`` defaults to the canonical value (AC-F11.4).
    - A ``resource`` equal to canonical (after trailing-slash normalization) is
      accepted and the canonical form is returned (AC-F11.2).
    - Any other value is rejected with ``invalid_target`` (AC-F11.3).

    The return value is **always** the canonical form, so a client can never
    widen or redirect the audience (AC-F11.5).
    """
    canonical = canonical_resource(base)
    if sent is None or sent.strip() == "":
        return canonical
    if normalize_resource(sent.strip()) != normalize_resource(canonical):
        raise OAuthError("invalid_target", "resource does not match this MCP server")
    return canonical


def protected_resource_document(base: str) -> dict[str, Any]:
    """RFC 9728 Protected Resource Metadata (AC-F2.1 / F3.3 / F11.1).

    ``resource`` is the canonical ``<base>/mcp``; ``authorization_servers[0]`` is
    the AS issuer (``base``), so the PRM resolves to the AS whose metadata the
    ``/.well-known/oauth-authorization-server`` document serves (AC-F3.3).
    """
    return {
        "resource": canonical_resource(base),
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    }


def authorization_server_document(base: str) -> dict[str, Any]:
    """RFC 8414 Authorization Server Metadata (AC-F2.2).

    ``code_challenge_methods_supported`` is ``["S256"]`` and the advertised grant
    types are ``authorization_code`` + ``refresh_token`` (never
    ``client_credentials``, AC-F4.5). Every client is public
    (``token_endpoint_auth_methods_supported == ["none"]``, HIGH-8).
    """
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
