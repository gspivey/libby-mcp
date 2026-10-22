"""Tests for src/oauth/metadata.py — canonical resource + discovery docs (FR-F2, FR-F11)."""

from __future__ import annotations

import pytest

metadata = pytest.importorskip(
    "src.oauth.metadata", reason="PR-F1 not yet implemented (TDD red phase)"
)

BASE = "https://mcp.example.test"


class TestCanonicalResource:
    """AC-F11.1/F11.2: canonical resource is <base>/mcp; trailing slash normalized."""

    def test_canonical(self) -> None:
        assert metadata.canonical_resource(BASE) == f"{BASE}/mcp"

    def test_canonical_strips_base_trailing_slash(self) -> None:
        assert metadata.canonical_resource(BASE + "/") == f"{BASE}/mcp"

    def test_normalize_trailing_slash_equal(self) -> None:
        assert metadata.normalize_resource(f"{BASE}/mcp/") == metadata.normalize_resource(
            f"{BASE}/mcp"
        )


class TestResolveResource:
    """AC-F11.3/F11.4: missing defaults to canonical; non-canonical → invalid_target."""

    def test_missing_defaults_to_canonical(self) -> None:
        assert metadata.resolve_resource(None, BASE) == f"{BASE}/mcp"

    def test_empty_defaults_to_canonical(self) -> None:
        assert metadata.resolve_resource("   ", BASE) == f"{BASE}/mcp"

    def test_canonical_accepted(self) -> None:
        assert metadata.resolve_resource(f"{BASE}/mcp", BASE) == f"{BASE}/mcp"

    def test_canonical_trailing_slash_accepted(self) -> None:
        assert metadata.resolve_resource(f"{BASE}/mcp/", BASE) == f"{BASE}/mcp"

    def test_non_canonical_rejected(self) -> None:
        with pytest.raises(metadata.OAuthError) as exc:
            metadata.resolve_resource("https://evil.test/mcp", BASE)
        assert exc.value.error == "invalid_target"

    def test_different_path_rejected(self) -> None:
        with pytest.raises(metadata.OAuthError) as exc:
            metadata.resolve_resource(f"{BASE}/other", BASE)
        assert exc.value.error == "invalid_target"


class TestProtectedResourceDocument:
    """AC-F2.1 / F3.3 / F11.1."""

    def test_prm_shape(self) -> None:
        doc = metadata.protected_resource_document(BASE)
        assert doc["resource"] == f"{BASE}/mcp"
        assert doc["authorization_servers"] == [BASE]
        assert doc["bearer_methods_supported"] == ["header"]

    def test_prm_as_consistency(self) -> None:
        """AC-F3.3: PRM authorization_servers[0] == AS issuer."""
        prm = metadata.protected_resource_document(BASE)
        asm = metadata.authorization_server_document(BASE)
        assert prm["authorization_servers"][0] == asm["issuer"]


class TestAuthorizationServerDocument:
    """AC-F2.2: S256 only; grants exclude client_credentials; public clients."""

    def test_as_metadata_shape(self) -> None:
        doc = metadata.authorization_server_document(BASE)
        assert doc["issuer"] == BASE
        assert doc["authorization_endpoint"] == f"{BASE}/authorize"
        assert doc["token_endpoint"] == f"{BASE}/token"
        assert doc["registration_endpoint"] == f"{BASE}/register"
        assert doc["code_challenge_methods_supported"] == ["S256"]
        assert doc["grant_types_supported"] == ["authorization_code", "refresh_token"]
        assert "client_credentials" not in doc["grant_types_supported"]
        assert doc["response_types_supported"] == ["code"]
        assert doc["token_endpoint_auth_methods_supported"] == ["none"]
