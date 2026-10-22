"""OAuth configuration resolution + startup checks (FR-F1, FR-F6, FR-F10, HIGH-10).

Centralizes the env-driven OAuth configuration so no call site re-implements it:

- :func:`oauth_enabled` — ``LIBBY_MCP_OAUTH_ENABLED == "true"`` (empty == unset).
- :func:`resolve_base_url` — the **trusted** public base URL for all generated
  OAuth URLs. ``LIBBY_MCP_PUBLIC_BASE_URL`` is used verbatim when set; the request
  Host is used only when the resolved base is loopback (AC-F10.2 / F5a). When the
  base is unset and the request Host is non-loopback, an :class:`OAuthNotConfigured`
  is raised so the routes return HTTP 500 ``oauth_not_configured`` (AC-F10.1).
- :func:`effective_owner_secret` — ``LIBBY_MCP_OAUTH_OWNER_SECRET`` else
  ``LIBBY_MCP_API_KEY`` (AC-F1.6).
- :func:`check_startup_config` — fails fast when OAuth is enabled but the signing
  key (AC-F6.2) or the owner secret (AC-F1.8) is missing, or when dev auto-approve
  is requested without a loopback base URL (AC-F1.10).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from src.auth import _env
from src.oauth import jwt

OAUTH_ENABLED_ENV = "LIBBY_MCP_OAUTH_ENABLED"
PUBLIC_BASE_URL_ENV = "LIBBY_MCP_PUBLIC_BASE_URL"
OWNER_SECRET_ENV = "LIBBY_MCP_OAUTH_OWNER_SECRET"
API_KEY_ENV = "LIBBY_MCP_API_KEY"
DEV_AUTO_APPROVE_ENV = "LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class OAuthNotConfigured(Exception):
    """Raised when a non-loopback OAuth URL cannot be built (AC-F10.1).

    The router maps this to HTTP 500 ``oauth_not_configured`` rather than
    emitting a spoofable Host-derived URL.
    """


class OAuthStartupError(Exception):
    """Raised at app build time for a fatal OAuth misconfiguration.

    Covers the missing signing key (AC-F6.2), the missing owner secret
    (AC-F1.8), and dev auto-approve requested without a loopback base URL
    (AC-F1.10).
    """


def oauth_enabled() -> bool:
    """True when ``LIBBY_MCP_OAUTH_ENABLED`` is exactly ``true`` (empty == unset)."""
    return _env(OAUTH_ENABLED_ENV) == "true"


def dev_auto_approve_requested() -> bool:
    """True when ``LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE`` is exactly ``true``."""
    return _env(DEV_AUTO_APPROVE_ENV) == "true"


def dev_auto_approve_active(is_lambda: bool) -> bool:
    """True when dev auto-approve is actually in effect at request time (AC-F1.9).

    Auto-approve (skipping the interactive owner-secret consent form) runs **only**
    when **all three** hold:

    1. ``LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true`` is explicitly set, **and**
    2. the process is **not** on Lambda (``is_lambda`` is False), **and**
    3. ``LIBBY_MCP_PUBLIC_BASE_URL`` is explicitly set to a loopback origin
       (host exactly ``127.0.0.1``/``localhost``/``::1``).

    This never relaxes the AC-F1.8 startup requirement that an owner secret is
    configured — the server refuses to start in OAuth mode with no owner secret,
    so there is no state in which auto-approve runs with none configured. It only
    suppresses the consent-form prompt. The startup check (``check_startup_config``)
    already fails fast when the flag is set with an unset/non-loopback base URL
    (AC-F1.10), so this request-time helper is a defensive re-check.
    """
    if not dev_auto_approve_requested():
        return False
    if is_lambda:
        return False
    base = _env(PUBLIC_BASE_URL_ENV)
    if base is None or not is_loopback_base(base):
        return False
    return True


def effective_owner_secret() -> str | None:
    """The owner-consent secret: owner-secret env else API key (AC-F1.6)."""
    return _env(OWNER_SECRET_ENV) or _env(API_KEY_ENV)


def _host_of(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    return host.lower()


def is_loopback_base(base: str) -> bool:
    """True when ``base``'s host is a loopback host (``127.0.0.1``/``localhost``/``::1``)."""
    return _host_of(base) in _LOOPBACK_HOSTS


def _request_base_url(scope_host: str | None, scheme: str | None) -> str | None:
    """Derive a base URL from the request Host (loopback-only use, AC-F10.2)."""
    if not scope_host:
        return None
    sch = scheme or "http"
    return f"{sch}://{scope_host}"


def resolve_base_url(host: str | None = None, scheme: str | None = None) -> str:
    """Resolve the trusted public base URL for generated OAuth URLs (HIGH-10).

    - ``LIBBY_MCP_PUBLIC_BASE_URL`` is used verbatim when set-and-non-empty. If it
      is non-loopback it MUST be ``https`` (a non-loopback ``http`` base is a
      misconfiguration → :class:`OAuthNotConfigured`, AC-F5.1/F10.1).
    - When unset, the request Host is used **only** if it is loopback (local dev,
      AC-F10.2); a non-loopback Host with no configured base URL raises
      :class:`OAuthNotConfigured` → HTTP 500 ``oauth_not_configured`` (AC-F10.1).

    Returns the base URL as an origin (no trailing ``/mcp`` path).
    """
    configured = _env(PUBLIC_BASE_URL_ENV)
    if configured is not None:
        base = configured.rstrip("/")
        if not is_loopback_base(base) and urlsplit(base).scheme != "https":
            raise OAuthNotConfigured(
                f"{PUBLIC_BASE_URL_ENV} must be https for a non-loopback deployment"
            )
        return base

    derived = _request_base_url(host, scheme)
    if derived is not None and is_loopback_base(derived):
        return derived.rstrip("/")
    raise OAuthNotConfigured(f"{PUBLIC_BASE_URL_ENV} is required for a non-loopback deployment")


def check_startup_config(is_lambda: bool) -> None:
    """Fail fast on a fatal OAuth misconfiguration at app build time.

    Only runs when OAuth is enabled. Enforces:

    - signing key present and >= 32 bytes (AC-F6.2 / F6.1);
    - effective owner secret present and non-empty — unconditional, including
      when dev auto-approve is requested (AC-F1.8, CRITICAL-1);
    - dev auto-approve requested → ``LIBBY_MCP_PUBLIC_BASE_URL`` must be set to a
      loopback origin, else refuse to start (AC-F1.10, HIGH-6). Off-Lambda is a
      further precondition for auto-approve to actually run (enforced at request
      time), but an unset base URL is a startup error regardless.
    """
    if not oauth_enabled():
        return

    # Signing key (AC-F6.2): derive once; SigningKeyError → refuse to start.
    try:
        jwt.signing_key()
    except jwt.SigningKeyError as e:
        raise OAuthStartupError(str(e)) from e

    # Owner secret (AC-F1.8): required unconditionally in OAuth mode.
    if effective_owner_secret() is None:
        raise OAuthStartupError(
            "OAuth is enabled but no owner secret is configured: set "
            f"{OWNER_SECRET_ENV} (or {API_KEY_ENV}). The server will not "
            "auto-approve consent as a fallback."
        )

    # Dev auto-approve (AC-F1.10): if requested, the loopback base URL must be
    # declared explicitly — no implicit loopback default.
    if dev_auto_approve_requested():
        base = _env(PUBLIC_BASE_URL_ENV)
        if base is None or not is_loopback_base(base):
            raise OAuthStartupError(
                f"{DEV_AUTO_APPROVE_ENV}=true requires {PUBLIC_BASE_URL_ENV} to be "
                "set to a loopback origin (http://127.0.0.1[:port], "
                "http://localhost[:port], or http://[::1][:port])"
            )
        _ = is_lambda  # Lambda gating of auto-approve is enforced at request time.
