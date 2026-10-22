"""Tests for src/oauth/token.py — POST /token (FR-F2, FR-F5, FR-F6, FR-F11).

Covers the authorization_code exchange + PKCE verify (AC-F2.5), refresh rotation
(AC-F2.6), canonical ``aud`` on issued tokens (AC-F11.5), RFC 6749 error codes
(AC-F5.2), ``invalid_target`` on a non-canonical ``resource`` (AC-F11.3), and the
token-swap rejections (HIGH-5).
"""

from __future__ import annotations

import base64
import hashlib

import pytest

token_mod = pytest.importorskip(
    "src.oauth.token", reason="PR-F2 not yet implemented (TDD red phase)"
)
registration = pytest.importorskip("src.oauth.registration")
jwt = pytest.importorskip("src.oauth.jwt")

BASE = "https://mcp.example.test"
CANONICAL = f"{BASE}/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64


def _challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", "s" * 40)
    monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)


def _client_id() -> str:
    return registration.mint_client_id([REDIRECT], "Test Client", BASE)


def _code(client_id=None, challenge=None, redirect=REDIRECT, resource=CANONICAL, ttl=60) -> str:
    return jwt.sign(
        {
            "code_challenge": challenge or _challenge(),
            "redirect_uri": redirect,
            "client_id": client_id or _client_id(),
            "resource": resource,
        },
        typ="code",
        iss=BASE,
        ttl_seconds=ttl,
    )


class TestAuthorizationCodeGrant:
    def test_valid_exchange_issues_tokens(self) -> None:
        cid = _client_id()
        form = {
            "grant_type": "authorization_code",
            "code": _code(client_id=cid),
            "code_verifier": VERIFIER,
            "client_id": cid,
            "redirect_uri": REDIRECT,
        }
        status, body = token_mod.token(form, BASE)
        assert status == 200
        assert body["token_type"] == "Bearer"
        assert body["access_token"]
        assert body["refresh_token"]

    def test_issued_access_and_refresh_aud_is_canonical(self) -> None:
        """AC-F11.5: both issued tokens carry aud == canonical <base>/mcp."""
        cid = _client_id()
        form = {
            "grant_type": "authorization_code",
            "code": _code(client_id=cid),
            "code_verifier": VERIFIER,
            "client_id": cid,
        }
        _, body = token_mod.token(form, BASE)
        access = jwt.verify(body["access_token"], expected_typ="access", expected_iss=BASE)
        refresh = jwt.verify(body["refresh_token"], expected_typ="refresh", expected_iss=BASE)
        assert access["aud"] == CANONICAL
        assert refresh["aud"] == CANONICAL

    def test_pkce_mismatch_invalid_grant(self) -> None:
        cid = _client_id()
        form = {
            "grant_type": "authorization_code",
            "code": _code(client_id=cid),
            "code_verifier": "wrong" * 13,
            "client_id": cid,
        }
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_grant"

    def test_expired_code_invalid_grant(self) -> None:
        cid = _client_id()
        expired = jwt.sign(
            {
                "code_challenge": _challenge(),
                "redirect_uri": REDIRECT,
                "client_id": cid,
                "resource": CANONICAL,
            },
            typ="code",
            iss=BASE,
            ttl_seconds=-5,
        )
        form = {
            "grant_type": "authorization_code",
            "code": expired,
            "code_verifier": VERIFIER,
            "client_id": cid,
        }
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_grant"

    def test_redirect_uri_mismatch_invalid_grant(self) -> None:
        cid = _client_id()
        form = {
            "grant_type": "authorization_code",
            "code": _code(client_id=cid),
            "code_verifier": VERIFIER,
            "client_id": cid,
            "redirect_uri": "https://claude.ai/other",
        }
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_grant"

    def test_missing_code_invalid_request(self) -> None:
        status, body = token_mod.token(
            {"grant_type": "authorization_code", "code_verifier": VERIFIER}, BASE
        )
        assert status == 400
        assert body["error"] == "invalid_request"


class TestRefreshTokenGrant:
    def _refresh(self, client_id=None, resource=CANONICAL, ttl=3600) -> str:
        return jwt.sign(
            {"aud": resource, "client_id": client_id or _client_id()},
            typ="refresh",
            iss=BASE,
            ttl_seconds=ttl,
        )

    def test_refresh_rotates_and_issues_new_tokens(self) -> None:
        """AC-F2.6: refresh_token grant issues a new access token + a rotated refresh."""
        original = self._refresh()
        form = {"grant_type": "refresh_token", "refresh_token": original}
        status, body = token_mod.token(form, BASE)
        assert status == 200
        assert body["access_token"]
        assert body["refresh_token"]
        # Rotation: the new refresh token differs from the one presented.
        assert body["refresh_token"] != original

    def test_expired_refresh_invalid_grant(self) -> None:
        """AC-F5.2: an expired refresh token → invalid_grant."""
        form = {"grant_type": "refresh_token", "refresh_token": self._refresh(ttl=-5)}
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_grant"

    def test_unknown_refresh_invalid_grant(self) -> None:
        form = {"grant_type": "refresh_token", "refresh_token": "not.a.jwt"}
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_grant"


class TestTokenSwapRejection:
    """HIGH-5: an artifact presented to the wrong grant is rejected (typed verify)."""

    def test_access_token_replayed_as_refresh_rejected(self) -> None:
        access = jwt.sign({"aud": CANONICAL}, typ="access", iss=BASE, ttl_seconds=60)
        form = {"grant_type": "refresh_token", "refresh_token": access}
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_grant"

    def test_refresh_token_replayed_as_code_rejected(self) -> None:
        refresh = jwt.sign({"aud": CANONICAL}, typ="refresh", iss=BASE, ttl_seconds=60)
        form = {
            "grant_type": "authorization_code",
            "code": refresh,
            "code_verifier": VERIFIER,
        }
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_grant"

    def test_alg_none_rejected(self) -> None:
        import json

        header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
            .rstrip(b"=")
            .decode()
        )
        payload = (
            base64.urlsafe_b64encode(
                json.dumps({"typ": "refresh", "iss": BASE, "aud": CANONICAL}).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        forged = f"{header}.{payload}."
        form = {"grant_type": "refresh_token", "refresh_token": forged}
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_grant"


class TestResourceValidation:
    def test_non_canonical_resource_on_token_invalid_target(self) -> None:
        """AC-F11.3: a non-canonical resource on /token → invalid_target."""
        cid = _client_id()
        form = {
            "grant_type": "authorization_code",
            "code": _code(client_id=cid),
            "code_verifier": VERIFIER,
            "client_id": cid,
            "resource": "https://evil.test/mcp",
        }
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_target"

    def test_missing_resource_defaults_canonical(self) -> None:
        """AC-F11.4: a missing resource defaults to canonical (not an error)."""
        cid = _client_id()
        form = {
            "grant_type": "authorization_code",
            "code": _code(client_id=cid),
            "code_verifier": VERIFIER,
            "client_id": cid,
        }
        status, _ = token_mod.token(form, BASE)
        assert status == 200


class TestGrantTypeErrors:
    def test_missing_grant_type_invalid_request(self) -> None:
        status, body = token_mod.token({}, BASE)
        assert status == 400
        assert body["error"] == "invalid_request"

    def test_unsupported_grant_type(self) -> None:
        status, body = token_mod.token({"grant_type": "client_credentials"}, BASE)
        assert status == 400
        assert body["error"] == "unsupported_grant_type"

    def test_bad_client_id_invalid_client(self) -> None:
        form = {
            "grant_type": "authorization_code",
            "code": _code(client_id="not.a.jwt"),
            "code_verifier": VERIFIER,
            "client_id": "not.a.jwt",
        }
        status, body = token_mod.token(form, BASE)
        assert status == 400
        assert body["error"] == "invalid_client"
