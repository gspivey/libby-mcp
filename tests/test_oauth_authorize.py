"""Tests for src/oauth/authorize.py — GET/POST /authorize (FR-F1, FR-F2, FR-F9, FR-F11).

Covers PKCE S256 enforcement (AC-F2.4), the owner-consent gate (AC-F1.6 /
CRITICAL-3), dev auto-approve gating (AC-F1.9), redirect-URI exact-match
(AC-F9.1), ``invalid_target`` on a non-canonical ``resource`` (AC-F11.3), and the
consent-page XSS escaping + security headers (AC-F2.11 / F2.12 / HIGH-C).
"""

from __future__ import annotations

import base64
import hashlib

import pytest

authorize = pytest.importorskip(
    "src.oauth.authorize", reason="PR-F2 not yet implemented (TDD red phase)"
)
registration = pytest.importorskip("src.oauth.registration")
jwt = pytest.importorskip("src.oauth.jwt")

BASE = "https://mcp.example.test"
CANONICAL = f"{BASE}/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "a" * 64


def _challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("LIBBY_MCP_OAUTH_SIGNING_KEY", "s" * 40)
    monkeypatch.setenv("LIBBY_MCP_OAUTH_OWNER_SECRET", "owner-secret")
    monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", BASE)
    monkeypatch.delenv("LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_RUNTIME_API", raising=False)


def _client_id(redirect_uris=None, client_name="Test Client") -> str:
    return registration.mint_client_id(redirect_uris or [REDIRECT], client_name, BASE)


def _params(**overrides) -> dict[str, str]:
    params = {
        "client_id": _client_id(),
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": _challenge(),
        "code_challenge_method": "S256",
        "state": "xyz",
    }
    params.update(overrides)
    return params


def _header_value(headers, name):
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


class TestConsentPagePKCE:
    def test_get_renders_consent_page(self) -> None:
        status, body, headers = authorize.authorize_page(_params(), BASE)
        assert status == 200
        assert "Owner secret" in body
        assert 'name="owner_secret"' in body

    def test_missing_pkce_400(self) -> None:
        p = _params()
        del p["code_challenge"]
        status, body, _ = authorize.authorize_page(p, BASE)
        assert status == 400

    def test_non_s256_pkce_400(self) -> None:
        status, _, _ = authorize.authorize_page(_params(code_challenge_method="plain"), BASE)
        assert status == 400


class TestRedirectUriExactMatch:
    def test_unregistered_redirect_400_not_redirect(self) -> None:
        """AC-F9.1: an unregistered redirect_uri → HTTP 400 error page, not a redirect."""
        status, body, headers = authorize.authorize_page(
            _params(redirect_uri="https://claude.ai/evil"), BASE
        )
        assert status == 400
        assert _header_value(headers, "location") is None

    def test_missing_redirect_400(self) -> None:
        p = _params()
        del p["redirect_uri"]
        status, _, _ = authorize.authorize_page(p, BASE)
        assert status == 400

    def test_bad_client_id_400(self) -> None:
        status, _, _ = authorize.authorize_page(_params(client_id="not.a.jwt"), BASE)
        assert status == 400


class TestResourceBinding:
    def test_canonical_resource_accepted(self) -> None:
        status, _, _ = authorize.authorize_page(_params(resource=CANONICAL), BASE)
        assert status == 200

    def test_trailing_slash_resource_accepted(self) -> None:
        status, _, _ = authorize.authorize_page(_params(resource=CANONICAL + "/"), BASE)
        assert status == 200

    def test_non_canonical_resource_invalid_target(self) -> None:
        """AC-F11.3: a non-canonical resource → invalid_target (400), before any code."""
        status, body, _ = authorize.authorize_page(_params(resource="https://evil.test/mcp"), BASE)
        assert status == 400
        assert "invalid_target" in body


class TestOwnerGate:
    def test_missing_owner_secret_401_no_code(self) -> None:
        """AC-F1.6 / CRITICAL-3: POST with no owner secret → 401 before any code."""
        status, body, headers = authorize.authorize_submit(_params(), BASE, is_lambda=False)
        assert status == 401
        assert _header_value(headers, "location") is None

    def test_wrong_owner_secret_401(self) -> None:
        status, _, _ = authorize.authorize_submit(
            _params(owner_secret="nope"), BASE, is_lambda=False
        )
        assert status == 401

    def test_correct_owner_secret_mints_code_and_redirects(self) -> None:
        """AC-F1.6: correct owner secret → 302 redirect to the registered URI with a code."""
        status, _, headers = authorize.authorize_submit(
            _params(owner_secret="owner-secret"), BASE, is_lambda=False
        )
        assert status == 302
        loc = _header_value(headers, "location")
        assert loc.startswith(REDIRECT)
        assert "code=" in loc
        assert "state=xyz" in loc

    def test_minted_code_is_signed_typ_code_with_bound_values(self) -> None:
        status, _, headers = authorize.authorize_submit(
            _params(owner_secret="owner-secret"), BASE, is_lambda=False
        )
        loc = _header_value(headers, "location")
        from urllib.parse import parse_qs, urlsplit

        code = parse_qs(urlsplit(loc).query)["code"][0]
        claims = jwt.verify(code, expected_typ="code", expected_iss=BASE)
        assert claims["redirect_uri"] == REDIRECT
        assert claims["resource"] == CANONICAL
        assert claims["code_challenge"] == _challenge()


class TestDevAutoApprove:
    def test_refused_on_lambda(self, monkeypatch) -> None:
        """AC-F1.9: auto-approve refused on Lambda even with the flag + loopback base."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE", "true")
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", "http://127.0.0.1:8000")
        base = "http://127.0.0.1:8000"
        cid = registration.mint_client_id([REDIRECT], "c", base)
        p = _params(client_id=cid)
        # No owner secret provided; auto-approve must be refused on Lambda → 401.
        status, _, _ = authorize.authorize_submit(p, base, is_lambda=True)
        assert status == 401

    def test_refused_non_loopback_base(self, monkeypatch) -> None:
        """AC-F1.9: auto-approve refused when base URL is non-loopback."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE", "true")
        # base URL stays the non-loopback BASE from the fixture.
        status, _, _ = authorize.authorize_submit(_params(), BASE, is_lambda=False)
        assert status == 401

    def test_allowed_loopback_non_lambda(self, monkeypatch) -> None:
        """AC-F1.9: flag + non-Lambda + loopback base → code minted without the secret."""
        monkeypatch.setenv("LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE", "true")
        base = "http://127.0.0.1:8000"
        monkeypatch.setenv("LIBBY_MCP_PUBLIC_BASE_URL", base)
        cid = registration.mint_client_id([REDIRECT], "c", base)
        p = _params(client_id=cid, resource=f"{base}/mcp")
        status, _, headers = authorize.authorize_submit(p, base, is_lambda=False)
        assert status == 302
        assert "code=" in _header_value(headers, "location")


class TestConsentPageSecurity:
    def test_client_name_script_is_escaped(self) -> None:
        """AC-F2.11 / HIGH-C: attacker-controlled client_name is rendered inert."""
        cid = _client_id(client_name="<script>alert(1)</script>")
        status, body, _ = authorize.authorize_page(_params(client_id=cid), BASE)
        assert status == 200
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_state_and_redirect_escaped_in_hidden_inputs(self) -> None:
        status, body, _ = authorize.authorize_page(_params(state='"><script>x</script>'), BASE)
        assert '"><script>x</script>' not in body
        assert "&lt;script&gt;" in body or "&gt;&lt;" in body

    def test_security_headers_present(self) -> None:
        """AC-F2.12: CSP frame-ancestors none + Cache-Control no-store on GET /authorize."""
        _, _, headers = authorize.authorize_page(_params(), BASE)
        assert _header_value(headers, "content-security-policy") == "frame-ancestors 'none'"
        assert _header_value(headers, "cache-control") == "no-store"

    def test_error_page_has_security_headers(self) -> None:
        _, _, headers = authorize.authorize_page(_params(client_id="bad"), BASE)
        assert _header_value(headers, "content-security-policy") == "frame-ancestors 'none'"
        assert _header_value(headers, "cache-control") == "no-store"
