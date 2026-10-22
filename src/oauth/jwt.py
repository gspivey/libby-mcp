"""Stateless signed-token core for the embedded OAuth AS (FR-F6, HIGH-4/5/9).

Every OAuth artifact — client IDs, authorization codes, access tokens, refresh
tokens — is a self-contained HS256 JWT signed with a key derived from
``LIBBY_MCP_OAUTH_SIGNING_KEY``. Because the key is shared, any instance validates
a token minted by any other instance with no server-side store (AC-CRITICAL-2).

Standard-library only: ``hmac``/``hashlib``/``base64``/``json`` — no PyJWT or other
third-party JWT package (HIGH-4 / AC-F6.4), so the Lambda path adds zero new
dependencies.

Security properties enforced here:

- **Pinned algorithm.** The JWT header ``alg`` MUST be ``HS256``; a token with
  ``alg: none`` or any other alg is rejected *before* the HMAC is computed
  (defeats alg-confusion / ``alg:none``) — AC-F6.5.
- **Typed artifacts.** Every token carries a ``typ`` claim (``client``/``code``/
  ``access``/``refresh``) and an ``iss`` claim; each verifier is called with the
  exact ``typ`` it expects and rejects a mismatch, so a refresh token or a code
  presented as a ``/mcp`` Bearer is rejected and an access token replayed at
  ``/token`` is rejected (AC-F6.5 / HIGH-5).
- **Constant-time comparison.** The signature check uses ``hmac.compare_digest``
  (AC-F6.6).
- **Signing key.** The HMAC key is ``SHA-256(raw UTF-8 bytes of the secret)`` — a
  fixed 32-byte key — so any sufficiently-long secret works and there is no
  hex/base64 ambiguity (AC-F6.1 / HIGH-9). The raw secret MUST be at least 32
  bytes; a shorter secret is a startup error (AC-F6.1).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from src.auth import _env

# Env var names (read through src.auth._env so unset == empty-string, HIGH-2).
SIGNING_KEY_ENV = "LIBBY_MCP_OAUTH_SIGNING_KEY"
ACCESS_TTL_ENV = "LIBBY_MCP_OAUTH_ACCESS_TTL_SECONDS"
REFRESH_TTL_ENV = "LIBBY_MCP_OAUTH_REFRESH_TTL_SECONDS"
CODE_TTL_ENV = "LIBBY_MCP_OAUTH_CODE_TTL_SECONDS"

# TTL defaults (seconds) — AC-F2.9/F2.10 (HIGH-6).
DEFAULT_ACCESS_TTL = 3600
DEFAULT_REFRESH_TTL = 2592000  # 30 days
DEFAULT_CODE_TTL = 60

# Minimum signing-secret length in UTF-8 bytes (AC-F6.1).
MIN_SIGNING_KEY_BYTES = 32

# Allowed typed artifacts (AC-F6.5).
ARTIFACT_TYPES = ("client", "code", "access", "refresh")

_HEADER = {"alg": "HS256", "typ": "JWT"}


class JWTError(Exception):
    """Raised when a token fails verification (bad alg/signature/typ/iss/exp)."""


class SigningKeyError(Exception):
    """Raised at startup when the signing key is missing or too short (AC-F6.2)."""


def _b64url_encode(raw: bytes) -> str:
    """URL-safe base64 without padding (JWT segment encoding)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    """Decode a URL-safe base64 JWT segment, restoring stripped padding."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def signing_key() -> bytes:
    """Return the HMAC key derived from ``LIBBY_MCP_OAUTH_SIGNING_KEY`` (AC-F6.1).

    The HMAC key is ``SHA-256(secret_utf8_bytes)`` — a fixed 32-byte key from the
    raw secret, with no hex/base64 decode step (HIGH-9). Raises
    :class:`SigningKeyError` when the secret is unset or shorter than 32 UTF-8
    bytes, so callers (startup) fail fast rather than fall back to a random key
    (AC-F6.2).
    """
    secret = _env(SIGNING_KEY_ENV)
    if secret is None:
        raise SigningKeyError(
            f"{SIGNING_KEY_ENV} is required when OAuth is enabled "
            "(a secret string of at least 32 bytes)"
        )
    raw = secret.encode("utf-8")
    if len(raw) < MIN_SIGNING_KEY_BYTES:
        raise SigningKeyError(
            f"{SIGNING_KEY_ENV} must be at least {MIN_SIGNING_KEY_BYTES} bytes " f"(got {len(raw)})"
        )
    return hashlib.sha256(raw).digest()


def _ttl(env_name: str, default: int) -> int:
    """Read a numeric TTL env var, falling back to ``default`` (HIGH-6).

    An unset/empty value (via ``_env``) or a non-integer value yields the
    documented default; a valid integer string overrides it (so a test can
    assert overriding the env changes the issued ``exp``).
    """
    raw = _env(env_name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def access_ttl() -> int:
    """Access-token lifetime in seconds (default 3600, AC-F2.10)."""
    return _ttl(ACCESS_TTL_ENV, DEFAULT_ACCESS_TTL)


def refresh_ttl() -> int:
    """Refresh-token lifetime in seconds (default 2592000 = 30 days, AC-F2.10)."""
    return _ttl(REFRESH_TTL_ENV, DEFAULT_REFRESH_TTL)


def code_ttl() -> int:
    """Authorization-code lifetime in seconds (default 60, AC-F2.9)."""
    return _ttl(CODE_TTL_ENV, DEFAULT_CODE_TTL)


def sign(
    payload: dict[str, Any],
    *,
    typ: str,
    iss: str,
    ttl_seconds: int | None = None,
    key: bytes | None = None,
    now: int | None = None,
) -> str:
    """Sign a payload into an HS256 JWT carrying ``typ``, ``iss``, and ``iat``.

    Args:
        payload: Claims to embed (``typ``/``iss``/``iat``/``exp`` are set here and
            override any same-named keys in ``payload``).
        typ: Artifact type — one of :data:`ARTIFACT_TYPES`.
        iss: Issuer (the resolved public base URL).
        ttl_seconds: When provided, an ``exp`` claim is added ``ttl_seconds`` in
            the future. A client-id token has no expiry, so it is signed with
            ``ttl_seconds=None``.
        key: HMAC key (defaults to :func:`signing_key`); passed explicitly by
            tests simulating a second instance / a wrong key.
        now: Current epoch seconds (defaults to ``time.time()``; injectable for
            deterministic tests).

    Returns:
        The compact ``header.payload.signature`` JWT string.
    """
    if typ not in ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact typ: {typ!r}")
    if key is None:
        key = signing_key()
    issued_at = int(time.time()) if now is None else now

    claims = dict(payload)
    claims["typ"] = typ
    claims["iss"] = iss
    claims["iat"] = issued_at
    if ttl_seconds is not None:
        claims["exp"] = issued_at + ttl_seconds

    header_seg = _b64url_encode(json.dumps(_HEADER, separators=(",", ":")).encode("utf-8"))
    payload_seg = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    sig = hmac.digest(key, signing_input, "sha256")
    return f"{header_seg}.{payload_seg}.{_b64url_encode(sig)}"


def verify(
    token: str,
    *,
    expected_typ: str,
    expected_iss: str,
    key: bytes | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify an HS256 JWT and return its claims, or raise :class:`JWTError`.

    Enforces, in order (AC-F6.5):

    1. three-segment structure;
    2. header ``alg == "HS256"`` (``none``/other rejected before the HMAC);
    3. signature via ``hmac.compare_digest`` (AC-F6.6);
    4. ``typ == expected_typ`` (so a swapped artifact is rejected, HIGH-5);
    5. ``iss == expected_iss``;
    6. ``exp`` not in the past (when present).
    """
    if key is None:
        key = signing_key()
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("malformed token: expected three segments")
    header_seg, payload_seg, sig_seg = parts

    # (2) alg pin — reject alg:none / any non-HS256 before computing the HMAC.
    try:
        header = json.loads(_b64url_decode(header_seg))
    except (ValueError, json.JSONDecodeError) as e:
        raise JWTError("malformed token header") from e
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise JWTError("unsupported or missing alg (only HS256 is accepted)")

    # (3) signature — constant-time compare.
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    expected_sig = hmac.digest(key, signing_input, "sha256")
    try:
        presented_sig = _b64url_decode(sig_seg)
    except ValueError as e:
        raise JWTError("malformed token signature") from e
    if not hmac.compare_digest(expected_sig, presented_sig):
        raise JWTError("bad signature")

    try:
        claims = json.loads(_b64url_decode(payload_seg))
    except (ValueError, json.JSONDecodeError) as e:
        raise JWTError("malformed token payload") from e
    if not isinstance(claims, dict):
        raise JWTError("malformed token payload")

    # (4) typ — the verifier is called with the exact typ it expects.
    if claims.get("typ") != expected_typ:
        raise JWTError(f"wrong typ: expected {expected_typ!r}, got {claims.get('typ')!r}")

    # (5) iss.
    if claims.get("iss") != expected_iss:
        raise JWTError("issuer mismatch")

    # (6) exp (when present).
    exp = claims.get("exp")
    if exp is not None:
        current = int(time.time()) if now is None else now
        if current >= int(exp):
            raise JWTError("token expired")

    return claims
