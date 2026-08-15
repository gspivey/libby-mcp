"""Tests for search_titles tool — catalog search with filtering.

Covers: parameter mapping, format mapping, availability filter, BISAC filtering,
duration filtering, response normalization, pagination metadata, facets,
validation errors, invalid slug detection.
"""

from unittest.mock import MagicMock

import pytest

search_titles = pytest.importorskip(
    "src.tools.search_titles", reason="Implementation not yet written (TDD red phase)"
)


@pytest.fixture
def thunder_client(sample_search_response: dict) -> MagicMock:
    """A mock ThunderClient that returns the sample search response."""
    client = MagicMock()
    client.search.return_value = sample_search_response
    return client


class TestParameterMapping:
    """Test that MCP params are correctly mapped to Thunder API params."""

    def test_subjects_mapped_to_repeated_subject(self, thunder_client: MagicMock) -> None:
        """AC-1.2: subjects=[24, 80] maps to subject=["24", "80"] (coerced to strings)."""
        search_titles.handle({"library_slug": "lcpl", "subjects": [24, 80]}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert call_params["subject"] == ["24", "80"], (
            "REMEDIATION: search_titles must map subjects array to 'subject' param "
            "with values coerced to strings"
        )

    def test_subjects_accepts_strings(self, thunder_client: MagicMock) -> None:
        """AC-1.2: subjects accepts strings or integers, coerced to strings."""
        search_titles.handle({"library_slug": "lcpl", "subjects": ["24", "80"]}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert call_params["subject"] == ["24", "80"]


class TestFormatMapping:
    """Test format value mapping."""

    def test_audiobook_maps_to_overdrive(self, thunder_client: MagicMock) -> None:
        """AC-1.3: 'audiobook' maps to 'audiobook-overdrive'."""
        search_titles.handle({"library_slug": "lcpl", "format": "audiobook"}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert (
            call_params["format"] == "audiobook-overdrive"
        ), "REMEDIATION: search_titles must map 'audiobook' to 'audiobook-overdrive'"

    def test_ebook_maps_to_overdrive(self, thunder_client: MagicMock) -> None:
        """AC-1.3: 'ebook' maps to 'ebook-overdrive'."""
        search_titles.handle({"library_slug": "lcpl", "format": "ebook"}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert (
            call_params["format"] == "ebook-overdrive"
        ), "REMEDIATION: search_titles must map 'ebook' to 'ebook-overdrive'"


class TestAvailabilityFilter:
    """Test available parameter mapping."""

    def test_available_true_maps_to_show_only(self, thunder_client: MagicMock) -> None:
        """AC-1.4: available=true passes showOnlyAvailable=true."""
        search_titles.handle({"library_slug": "lcpl", "available": True}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert (
            call_params.get("showOnlyAvailable") == "true"
        ), "REMEDIATION: available=true must map to showOnlyAvailable=true"

    def test_available_omitted_maps_to_available_first(self, thunder_client: MagicMock) -> None:
        """AC-1.16: When available omitted, pass availableFirst=true."""
        search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert (
            call_params.get("availableFirst") == "true"
        ), "REMEDIATION: When available is omitted, must pass availableFirst=true"


class TestBISACFiltering:
    """Test belt-and-suspenders BISAC filtering."""

    def test_bisac_sent_server_side(self, thunder_client: MagicMock) -> None:
        """AC-1.6: bisac is sent to Thunder API as bisacCode param."""
        search_titles.handle({"library_slug": "lcpl", "bisac": "FIC129000"}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert (
            call_params.get("bisacCode") == "FIC129000"
        ), "REMEDIATION: search_titles must send bisac as bisacCode to Thunder API"

    def test_bisac_filtered_client_side(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.6: Client-side filter keeps only items with matching bisacCodes."""
        thunder_client.search.return_value = sample_search_response
        # FIC028000 is only on item 3 (Project Hail Mary)
        result = search_titles.handle(
            {"library_slug": "lcpl", "bisac": "FIC028000"}, thunder_client
        )
        titles = result["titles"]
        for title in titles:
            assert (
                "FIC028000" in title["bisac_codes"]
            ), "REMEDIATION: Client-side BISAC filter must keep only items with matching code"


class TestDurationFiltering:
    """Test client-side duration filtering (under_hours)."""

    def test_under_hours_filters_long_audiobooks(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.8: Items with duration > under_hours are excluded."""
        thunder_client.search.return_value = sample_search_response
        # Item 2 ("Name of the Wind") has duration 27:55:12 (~28h)
        result = search_titles.handle({"library_slug": "lcpl", "under_hours": 15}, thunder_client)
        titles = result["titles"]
        title_ids = [t["id"] for t in titles]
        assert (
            "1234567" not in title_ids
        ), "REMEDIATION: under_hours must exclude items with duration > threshold"
        # Item 1 ("Dungeon Crawler Carl") has 12:34:56 (~12.6h) — should pass
        assert "9876543" in title_ids

    def test_under_hours_includes_items_without_duration(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.8: Items without a duration (e.g., ebooks) are INCLUDED."""
        thunder_client.search.return_value = sample_search_response
        # Item 3 has empty duration string — should be included
        result = search_titles.handle({"library_slug": "lcpl", "under_hours": 15}, thunder_client)
        titles = result["titles"]
        title_ids = [t["id"] for t in titles]
        assert "5555555" in title_ids, (
            "REMEDIATION: Items with no duration (ebooks) must be INCLUDED "
            "when under_hours filter is active"
        )


class TestResponseNormalization:
    """Test that items are normalized into the Title Object schema."""

    def test_title_object_schema(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.14: Response items contain required fields."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        title = result["titles"][0]
        assert "id" in title and isinstance(
            title["id"], str
        ), "REMEDIATION: Title id must be a string"
        assert "title" in title
        assert "creator" in title
        assert "format" in title
        assert "available" in title and isinstance(title["available"], bool)
        assert "available_copies" in title
        assert "owned_copies" in title
        assert "holds_count" in title
        assert "bisac_codes" in title
        assert "link" in title

    def test_availability_computation(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.17: available = isAvailable AND availableCopies >= 1."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        # Item 1: isAvailable=true, availableCopies=2 -> available=true
        assert result["titles"][0]["available"] is True
        # Item 2: isAvailable=false, availableCopies=0 -> available=false
        assert result["titles"][1]["available"] is False

    def test_deep_link_format(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.14: link follows format https://{slug}.overdrive.com/media/{id}."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        title = result["titles"][0]
        assert (
            title["link"] == "https://lcpl.overdrive.com/media/9876543"
        ), "REMEDIATION: Title link must be https://{slug}.overdrive.com/media/{id}"


class TestPaginationMetadata:
    """Test pagination fields in response."""

    def test_total_items_from_server(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.15: total_items from server response (before client-side filtering)."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert (
            result["total_items"] == 843
        ), "REMEDIATION: total_items must come from Thunder API totalItems"
        assert result["total_pages"] == 9, "REMEDIATION: total_pages must come from links.last.page"
        assert result["page"] == 1

    def test_filtered_count_after_client_filter(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.15: filtered_count reflects post-client-filter count."""
        thunder_client.search.return_value = sample_search_response
        # With BISAC filter FIC028000 (only item 3 matches)
        result = search_titles.handle(
            {"library_slug": "lcpl", "bisac": "FIC028000"}, thunder_client
        )
        assert (
            result["filtered_count"] == 1
        ), "REMEDIATION: filtered_count must reflect items remaining after client-side filtering"


class TestFacets:
    """Test that facets are included in the response."""

    def test_facets_included(self, thunder_client: MagicMock, sample_search_response: dict) -> None:
        """AC-1.18: Response includes facets with subjects."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert "facets" in result, "REMEDIATION: search_titles response must include facets"
        assert "subjects" in result["facets"]


class TestValidation:
    """Test input validation errors."""

    def test_missing_library_slug_returns_error(self, thunder_client: MagicMock) -> None:
        """Validation: library_slug is required."""
        result = search_titles.handle({}, thunder_client)
        assert (
            "error" in result
        ), "REMEDIATION: search_titles must return error when library_slug is missing"


class TestInvalidSlugDetection:
    """Test best-effort invalid slug detection."""

    def test_empty_results_no_filters_returns_error(self, thunder_client: MagicMock) -> None:
        """AC-4.3: Empty results with no narrowing filters → library not found error."""
        thunder_client.search.return_value = {
            "items": [],
            "totalItems": 0,
            "links": {"self": {"page": 1}, "last": {"page": 0}},
            "facets": {},
        }
        result = search_titles.handle({"library_slug": "invalid-slug"}, thunder_client)
        assert (
            "error" in result
        ), "REMEDIATION: Empty results with totalItems=0 and no filters must return error"
        assert "not found" in result["error"].lower() or "unavailable" in result["error"].lower()

    def test_empty_results_with_filters_is_valid(self, thunder_client: MagicMock) -> None:
        """AC-4.3: Empty results WITH narrowing filters is valid (not an error)."""
        thunder_client.search.return_value = {
            "items": [],
            "totalItems": 0,
            "links": {"self": {"page": 1}, "last": {"page": 0}},
            "facets": {},
        }
        # With creator filter, empty is a valid search outcome
        result = search_titles.handle(
            {"library_slug": "lcpl", "creator": "Nonexistent Author"}, thunder_client
        )
        assert (
            "error" not in result
        ), "REMEDIATION: Empty results with narrowing filters must NOT be treated as error"
