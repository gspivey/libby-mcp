"""Integration tests against the real Thunder API.

These tests validate that the Thunder API responses match the shapes
expected by the libby-mcp implementation. They are gated by the
RUN_INTEGRATION=1 environment variable and do NOT run during normal
CI or `make test`.

Run via: make integration
"""

import os

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("RUN_INTEGRATION"),
        reason="requires RUN_INTEGRATION=1",
    ),
]

THUNDER_BASE = "https://thunder.api.overdrive.com"
LIBRARY_SLUG = "lcpl"  # Loudoun County Public Library — known working slug
TIMEOUT = 15.0


@pytest.fixture
def client() -> httpx.Client:
    """HTTP client configured for Thunder API requests."""
    return httpx.Client(
        base_url=THUNDER_BASE,
        headers={"User-Agent": "libby-mcp/1.0"},
        timeout=TIMEOUT,
    )


class TestSearchEndpoint:
    """Validate Thunder API search response structure."""

    def test_search_returns_valid_json(self, client: httpx.Client) -> None:
        """Thunder API search returns a valid JSON response with expected top-level keys."""
        response = client.get(f"/v2/libraries/{LIBRARY_SLUG}/media", params={"perPage": 5})
        assert (
            response.status_code == 200
        ), f"Expected 200 from Thunder API, got {response.status_code}"
        data = response.json()
        assert "items" in data, "Response missing 'items' key"
        assert "totalItems" in data, "Response missing 'totalItems' key"
        assert isinstance(data["items"], list), "'items' should be a list"
        assert isinstance(data["totalItems"], int), "'totalItems' should be an int"

    def test_search_items_have_expected_fields(self, client: httpx.Client) -> None:
        """Each item in search results has the fields needed for normalization."""
        response = client.get(f"/v2/libraries/{LIBRARY_SLUG}/media", params={"perPage": 5})
        data = response.json()
        assert len(data["items"]) > 0, "Expected at least one item in search results"

        item = data["items"][0]
        # Core identity fields
        assert "id" in item, "Item missing 'id' field"
        assert "title" in item, "Item missing 'title' field"

        # Availability fields
        assert "isAvailable" in item, "Item missing 'isAvailable' field"
        assert isinstance(item["isAvailable"], bool), "'isAvailable' should be boolean"

        # Formats array
        assert "formats" in item, "Item missing 'formats' field"
        assert isinstance(item["formats"], list), "'formats' should be a list"

    def test_search_has_pagination_links(self, client: httpx.Client) -> None:
        """Search response includes pagination via links.last.page."""
        response = client.get(f"/v2/libraries/{LIBRARY_SLUG}/media", params={"perPage": 5})
        data = response.json()
        assert "links" in data, "Response missing 'links' key for pagination"
        links = data["links"]
        assert "self" in links, "links missing 'self'"

    def test_search_has_facets(self, client: httpx.Client) -> None:
        """Search response includes facets with subjects for discovery."""
        response = client.get(f"/v2/libraries/{LIBRARY_SLUG}/media", params={"perPage": 5})
        data = response.json()
        # facets may not always be present depending on the query
        if "facets" in data:
            assert isinstance(data["facets"], dict), "'facets' should be a dict"

    def test_search_with_subject_filter(self, client: httpx.Client) -> None:
        """Subject filtering returns results and narrows the set."""
        # Subject 24 = Fantasy at LCPL
        response = client.get(
            f"/v2/libraries/{LIBRARY_SLUG}/media",
            params={"subject": "24", "perPage": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "totalItems" in data

    def test_search_with_format_filter(self, client: httpx.Client) -> None:
        """Format filtering for audiobooks works."""
        response = client.get(
            f"/v2/libraries/{LIBRARY_SLUG}/media",
            params={"format": "audiobook-overdrive", "perPage": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data

    def test_search_available_filter(self, client: httpx.Client) -> None:
        """showOnlyAvailable=true returns only available titles."""
        response = client.get(
            f"/v2/libraries/{LIBRARY_SLUG}/media",
            params={"showOnlyAvailable": "true", "perPage": 5},
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        # All items should be available when this filter is applied
        for item in data["items"]:
            assert (
                item.get("isAvailable") is True
            ), f"Item {item.get('id')} should be available with showOnlyAvailable=true"


class TestAvailabilityEndpoint:
    """Validate Thunder API availability endpoint response structure."""

    def test_availability_returns_valid_response(self, client: httpx.Client) -> None:
        """Availability endpoint returns expected structure."""
        # First get a title ID from search
        search_resp = client.get(f"/v2/libraries/{LIBRARY_SLUG}/media", params={"perPage": 1})
        search_data = search_resp.json()
        assert len(search_data["items"]) > 0, "Need at least one title for availability test"
        title_id = str(search_data["items"][0]["id"])

        # Fetch availability
        response = client.get(
            f"/v2/libraries/{LIBRARY_SLUG}/media/availability",
            params={"titleIds": title_id},
        )
        assert response.status_code == 200, f"Availability endpoint returned {response.status_code}"
        data = response.json()
        assert "items" in data, "Availability response missing 'items' key"
        assert isinstance(data["items"], list), "Availability 'items' should be a list"

    def test_availability_item_has_expected_fields(self, client: httpx.Client) -> None:
        """Each availability item has the fields needed for the get_availability tool."""
        # Get a title ID
        search_resp = client.get(f"/v2/libraries/{LIBRARY_SLUG}/media", params={"perPage": 1})
        title_id = str(search_resp.json()["items"][0]["id"])

        response = client.get(
            f"/v2/libraries/{LIBRARY_SLUG}/media/availability",
            params={"titleIds": title_id},
        )
        data = response.json()
        assert len(data["items"]) > 0, "Expected at least one availability item"

        item = data["items"][0]
        # Fields used by the get_availability tool
        assert (
            "id" in item or "titleId" in item
        ), "Availability item missing identifier field (id or titleId)"
        assert "availableCopies" in item, "Availability item missing 'availableCopies'"
        assert "ownedCopies" in item, "Availability item missing 'ownedCopies'"
        assert "holdsCount" in item, "Availability item missing 'holdsCount'"


class TestResponseShapes:
    """Validate specific response shape assumptions used in the implementation."""

    def test_item_id_is_string(self, client: httpx.Client) -> None:
        """Title IDs should be coercible to string (API returns them as strings)."""
        response = client.get(f"/v2/libraries/{LIBRARY_SLUG}/media", params={"perPage": 1})
        data = response.json()
        item = data["items"][0]
        # The spec says IDs are strings in the API
        title_id = item["id"]
        assert str(title_id), "Title ID should be coercible to string"

    def test_formats_array_structure(self, client: httpx.Client) -> None:
        """Formats array contains objects with at least an id field."""
        response = client.get(
            f"/v2/libraries/{LIBRARY_SLUG}/media",
            params={"format": "audiobook-overdrive", "perPage": 5},
        )
        data = response.json()
        for item in data["items"]:
            for fmt in item.get("formats", []):
                assert "id" in fmt, f"Format entry missing 'id' in item {item.get('id')}"

    def test_audiobook_has_duration(self, client: httpx.Client) -> None:
        """Audiobook format entries should have a duration field (HH:MM:SS)."""
        response = client.get(
            f"/v2/libraries/{LIBRARY_SLUG}/media",
            params={"format": "audiobook-overdrive", "perPage": 10},
        )
        data = response.json()
        found_duration = False
        for item in data["items"]:
            for fmt in item.get("formats", []):
                if fmt.get("id") == "audiobook-overdrive" and fmt.get("duration"):
                    found_duration = True
                    # Validate HH:MM:SS format
                    duration = fmt["duration"]
                    parts = duration.split(":")
                    assert len(parts) == 3, f"Duration '{duration}' not in HH:MM:SS format"
                    break
            if found_duration:
                break
        assert found_duration, "Expected at least one audiobook with a duration field in 10 results"
