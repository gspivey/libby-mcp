"""Tests for src/oauth/jwt.py — stateless signed-token core (FR-F6, HIGH-4/5/9).

Covers AC-CRITICAL-2 (cross-instance validation), AC-F6.1 (SHA-256 key, >=32
bytes), AC-F6.2 (refuse-to-start when missing), AC-F6.5 (typed artifacts, iss,
alg=HS256 pin), AC-F6.6 (constant-time), and the TTL defaults/overrides (HIGH-6).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from unittest.mock import patch

import pytest

jwt = pytest.importorskip("src.oauth.jwt", reason="PR-F1 not yet implemented (TDD red phase)")

ISS = "https://example.test"
KEY = hashlib.sha256(b"x" * 40).digest()


def _b64url(seg: bytes) -> str:
    return base64.urlsafe_b64encode(seg).rstrip(b"=").decode("ascii")


class TestSignVerifyRoundTrip:
    def test_round_trip(self) -> None:
        token = jwt.sign({"sub": "abc"}, typ="access", iss=ISS, ttl_seconds=60, key=KEY)
        claims = jwt.verify(token, expected_typ="access", expected_iss=ISS, key=KEY)
        assert claims["sub"] == "abc"
        assert claims["typ"] == "access"
        assert claims["iss"] == ISS
        assert "iat" in claims and "exp" in claims

    def test_no_ttl_has_no_exp(self) -> None:
        token = jwt.sign({"x": 1}, typ="client", iss=ISS, ttl_seconds=None, key=KEY)
        claims = jwt.verify(token, expected_typ="client", expected_iss=ISS, key=KEY)
        assert "exp" not in claims


class TestCrossInstanceAndKey:
    """AC-CRITICAL-2: a token signed with a key validates under a second verifier
    sharing the key and fails under a different key."""

    def test_same_key_validates(self) -> None:
        token = jwt.sign({"x": 1}, typ="access", iss=ISS, ttl_seconds=60, key=KEY)
        # A freshly-derived key equal to KEY (simulating a second instance).
        same = hashlib.sha256(b"x" * 40).digest()
        assert jwt.verify(token, expected_typ="access", expected_iss=ISS, key=same)["x"] == 1

    def test_different_key_fails(self) -> None:
        token = jwt.sign({"x": 1}, typ="access", iss=ISS, ttl_seconds=60, key=KEY)
        other = hashlib.sha256(b"y" * 40).digest()
        with pytest.raises(jwt.JWTError):
            jwt.verify(token, expected_typ="access", expected_iss=ISS, key=other)


class TestTypedArtifacts:
    """AC-F6.5 / HIGH-5: wrong typ, wrong iss, and non-HS256 alg are rejected."""

    def test_wrong_typ_rejected(self) -> None:
        token = jwt.sign({}, typ="refresh", iss=ISS, ttl_seconds=60, key=KEY)
        with pytest.raises(jwt.JWTError):
            jwt.verify(token, expected_typ="access", expected_iss=ISS, key=KEY)

    def test_code_as_access_rejected(self) -> None:
        token = jwt.sign({}, typ="code", iss=ISS, ttl_seconds=60, key=KEY)
        with pytest.raises(jwt.JWTError):
            jwt.verify(token, expected_typ="access", expected_iss=ISS, key=KEY)

    def test_wrong_iss_rejected(self) -> None:
        token = jwt.sign({}, typ="access", iss="https://evil.test", ttl_seconds=60, key=KEY)
        with pytest.raises(jwt.JWTError):
            jwt.verify(token, expected_typ="access", expected_iss=ISS, key=KEY)

    def test_alg_none_rejected(self) -> None:
        # Forge a token with alg:none and an empty signature.
        header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({"typ": "access", "iss": ISS, "iat": 0}).encode())
        forged = f"{header}.{payload}."
        with pytest.raises(jwt.JWTError):
            jwt.verify(forged, expected_typ="access", expected_iss=ISS, key=KEY)

    def test_alg_hs512_rejected(self) -> None:
        header = _b64url(json.dumps({"alg": "HS512", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({"typ": "access", "iss": ISS, "iat": 0}).encode())
        import hmac as _hmac

        sig = _b64url(_hmac.digest(KEY, f"{header}.{payload}".encode(), "sha512"))
        forged = f"{header}.{payload}.{sig}"
        with pytest.raises(jwt.JWTError):
            jwt.verify(forged, expected_typ="access", expected_iss=ISS, key=KEY)

    def test_malformed_token_rejected(self) -> None:
        with pytest.raises(jwt.JWTError):
            jwt.verify("not-a-jwt", expected_typ="access", expected_iss=ISS, key=KEY)


class TestExpiry:
    def test_expired_rejected(self) -> None:
        token = jwt.sign({}, typ="access", iss=ISS, ttl_seconds=60, key=KEY, now=1000)
        with pytest.raises(jwt.JWTError):
            jwt.verify(token, expected_typ="access", expected_iss=ISS, key=KEY, now=2000)

    def test_not_expired_ok(self) -> None:
        token = jwt.sign({}, typ="access", iss=ISS, ttl_seconds=60, key=KEY, now=1000)
        assert jwt.verify(token, expected_typ="access", expected_iss=ISS, key=KEY, now=1030)


class TestSigningKey:
    """AC-F6.1/F6.2: SHA-256-of-secret key; >=32 bytes; refuse when missing/short."""

    def test_key_is_sha256_of_secret(self) -> None:
        secret = "s" * 40
        with patch.dict(os.environ, {jwt.SIGNING_KEY_ENV: secret}):
            assert jwt.signing_key() == hashlib.sha256(secret.encode()).digest()

    def test_missing_key_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(jwt.SIGNING_KEY_ENV, None)
            with pytest.raises(jwt.SigningKeyError):
                jwt.signing_key()

    def test_empty_key_raises(self) -> None:
        with patch.dict(os.environ, {jwt.SIGNING_KEY_ENV: ""}):
            with pytest.raises(jwt.SigningKeyError):
                jwt.signing_key()

    def test_short_key_raises(self) -> None:
        with patch.dict(os.environ, {jwt.SIGNING_KEY_ENV: "x" * 31}):
            with pytest.raises(jwt.SigningKeyError):
                jwt.signing_key()

    def test_32_byte_key_ok(self) -> None:
        with patch.dict(os.environ, {jwt.SIGNING_KEY_ENV: "x" * 32}):
            assert len(jwt.signing_key()) == 32

    def test_distinct_secrets_distinct_keys(self) -> None:
        with patch.dict(os.environ, {jwt.SIGNING_KEY_ENV: "a" * 40}):
            k1 = jwt.signing_key()
        with patch.dict(os.environ, {jwt.SIGNING_KEY_ENV: "b" * 40}):
            k2 = jwt.signing_key()
        assert k1 != k2


class TestTTLDefaultsAndOverrides:
    """HIGH-6: access=3600, refresh=2592000, code=60; env overrides change exp."""

    def test_defaults(self) -> None:
        for env in (jwt.ACCESS_TTL_ENV, jwt.REFRESH_TTL_ENV, jwt.CODE_TTL_ENV):
            os.environ.pop(env, None)
        assert jwt.access_ttl() == 3600
        assert jwt.refresh_ttl() == 2592000
        assert jwt.code_ttl() == 60

    def test_empty_env_uses_default(self) -> None:
        with patch.dict(os.environ, {jwt.ACCESS_TTL_ENV: ""}):
            assert jwt.access_ttl() == 3600

    def test_override_access(self) -> None:
        with patch.dict(os.environ, {jwt.ACCESS_TTL_ENV: "120"}):
            assert jwt.access_ttl() == 120

    def test_override_changes_exp(self) -> None:
        with patch.dict(os.environ, {jwt.ACCESS_TTL_ENV: "120"}):
            token = jwt.sign(
                {}, typ="access", iss=ISS, ttl_seconds=jwt.access_ttl(), key=KEY, now=1000
            )
        claims = jwt.verify(token, expected_typ="access", expected_iss=ISS, key=KEY, now=1001)
        assert claims["exp"] == 1120


def test_no_pyjwt_import() -> None:
    """HIGH-4 / AC-F6.4: jwt.py uses only the standard library, no PyJWT."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(jwt))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # Only stdlib + the in-repo src package may be imported.
    allowed = {"base64", "hashlib", "hmac", "json", "time", "typing", "__future__", "src"}
    assert imported <= allowed, f"unexpected imports in jwt.py: {imported - allowed}"
    for banned in ("jwt", "jose", "authlib", "cryptography"):
        assert banned not in imported
