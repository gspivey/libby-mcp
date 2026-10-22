"""Token endpoint — ``POST /token`` (FR-F2, FR-F5, FR-F11).

Consumes ``application/x-www-form-urlencoded`` and supports two grants
(AC-F2.5/F2.6):

- ``authorization_code`` — verifies the signed code (``typ:"code"``, signature,
  ``exp``), checks the PKCE ``S256(code_verifier) == code_challenge``, and issues a
  signed access token + refresh token whose ``aud`` is the **canonical resource**
  ``<base>/mcp`` (AC-F11.5), regardless of the client-sent ``resource``.
- ``refresh_token`` — verifies the signed refresh token (``typ:"refresh"``) and
  **rotates** it (a new refresh JWT with a fresh ``jti``/``iat``) plus a new access
  token (AC-F2.6). Stateless: the previous refresh token stays valid until its own
  ``exp`` (documented deviation, HIGH-6).

Errors follow RFC 6749 / RFC 8707 (AC-F5.2): ``invalid_grant`` (bad/expired/
wrong-``typ`` code or refresh token, PKCE mismatch, redirect-URI mismatch),
``invalid_client`` (bad ``client_id``), ``invalid_request`` (malformed /
unsupported grant / missing field), ``invalid_target`` (non-canonical
``resource``). Typed-artifact verification rejects a token presented to the wrong
grant (HIGH-5): an access token replayed at ``/token`` fails because it is verified
as ``typ:"code"`` / ``typ:"refresh"``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from typing import Any

from src.oauth import jwt, registration
from src.oauth.metadata import OAuthError, resolve_resource

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """An RFC 6749 / RFC 8707 token-endpoint error.

    ``error`` is the OAuth error code (``invalid_grant`` / ``invalid_client`` /
    ``invalid_request`` / ``invalid_target`` / ``unsupported_grant_type``);
    ``status`` is the HTTP status (400 for all of these per RFC 6749 §5.2).
    """

    def __init__(self, error: str, description: str | None = None, status: int = 400) -> None:
        super().__init__(description or error)
        self.error = error
        self.description = description or error
        self.status = status


def _pkce_s256(verifier: str) -> str:
    """Compute the PKCE S256 challenge for ``verifier`` (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _verify_client(client_id: str, base: str) -> dict[str, Any]:
    """Verify the presented ``client_id`` or raise ``invalid_client``."""
    if not client_id:
        raise TokenError("invalid_client", "client_id is required")
    try:
        return registration.verify_client_id(client_id, base)
    except jwt.JWTError as e:
        raise TokenError("invalid_client", "unknown or invalid client_id") from e


def _issue_tokens(client_id: str, resource: str, base: str) -> dict[str, Any]:
    """Mint an access token + refresh token bound to the canonical ``resource``.

    Both tokens carry ``aud == resource`` (the canonical value, AC-F11.5) and a
    fresh ``jti`` so a rotated refresh token differs from its predecessor.
    """
    access = jwt.sign(
        {"aud": resource, "client_id": client_id, "jti": secrets.token_urlsafe(8)},
        typ="access",
        iss=base,
        ttl_seconds=jwt.access_ttl(),
    )
    refresh = jwt.sign(
        {"aud": resource, "client_id": client_id, "jti": secrets.token_urlsafe(8)},
        typ="refresh",
        iss=base,
        ttl_seconds=jwt.refresh_ttl(),
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": jwt.access_ttl(),
        "refresh_token": refresh,
    }


def _authorization_code_grant(form: dict[str, str], base: str) -> dict[str, Any]:
    """Exchange an authorization ``code`` + PKCE verifier for tokens (AC-F2.5)."""
    code = form.get("code", "")
    if not code:
        raise TokenError("invalid_request", "code is required")
    verifier = form.get("code_verifier", "")
    if not verifier:
        raise TokenError("invalid_request", "code_verifier is required (PKCE)")

    # Verify the signed code: typ:"code", signature, exp (HIGH-5 — an access or
    # refresh token replayed here fails the typ check).
    try:
        claims = jwt.verify(code, expected_typ="code", expected_iss=base)
    except jwt.JWTError as e:
        raise TokenError("invalid_grant", "authorization code is invalid or expired") from e

    # The client_id on the token request (when present) must match the code.
    req_client_id = form.get("client_id")
    code_client_id = str(claims.get("client_id", ""))
    if req_client_id is not None and req_client_id != "" and req_client_id != code_client_id:
        raise TokenError("invalid_grant", "client_id does not match the authorization code")
    _verify_client(code_client_id, base)

    # redirect_uri must match the one bound into the code (RFC 6749 §4.1.3).
    req_redirect = form.get("redirect_uri")
    if req_redirect is not None and req_redirect != claims.get("redirect_uri"):
        raise TokenError("invalid_grant", "redirect_uri does not match the authorization code")

    # PKCE: S256(verifier) must equal the challenge embedded in the code.
    challenge = str(claims.get("code_challenge", ""))
    if not hmac.compare_digest(_pkce_s256(verifier), challenge):
        raise TokenError("invalid_grant", "PKCE verification failed")

    # Resolve any resource param against the canonical value (AC-F11.3); the code's
    # bound resource is already canonical, and the issued aud is always canonical.
    resource = _resolve_resource_or_error(form.get("resource"), base)
    bound = str(claims.get("resource", resource))
    if bound != resource:  # pragma: no cover - code always binds canonical
        raise TokenError("invalid_target", "resource does not match the authorization code")

    return _issue_tokens(code_client_id, resource, base)


def _refresh_token_grant(form: dict[str, str], base: str) -> dict[str, Any]:
    """Rotate a refresh token and issue a new access token (AC-F2.6)."""
    refresh = form.get("refresh_token", "")
    if not refresh:
        raise TokenError("invalid_request", "refresh_token is required")

    # Verify the refresh token: typ:"refresh" (HIGH-5 — an access token or code
    # presented here fails the typ check and surfaces as invalid_grant).
    try:
        claims = jwt.verify(refresh, expected_typ="refresh", expected_iss=base)
    except jwt.JWTError as e:
        raise TokenError("invalid_grant", "refresh token is invalid or expired") from e

    client_id = str(claims.get("client_id", ""))
    # The refresh token's aud is already canonical; re-validate any sent resource.
    resource = _resolve_resource_or_error(form.get("resource"), base)
    bound = str(claims.get("aud", resource))
    if bound != resource:  # pragma: no cover - refresh always carries canonical aud
        raise TokenError("invalid_target", "resource does not match the refresh token")

    return _issue_tokens(client_id, resource, base)


def _resolve_resource_or_error(sent: str | None, base: str) -> str:
    """Resolve a ``resource`` form value or raise ``invalid_target`` (AC-F11.3)."""
    try:
        return resolve_resource(sent, base)
    except OAuthError as e:
        raise TokenError(e.error, e.description) from e


def token(form: dict[str, str], base: str) -> tuple[int, dict[str, Any]]:
    """Handle ``POST /token`` and return ``(status, json_body)``.

    Dispatches on ``grant_type``; a successful exchange returns ``(200, tokens)``,
    an error returns ``(status, {"error": ..., "error_description": ...})`` per
    RFC 6749 §5.2.
    """
    grant_type = form.get("grant_type", "")
    try:
        if grant_type == "authorization_code":
            body = _authorization_code_grant(form, base)
        elif grant_type == "refresh_token":
            body = _refresh_token_grant(form, base)
        elif not grant_type:
            raise TokenError("invalid_request", "grant_type is required")
        else:
            raise TokenError("unsupported_grant_type", f"unsupported grant_type: {grant_type}")
    except TokenError as e:
        logger.info("token_error: %s", e.error)
        return e.status, {"error": e.error, "error_description": e.description}
    return 200, body
