"""Authorization endpoint — ``GET/POST /authorize`` (FR-F1, FR-F2, FR-F9, FR-F11).

``GET /authorize`` renders a minimal owner-consent page; ``POST /authorize``
validates the owner secret (CRITICAL-3) and mints a short-lived signed
authorization ``code``. PKCE S256 is mandatory (AC-F2.4), the ``redirect_uri``
must exact-match a value registered for the ``client_id`` (AC-F9.1 / HIGH-9), and
any ``resource`` parameter is resolved to the canonical ``<base>/mcp`` or rejected
with ``invalid_target`` (AC-F11.3 / HIGH-7).

Security hardening (HIGH-C):

- Every reflected value (``state``, ``client_name``, ``redirect_uri``,
  ``client_id``, ``code_challenge``, ``resource``, …) is passed through
  ``html.escape(value, quote=True)`` before it is written into the consent HTML —
  in both element-text and attribute contexts — so the attacker-controlled
  ``client_name`` from public DCR cannot inject script (AC-F2.11).
- The HTML consent/error responses set ``Content-Security-Policy:
  frame-ancestors 'none'`` and ``Cache-Control: no-store`` (AC-F2.12).

The owner secret is compared with ``hmac.compare_digest`` (AC-F6.6); the dev
auto-approve path is reachable only off-Lambda with a loopback base URL
(AC-F1.9), and never waives the configured-owner-secret requirement (AC-F1.8).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
from typing import Any
from urllib.parse import urlencode

from src.oauth import config, jwt, registration
from src.oauth.metadata import OAuthError, resolve_resource

logger = logging.getLogger(__name__)

# PKCE: only S256 is supported (AC-F2.4 / AC-F2.2).
PKCE_METHOD = "S256"


class AuthorizeError(Exception):
    """A non-redirectable ``/authorize`` failure that renders an HTTP error page.

    Carries the HTTP ``status`` and an ``error`` code. Raised when the request is
    malformed *before* a trusted ``redirect_uri`` is established (bad client,
    unregistered redirect URI, missing PKCE, non-canonical ``resource``), so the
    error is shown as a page rather than bounced to an unvalidated URI
    (RFC 6749 §4.1.2.1 / AC-F9.1).
    """

    def __init__(self, status: int, error: str, description: str | None = None) -> None:
        super().__init__(description or error)
        self.status = status
        self.error = error
        self.description = description or error


def _pkce_s256(verifier: str) -> str:
    """Compute the PKCE S256 challenge for ``verifier`` (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _validate_request(params: dict[str, str], base: str) -> dict[str, Any]:
    """Validate the authorize request and return the resolved context.

    Enforces (before any code is minted or any redirect happens):

    - ``client_id`` is a valid signed client token (else ``invalid_client``);
    - ``redirect_uri`` exact-matches a registered value (AC-F9.1, HIGH-9);
    - PKCE ``code_challenge`` present and ``code_challenge_method == S256``
      (AC-F2.4);
    - ``response_type == code`` (the only supported type);
    - the ``resource`` parameter resolves to the canonical value, else
      ``invalid_target`` (AC-F11.3).

    Returns a context dict with the validated ``client_id``, ``redirect_uri``,
    ``code_challenge``, canonical ``resource``, and the registered ``client_name``.
    """
    client_id = params.get("client_id", "")
    if not client_id:
        raise AuthorizeError(400, "invalid_request", "client_id is required")
    try:
        client_claims = registration.verify_client_id(client_id, base)
    except jwt.JWTError as e:
        raise AuthorizeError(400, "invalid_client", "unknown or invalid client_id") from e

    registered = client_claims.get("redirect_uris")
    if not isinstance(registered, list):
        registered = []

    redirect_uri = params.get("redirect_uri", "")
    # Exact string match against a registered redirect URI (RFC 6749 §4.1.2.1).
    if not redirect_uri or redirect_uri not in registered:
        raise AuthorizeError(
            400,
            "invalid_request",
            "redirect_uri is missing or does not match a registered value",
        )

    response_type = params.get("response_type", "code")
    if response_type != "code":
        raise AuthorizeError(
            400, "unsupported_response_type", "only response_type=code is supported"
        )

    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")
    if not code_challenge or code_challenge_method != PKCE_METHOD:
        raise AuthorizeError(
            400,
            "invalid_request",
            "PKCE is required: provide code_challenge and code_challenge_method=S256",
        )

    # Resolve the resource indicator to the canonical value or reject (AC-F11.3).
    try:
        resource = resolve_resource(params.get("resource"), base)
    except OAuthError as e:
        raise AuthorizeError(400, e.error, e.description) from e

    client_name = client_claims.get("client_name")
    if not isinstance(client_name, str):
        client_name = ""

    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "resource": resource,
        "state": params.get("state"),
        "client_name": client_name,
    }


def _mint_code(ctx: dict[str, Any], base: str) -> str:
    """Mint a signed, short-lived authorization code (AC-F2.9).

    The code is a ``typ: "code"`` JWT at the configurable code TTL (60s default)
    embedding the PKCE ``code_challenge``, the ``redirect_uri``, the ``client_id``,
    and the bound **canonical** ``resource``. ``/token`` validates it without a
    server-side store (CRITICAL-2).
    """
    return jwt.sign(
        {
            "code_challenge": ctx["code_challenge"],
            "redirect_uri": ctx["redirect_uri"],
            "client_id": ctx["client_id"],
            "resource": ctx["resource"],
        },
        typ="code",
        iss=base,
        ttl_seconds=jwt.code_ttl(),
    )


def _redirect_location(redirect_uri: str, code: str, state: str | None) -> str:
    """Build the success redirect URL with ``code`` (and ``state`` when present)."""
    query = {"code": code}
    if state is not None:
        query["state"] = state
    sep = "&" if ("?" in redirect_uri) else "?"
    return f"{redirect_uri}{sep}{urlencode(query)}"


def _consent_html(ctx: dict[str, Any]) -> str:
    """Render the consent page, HTML-escaping every reflected value (AC-F2.11).

    Every request-/registration-sourced value is escaped with
    ``html.escape(value, quote=True)`` before being written into element text or a
    hidden-input ``value="…"`` attribute. Only the fixed form skeleton is a static
    literal; no reflected value is concatenated raw.
    """

    def esc(value: str | None) -> str:
        return html.escape(value or "", quote=True)

    client_name = esc(ctx["client_name"]) or "an application"
    hidden_fields = {
        "client_id": ctx["client_id"],
        "redirect_uri": ctx["redirect_uri"],
        "code_challenge": ctx["code_challenge"],
        "code_challenge_method": PKCE_METHOD,
        "resource": ctx["resource"],
        "response_type": "code",
    }
    if ctx.get("state") is not None:
        hidden_fields["state"] = ctx["state"]

    hidden_inputs = "\n".join(
        f'      <input type="hidden" name="{esc(name)}" value="{esc(value)}">'
        for name, value in hidden_fields.items()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Authorize access — libby-mcp</title>
</head>
<body>
  <main>
    <h1>Authorize access</h1>
    <p>The application <strong>{client_name}</strong> is requesting access to this
      libby-mcp server. Enter the owner secret to approve this connection.</p>
    <form method="post" action="/authorize">
{hidden_inputs}
      <label for="owner_secret">Owner secret</label>
      <input type="password" id="owner_secret" name="owner_secret" autocomplete="off" required>
      <button type="submit">Approve</button>
    </form>
  </main>
</body>
</html>
"""


def _html_headers() -> list[tuple[str, str]]:
    """Security headers for the HTML consent / error responses (AC-F2.12)."""
    return [
        ("content-type", "text/html; charset=utf-8"),
        ("content-security-policy", "frame-ancestors 'none'"),
        ("cache-control", "no-store"),
    ]


def _error_html(error: str, description: str) -> str:
    """Render an HTML error page, escaping the error code + description (AC-F9.1)."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Authorization error — libby-mcp</title></head>
<body>
  <main>
    <h1>Authorization error</h1>
    <p><strong>{html.escape(error, quote=True)}</strong></p>
    <p>{html.escape(description, quote=True)}</p>
  </main>
</body>
</html>
"""


def authorize_page(params: dict[str, str], base: str) -> tuple[int, str, list[tuple[str, str]]]:
    """Handle ``GET /authorize``: render the consent page (AC-F2.4 / F2.11 / F2.12).

    Returns ``(status, html_body, headers)``. On a validation failure before a
    trusted ``redirect_uri`` is established, returns an HTTP 400 error **page**
    (not a redirect, AC-F9.1).
    """
    try:
        ctx = _validate_request(params, base)
    except AuthorizeError as e:
        logger.info("authorize_page_rejected: %s", e.error)
        return e.status, _error_html(e.error, e.description), _html_headers()
    return 200, _consent_html(ctx), _html_headers()


def authorize_submit(
    params: dict[str, str], base: str, *, is_lambda: bool
) -> tuple[int, str, list[tuple[str, str]]]:
    """Handle ``POST /authorize``: owner gate → mint code + redirect (AC-F1.6).

    Returns ``(status, body, headers)``:

    - On a request-validation failure (bad client / redirect URI / PKCE /
      ``resource``): HTTP 400 error **page** (AC-F9.1).
    - On a missing/incorrect owner secret (when auto-approve is not active): HTTP
      401 error page **before any code is issued** (AC-F1.6 / CRITICAL-3).
    - On approval (correct owner secret, or dev auto-approve): HTTP 302 redirect
      to the registered ``redirect_uri`` carrying ``code`` (+ ``state``).
    """
    try:
        ctx = _validate_request(params, base)
    except AuthorizeError as e:
        logger.info("authorize_submit_rejected: %s", e.error)
        return e.status, _error_html(e.error, e.description), _html_headers()

    if not _owner_approved(params.get("owner_secret"), is_lambda=is_lambda):
        logger.info("authorize_submit_denied: owner secret missing or incorrect")
        return (
            401,
            _error_html("access_denied", "owner secret missing or incorrect"),
            _html_headers(),
        )

    code = _mint_code(ctx, base)
    location = _redirect_location(ctx["redirect_uri"], code, ctx.get("state"))
    logger.info("authorize_approved: code issued for client")
    headers = [("location", location), ("cache-control", "no-store")]
    return 302, "", headers


def _owner_approved(owner_secret: str | None, *, is_lambda: bool) -> bool:
    """Decide whether consent is approved (AC-F1.6 / AC-F1.9).

    Returns True when the dev auto-approve path is active (off-Lambda + loopback
    base URL + flag), **or** when the presented ``owner_secret`` matches the
    effective owner secret in constant time (``hmac.compare_digest``). The
    configured-owner-secret startup check (AC-F1.8) guarantees an owner secret is
    always configured in OAuth mode, so there is no state in which auto-approve
    runs with none set.
    """
    if config.dev_auto_approve_active(is_lambda):
        logger.warning(
            "authorize: DEV auto-approve active — consent granted without the owner "
            "secret (never enable LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE on an exposed deployment)"
        )
        return True
    expected = config.effective_owner_secret()
    if expected is None:
        # Startup (AC-F1.8) prevents this in OAuth mode; fail closed defensively.
        return False
    if owner_secret is None:
        return False
    return hmac.compare_digest(owner_secret, expected)
