"""Tests for get_deep_link tool — URL construction.

Covers: URL format, required params validation, title_id coercion.
"""

import pytest

deep_link = pytest.importorskip(
    "src.tools.deep_link", reason="Implementation not yet written (TDD red phase)"
)


class TestURLFormat:
    """Test deep link URL construction."""

    def test_correct_url_format(self) -> None:
        """AC-3.3: URL format is https://{slug}.overdrive.com/media/{id}."""
        result = deep_link.handle({"title_id": "12345", "library_slug": "lcpl"})
        assert result["url"] == "https://lcpl.overdrive.com/media/12345", (
            "REMEDIATION: get_deep_link must return "
            "https://{library_slug}.overdrive.com/media/{title_id}"
        )

    def test_returns_title_id_and_slug(self) -> None:
        """Response includes title_id and library_slug."""
        result = deep_link.handle({"title_id": "67890", "library_slug": "nypl"})
        assert result["title_id"] == "67890"
        assert result["library_slug"] == "nypl"

    def test_integer_title_id_coerced(self) -> None:
        """Title ID accepts integer and coerces to string."""
        result = deep_link.handle({"title_id": 12345, "library_slug": "lcpl"})
        assert result["url"] == "https://lcpl.overdrive.com/media/12345"
        assert result["title_id"] == "12345"


class TestRequiredParams:
    """Test that both params are required."""

    def test_missing_title_id_returns_error(self) -> None:
        """AC-3.4: Missing title_id returns clear validation error."""
        result = deep_link.handle({"library_slug": "lcpl"})
        assert (
            "error" in result
        ), "REMEDIATION: get_deep_link must return error when title_id is missing"

    def test_missing_library_slug_returns_error(self) -> None:
        """AC-3.4: Missing library_slug returns clear validation error."""
        result = deep_link.handle({"title_id": "12345"})
        assert (
            "error" in result
        ), "REMEDIATION: get_deep_link must return error when library_slug is missing"

    def test_empty_params_returns_error(self) -> None:
        """AC-3.4: Empty params returns clear validation error."""
        result = deep_link.handle({})
        assert "error" in result
