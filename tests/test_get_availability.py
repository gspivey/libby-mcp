"""Tests for get_availability tool — raw availability data retrieval.

Covers: required params validation, title_ids coercion, endpoint verification,
response mapping, not-found handling, no recommendations.
"""

from unittest.mock import MagicMock

import pytest

availability = pytest.importorskip(
    "src.tools.availability", reason="Implementation not yet written (TDD red phase)"
)


@pytest.fixture
def thunder_client(sample_availability_response: dict) -> MagicMock:
    """A mock ThunderClient returning sample availability data."""
    client = MagicMock()
    client.get_availability.return_value = sample_availability_response
    return client


class TestRequiredParams:
    """Test that required parameters are validated."""

    def test_missing_library_slug_returns_error(self, thunder_client: MagicMock) -> None:
        """library_slug is required."""
        result = availability.handle({"title_ids": ["12345"]}, thunder_client)
        assert "error" in result, (
            "REMEDIATION: get_availability must return error when library_slug is missing"
        )

    def test_missing_title_ids_returns_error(self, thunder_client: MagicMock) -> None:
        """title_ids is required."""
        result = availability.handle({"library_slug": "lcpl"}, thunder_client)
        assert "error" in result, (
            "REMEDIATION: get_availability must return error when title_ids is missing"
        )

    def test_empty_title_ids_returns_error(self, thunder_client: MagicMock) -> None:
        """Empty title_ids array is invalid."""
        result = availability.handle({"library_slug": "lcpl", "title_ids": []}, thunder_client)
        assert "error" in result, (
            "REMEDIATION: get_availability must return error for empty title_ids array"
        )


class TestTitleIdCoercion:
    """Test that title_ids accepts both strings and integers."""

    def test_integer_ids_coerced_to_strings(self, thunder_client: MagicMock) -> None:
        """AC-2.2: Integer IDs are coerced to strings internally."""
        availability.handle({"library_slug": "lcpl", "title_ids": [12345, 67890]}, thunder_client)
        call_args = thunder_client.get_availability.call_args[0]
        # Second arg should be list of strings
        assert call_args[1] == [
            "12345",
            "67890",
        ], "REMEDIATION: get_availability must coerce integer title_ids to strings"

    def test_string_ids_passed_directly(self, thunder_client: MagicMock) -> None:
        """AC-2.2: String IDs pass through unchanged."""
        availability.handle(
            {"library_slug": "lcpl", "title_ids": ["12345", "67890"]}, thunder_client
        )
        call_args = thunder_client.get_availability.call_args[0]
        assert call_args[1] == ["12345", "67890"]


class TestEndpointVerification:
    """Test that the correct endpoint is called."""

    def test_calls_get_availability_not_bulk(self, thunder_client: MagicMock) -> None:
        """AC-2.3: Uses get_availability (library-scoped), not bulk metadata."""
        availability.handle({"library_slug": "lcpl", "title_ids": ["12345"]}, thunder_client)
        thunder_client.get_availability.assert_called_once()
        # Should NOT call search or any other method
        thunder_client.search.assert_not_called()

    def test_passes_correct_slug(self, thunder_client: MagicMock) -> None:
        """AC-2.1: library_slug used in endpoint path."""
        availability.handle({"library_slug": "nypl", "title_ids": ["12345"]}, thunder_client)
        call_args = thunder_client.get_availability.call_args[0]
        assert call_args[0] == "nypl"


class TestResponseMapping:
    """Test response data extraction from Thunder API response."""

    def test_returns_per_title_availability(
        self, thunder_client: MagicMock, sample_availability_response: dict
    ) -> None:
        """AC-2.4: Returns raw availability data per title."""
        thunder_client.get_availability.return_value = sample_availability_response
        result = availability.handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "1234567"]}, thunder_client
        )
        assert "titles" in result
        first_title = result["titles"][0]
        assert "copies_owned" in first_title, "REMEDIATION: Each title must have copies_owned"
        assert "copies_available" in first_title, (
            "REMEDIATION: Each title must have copies_available"
        )
        assert "holds_count" in first_title, "REMEDIATION: Each title must have holds_count"
        assert "estimated_wait_days" in first_title, (
            "REMEDIATION: Each title must have estimated_wait_days"
        )
        assert "title_id" in first_title

    def test_returns_library_slug_in_response(self, thunder_client: MagicMock) -> None:
        """Response includes the library_slug."""
        result = availability.handle(
            {"library_slug": "lcpl", "title_ids": ["9876543"]}, thunder_client
        )
        assert result.get("library_slug") == "lcpl"


class TestNotFoundHandling:
    """Test graceful handling of titles not found."""

    def test_missing_title_gets_error_indicator(self, thunder_client: MagicMock) -> None:
        """AC-2.6: Titles not in response get error indicator per title."""
        # Response only has items for 9876543, not 99999
        thunder_client.get_availability.return_value = {
            "items": [
                {
                    "id": "9876543",
                    "availableCopies": 2,
                    "ownedCopies": 3,
                    "holdsCount": 0,
                    "estimatedWaitDays": 0,
                }
            ]
        }
        result = availability.handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "99999"]}, thunder_client
        )
        titles = result["titles"]
        not_found = [t for t in titles if t.get("title_id") == "99999"]
        assert len(not_found) == 1, (
            "REMEDIATION: Must include an entry for titles not found in response"
        )
        assert "error" in not_found[0], "REMEDIATION: Not-found titles must have an error indicator"


class TestNoRecommendations:
    """Test that response contains no advisory logic."""

    def test_no_ranking_or_verdict_fields(
        self, thunder_client: MagicMock, sample_availability_response: dict
    ) -> None:
        """AC-2.5: No recommendations, rankings, or verdicts."""
        thunder_client.get_availability.return_value = sample_availability_response
        result = availability.handle(
            {"library_slug": "lcpl", "title_ids": ["9876543"]}, thunder_client
        )
        for title in result["titles"]:
            assert "recommendation" not in title
            assert "ranking" not in title
            assert "verdict" not in title
            assert "strategy" not in title

    def test_multiple_ids_in_single_response(
        self, thunder_client: MagicMock, sample_availability_response: dict
    ) -> None:
        """AC-2.7: All requested title IDs returned in single response."""
        thunder_client.get_availability.return_value = sample_availability_response
        result = availability.handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "1234567"]}, thunder_client
        )
        assert len(result["titles"]) == 2, (
            "REMEDIATION: All requested title IDs must be in the response"
        )
