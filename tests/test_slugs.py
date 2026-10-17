"""Tests for src/tools/_slugs.py — library slug normalization (FR-D2)."""

from __future__ import annotations

import pytest

_slugs = pytest.importorskip(
    "src.tools._slugs", reason="Implementation not yet written (TDD red phase)"
)


class TestNormalizeSlug:
    """AC-D2.1–D2.3: normalize_slug lowercases and trims."""

    def test_lowercases(self) -> None:
        assert _slugs.normalize_slug("LCPL") == "lcpl"

    def test_strips_whitespace(self) -> None:
        assert _slugs.normalize_slug(" lcpl ") == "lcpl"

    def test_lowercase_and_strip_combined(self) -> None:
        assert _slugs.normalize_slug("  FairFax \t") == "fairfax"

    def test_already_normal_unchanged(self) -> None:
        assert _slugs.normalize_slug("nypl") == "nypl"

    def test_empty_after_strip(self) -> None:
        assert _slugs.normalize_slug("   ") == ""

    def test_no_per_slug_special_casing(self) -> None:
        """NFR-6.1: purely mechanical — no hardcoded library branches."""
        for raw, expected in [
            ("FCPL", "fcpl"),
            ("Some-Other-Lib", "some-other-lib"),
            (" MixedCase ", "mixedcase"),
        ]:
            assert _slugs.normalize_slug(raw) == expected
