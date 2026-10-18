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
        assert call_params["format"] == "audiobook-overdrive", (
            "REMEDIATION: search_titles must map 'audiobook' to 'audiobook-overdrive'"
        )

    def test_ebook_maps_to_overdrive(self, thunder_client: MagicMock) -> None:
        """AC-1.3: 'ebook' maps to 'ebook-overdrive'."""
        search_titles.handle({"library_slug": "lcpl", "format": "ebook"}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert call_params["format"] == "ebook-overdrive", (
            "REMEDIATION: search_titles must map 'ebook' to 'ebook-overdrive'"
        )


class TestAvailabilityFilter:
    """Test available parameter mapping."""

    def test_available_true_maps_to_show_only(self, thunder_client: MagicMock) -> None:
        """AC-1.4: available=true passes showOnlyAvailable=true."""
        search_titles.handle({"library_slug": "lcpl", "available": True}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert call_params.get("showOnlyAvailable") == "true", (
            "REMEDIATION: available=true must map to showOnlyAvailable=true"
        )

    def test_available_omitted_maps_to_available_first(self, thunder_client: MagicMock) -> None:
        """AC-1.16: When available omitted, pass availableFirst=true."""
        search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert call_params.get("availableFirst") == "true", (
            "REMEDIATION: When available is omitted, must pass availableFirst=true"
        )


class TestBISACFiltering:
    """Test belt-and-suspenders BISAC filtering."""

    def test_bisac_sent_server_side(self, thunder_client: MagicMock) -> None:
        """AC-1.6: bisac is sent to Thunder API as bisacCode param."""
        search_titles.handle({"library_slug": "lcpl", "bisac": "FIC129000"}, thunder_client)
        call_params = thunder_client.search.call_args[0][1]
        assert call_params.get("bisacCode") == "FIC129000", (
            "REMEDIATION: search_titles must send bisac as bisacCode to Thunder API"
        )

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
            assert "FIC028000" in title["bisac_codes"], (
                "REMEDIATION: Client-side BISAC filter must keep only items with matching code"
            )


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
        assert "1234567" not in title_ids, (
            "REMEDIATION: under_hours must exclude items with duration > threshold"
        )
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
        assert "id" in title and isinstance(title["id"], str), (
            "REMEDIATION: Title id must be a string"
        )
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
        assert title["link"] == "https://lcpl.overdrive.com/media/9876543", (
            "REMEDIATION: Title link must be https://{slug}.overdrive.com/media/{id}"
        )


class TestPaginationMetadata:
    """Test pagination fields in response."""

    def test_total_items_from_server(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-1.15: total_items from server response (before client-side filtering)."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert result["total_items"] == 843, (
            "REMEDIATION: total_items must come from Thunder API totalItems"
        )
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
        assert result["filtered_count"] == 1, (
            "REMEDIATION: filtered_count must reflect items remaining after client-side filtering"
        )


class TestFacets:
    """Test facets opt-in behavior (FR-A4)."""

    def test_facets_included_when_opted_in(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-A4.3: include_facets=True returns the populated facets object."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle(
            {"library_slug": "lcpl", "include_facets": True}, thunder_client
        )
        assert "facets" in result, (
            "REMEDIATION: search_titles response must include facets when include_facets=True"
        )
        assert "subjects" in result["facets"]

    def test_facets_omitted_by_default(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-A4.1/A4.2: facets key is absent (not empty) when include_facets omitted."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert "facets" not in result, (
            "REMEDIATION: facets must be omitted entirely when include_facets is not set"
        )

    def test_facets_omitted_when_explicitly_false(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-A4.2: include_facets=False omits the facets key entirely."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle(
            {"library_slug": "lcpl", "include_facets": False}, thunder_client
        )
        assert "facets" not in result


class TestLibrarySlugEcho:
    """FR-A3: single-library response echoes the queried library_slug."""

    def test_library_slug_echoed(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-A3.1: single-library response includes top-level library_slug."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert result.get("library_slug") == "lcpl", (
            "REMEDIATION: search_titles single-library response must include "
            "a top-level library_slug equal to the queried slug"
        )


class TestValidation:
    """Test input validation errors."""

    def test_missing_library_slug_returns_error(self, thunder_client: MagicMock) -> None:
        """Validation: library_slug is required."""
        result = search_titles.handle({}, thunder_client)
        assert "error" in result, (
            "REMEDIATION: search_titles must return error when library_slug is missing"
        )


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
        assert "error" in result, (
            "REMEDIATION: Empty results with totalItems=0 and no filters must return error"
        )
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
        assert "error" not in result, (
            "REMEDIATION: Empty results with narrowing filters must NOT be treated as error"
        )


# ---------------------------------------------------------------------------
# Group B — Fold availability into search (FR-B2)
# ---------------------------------------------------------------------------


class TestFoldedAvailability:
    """FR-B2: holds_per_copy and estimated_wait_days folded into each title."""

    def test_holds_per_copy_ratio(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-B2.1: holds_per_copy == round(holdsCount / ownedCopies, 1)."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # item 1: holds 0 / owned 3 -> 0.0
        assert titles["9876543"]["holds_per_copy"] == 0.0
        # item 2: holds 3 / owned 5 -> 0.6
        assert titles["1234567"]["holds_per_copy"] == 0.6
        # item 3: holds 1 / owned 2 -> 0.5
        assert titles["5555555"]["holds_per_copy"] == 0.5

    def test_holds_per_copy_zero_owned_zero_holds(self, thunder_client: MagicMock) -> None:
        """AC-B2.2: owned 0 and holds 0 -> 0.0 (no contention), no crash."""
        thunder_client.search.return_value = {
            "items": [{"id": "1", "title": "X", "ownedCopies": 0, "holdsCount": 0}],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert result["titles"][0]["holds_per_copy"] == 0.0

    def test_holds_per_copy_zero_owned_positive_holds(self, thunder_client: MagicMock) -> None:
        """AC-B2.2: owned 0 and holds > 0 -> None (ratio not meaningful)."""
        thunder_client.search.return_value = {
            "items": [{"id": "1", "title": "X", "ownedCopies": 0, "holdsCount": 4}],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert result["titles"][0]["holds_per_copy"] is None

    def test_estimated_wait_days_from_field(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-B2.3: estimated_wait_days sourced from estimatedWaitDays."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        assert titles["1234567"]["estimated_wait_days"] == 14

    def test_estimated_wait_days_absent_is_none(self, thunder_client: MagicMock) -> None:
        """AC-B2.3/B2.4: absent estimatedWaitDays -> None (unknown), not 0."""
        thunder_client.search.return_value = {
            "items": [{"id": "1", "title": "X", "ownedCopies": 2, "holdsCount": 1}],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        title = result["titles"][0]
        assert "estimated_wait_days" in title
        assert title["estimated_wait_days"] is None

    def test_existing_fields_intact(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-B2.5: folding does not break existing title fields."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        title = result["titles"][0]
        for key in (
            "available",
            "available_copies",
            "owned_copies",
            "holds_count",
            "hours",
        ):
            assert key in title


# ---------------------------------------------------------------------------
# Group B — Retire get_deep_link / well-formed links (FR-B3)
# ---------------------------------------------------------------------------


class TestWellFormedLinks:
    """FR-B3: every returned title carries a well-formed, non-empty link."""

    def test_link_well_formed_for_every_item(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-B3.2/B3.3: link == https://{slug}.overdrive.com/media/{id}, never malformed."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        for title in result["titles"]:
            assert title["link"] == f"https://lcpl.overdrive.com/media/{title['id']}"
            assert title["link"]
            assert not title["link"].endswith("/media/")
            assert not title["link"].endswith("/media/None")

    def test_item_missing_id_is_dropped(self, thunder_client: MagicMock) -> None:
        """AC-B3.3: an item with missing/None id is excluded, not emitted as a bad link."""
        thunder_client.search.return_value = {
            "items": [
                {"id": "111", "title": "Keeper"},
                {"id": None, "title": "No ID"},
                {"title": "Missing ID key"},
            ],
            "totalItems": 3,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        ids = [t["id"] for t in result["titles"]]
        assert ids == ["111"]
        assert result["filtered_count"] == 1


# ---------------------------------------------------------------------------
# Group B — Multi-library fan-out (FR-B1)
# ---------------------------------------------------------------------------


class TestLibrarySlugsValidation:
    """FR-B1 validation (AC-B1.9–B1.12)."""

    def test_empty_list(self) -> None:
        assert search_titles.validate_library_slugs([]) == "library_slugs must not be empty"

    def test_blank_entry(self) -> None:
        assert (
            search_titles.validate_library_slugs(["lcpl", "   "])
            == "library_slugs contains invalid entry: empty slug"
        )

    def test_empty_string_entry(self) -> None:
        assert (
            search_titles.validate_library_slugs([""])
            == "library_slugs contains invalid entry: empty slug"
        )

    def test_too_many_after_dedupe(self) -> None:
        slugs = [f"lib{i}" for i in range(11)]
        assert (
            search_titles.validate_library_slugs(slugs)
            == "library_slugs exceeds maximum of 10 libraries"
        )

    def test_duplicates_within_cap_ok(self) -> None:
        # 11 entries but only 2 unique -> valid
        slugs = ["lcpl", "fairfax"] * 6
        assert search_titles.validate_library_slugs(slugs) is None

    def test_dedupe_preserves_first_seen_order(self) -> None:
        assert search_titles.dedupe_library_slugs(
            ["lcpl", "fairfax", "lcpl", "nypl", "fairfax"]
        ) == ["lcpl", "fairfax", "nypl"]


class TestMultiLibraryFanOut:
    """FR-B1: grouped fan-out response and per-library failure isolation."""

    @staticmethod
    def _async_client_for(mapping: dict[str, dict]) -> MagicMock:
        """Build a mock ThunderClient whose search_async returns per-slug data."""
        client = MagicMock()

        async def _search_async(slug, params, async_client):
            return mapping[slug]

        client.search_async.side_effect = _search_async
        return client

    async def test_two_libraries_grouped(self) -> None:
        """AC-B1.4/B1.6: results grouped per library, each block carries its slug."""
        mapping = {
            "lcpl": {
                "items": [{"id": "a1", "title": "A"}],
                "totalItems": 1,
                "links": {"self": {"page": 1}, "last": {"page": 1}},
            },
            "fairfax": {
                "items": [{"id": "b1", "title": "B"}],
                "totalItems": 1,
                "links": {"self": {"page": 1}, "last": {"page": 1}},
            },
        }
        client = self._async_client_for(mapping)
        result = await search_titles.handle_multi(
            {"library_slugs": ["lcpl", "fairfax"], "include_facets": False}, client
        )
        assert "libraries" in result
        slugs = [b["library_slug"] for b in result["libraries"]]
        assert slugs == ["lcpl", "fairfax"]
        assert result["libraries"][0]["titles"][0]["id"] == "a1"
        assert result["libraries"][1]["titles"][0]["id"] == "b1"

    async def test_single_entry_still_grouped(self) -> None:
        """AC-B1.8: a single-entry library_slugs returns the grouped shape."""
        mapping = {
            "lcpl": {
                "items": [{"id": "a1", "title": "A"}],
                "totalItems": 1,
                "links": {"self": {"page": 1}, "last": {"page": 1}},
            }
        }
        client = self._async_client_for(mapping)
        result = await search_titles.handle_multi({"library_slugs": ["lcpl"]}, client)
        assert "libraries" in result
        assert len(result["libraries"]) == 1
        assert result["libraries"][0]["library_slug"] == "lcpl"

    async def test_per_library_failure_isolated(self) -> None:
        """AC-B1.5: one library's 404 yields an error block; the other returns titles."""
        mapping = {
            "lcpl": {
                "items": [{"id": "a1", "title": "A"}],
                "totalItems": 1,
                "links": {"self": {"page": 1}, "last": {"page": 1}},
            },
            "badslug": {
                "error": (
                    "unknown library slug: badslug — check the slug is correct "
                    "(e.g. lcpl, fairfax, nypl)"
                )
            },
        }
        client = self._async_client_for(mapping)
        result = await search_titles.handle_multi({"library_slugs": ["lcpl", "badslug"]}, client)
        blocks = {b["library_slug"]: b for b in result["libraries"]}
        assert "titles" in blocks["lcpl"]
        assert "error" in blocks["badslug"]
        assert "badslug" in blocks["badslug"]["error"]
        assert "titles" not in blocks["badslug"]


# ---------------------------------------------------------------------------
# Group C — Content enrichment (FR-C1, FR-C2, FR-C3)
#
# Field paths were verified against docs/references/thunder_openapi_v1.json
# (MediaDetailsResponse). The reference differs from the spec's original
# assumptions, so _normalize_item reads the real shapes while remaining
# tolerant of the spec's documented shapes:
#   - series name: bare `series` string, or `detailedSeries.seriesName`.
#   - series position: `detailedSeries.readingOrder` (string, coerced to float);
#     also tolerant of `series.readingOrder` when `series` is a dict.
#   - narrator: `creators[]` entries whose `role == "Narrator"` (`.name`).
#   - publisher: `publisher.name` (IdNamePair). None when a bare string/absent.
#   - edition: `edition` string. None when absent.
#   - is_abridged: `isAbridged` on the primary format entry; None when absent
#     (the field is not present in the OpenAPI reference, so it resolves to
#     None against the live API — exactly the AC-C3.2/C3.4 default).
# ---------------------------------------------------------------------------


class TestSeriesMetadata:
    """FR-C1: series_name and series_position (AC-C1.1–C1.4)."""

    def test_series_name_and_position_populated(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C1.1/C1.2: series_name and series_position populated from the item."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        assert titles["9876543"]["series_name"] == "Dungeon Crawler Carl"
        assert titles["9876543"]["series_position"] == 1.0
        assert isinstance(titles["9876543"]["series_position"], float)

    def test_series_position_coerced_to_float(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C1.4: readingOrder '1.5' coerces to 1.5 (float)."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        assert titles["1234567"]["series_position"] == 1.5

    def test_no_series_yields_none(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C1.3: no series object -> both keys present with None."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 3 (Project Hail Mary) has no series / detailedSeries.
        assert "series_name" in titles["5555555"]
        assert "series_position" in titles["5555555"]
        assert titles["5555555"]["series_name"] is None
        assert titles["5555555"]["series_position"] is None

    def test_non_numeric_reading_order_yields_none(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C1.4: non-numeric readingOrder -> series_position None (name still set)."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 4 has detailedSeries.readingOrder == "not-a-number".
        assert titles["7777777"]["series_name"] == "Bare Series String"
        assert titles["7777777"]["series_position"] is None

    def test_bare_string_series_does_not_crash(self, thunder_client: MagicMock) -> None:
        """AC-C1.3/C1.4: a bare-string `series` with no detailedSeries resolves cleanly."""
        thunder_client.search.return_value = {
            "items": [{"id": "1", "title": "X", "series": "Some Series"}],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        title = result["titles"][0]
        assert title["series_name"] == "Some Series"
        assert title["series_position"] is None


class TestToFloat:
    """FR-C1: _to_float helper (AC-C1.4)."""

    def test_numeric_string(self) -> None:
        assert search_titles._to_float("1") == 1.0
        assert search_titles._to_float("2.5") == 2.5

    def test_numeric_values(self) -> None:
        assert search_titles._to_float(3) == 3.0
        assert search_titles._to_float(4.0) == 4.0

    def test_non_numeric_and_missing(self) -> None:
        assert search_titles._to_float("not-a-number") is None
        assert search_titles._to_float(None) is None
        assert search_titles._to_float("") is None


class TestNarratorPublisherEdition:
    """FR-C2: narrator, publisher, edition (AC-C2.1–C2.4)."""

    def test_narrator_list_from_creators(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C2.1: narrator is the list of creators[] whose role == Narrator."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        assert titles["9876543"]["narrator"] == ["Jeff Hays"]
        assert titles["1234567"]["narrator"] == ["Nick Podehl", "Rupert Degas"]

    def test_narrator_empty_when_absent(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C2.1: narrator is [] when there are no creators / no narrators."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 3 has no creators array at all.
        assert titles["5555555"]["narrator"] == []

    def test_narrator_tolerates_malformed_creators(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C2.4: creators list with bare strings mixed in does not crash."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 4 creators = ["Just A String", {name: Real Narrator, role: Narrator}].
        assert titles["7777777"]["narrator"] == ["Real Narrator"]

    def test_publisher_string(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C2.2: publisher is the name string from publisher.name."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        assert titles["9876543"]["publisher"] == "Podium Audio"

    def test_publisher_bare_string_resolves_none(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C2.2/C2.4: a bare-string publisher (not an object) resolves to None."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 4 publisher is a bare string.
        assert titles["7777777"]["publisher"] is None

    def test_publisher_none_when_absent(self, thunder_client: MagicMock) -> None:
        """AC-C2.2: publisher is None when the field is absent."""
        thunder_client.search.return_value = {
            "items": [{"id": "1", "title": "X"}],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        title = result["titles"][0]
        assert "publisher" in title
        assert title["publisher"] is None

    def test_edition_string(self, thunder_client: MagicMock, sample_search_response: dict) -> None:
        """AC-C2.3: edition is the string value when present."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        assert titles["9876543"]["edition"] == "Unabridged"

    def test_edition_none_when_absent(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C2.3: edition is None when absent or null."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 3 has no edition key; item 4 has edition: null.
        assert titles["5555555"]["edition"] is None
        assert titles["7777777"]["edition"] is None


class TestDurationAndAbridged:
    """FR-C3: hours always present, is_abridged (AC-C3.1–C3.4)."""

    def test_hours_key_always_present(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C3.1: every title has an `hours` key (float or None)."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        for title in result["titles"]:
            assert "hours" in title
            assert title["hours"] is None or isinstance(title["hours"], float)
        titles = {t["id"]: t for t in result["titles"]}
        # Audiobook with a parseable duration -> float; ebook with empty -> None.
        assert titles["9876543"]["hours"] == 12.6
        assert titles["5555555"]["hours"] is None

    def test_is_abridged_reflects_field(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C3.2/C3.3: is_abridged reads isAbridged from the primary format."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        assert titles["9876543"]["is_abridged"] is False
        assert titles["1234567"]["is_abridged"] is True

    def test_is_abridged_none_when_absent(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-C3.2/C3.4: absent isAbridged -> None (unknown), not False."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 3 (ebook) and item 4 (audiobook) have no isAbridged on the format.
        assert "is_abridged" in titles["5555555"]
        assert titles["5555555"]["is_abridged"] is None
        assert titles["7777777"]["is_abridged"] is None

    def test_is_abridged_no_formats_does_not_crash(self, thunder_client: MagicMock) -> None:
        """AC-C3.4: an item with no formats resolves is_abridged to None cleanly."""
        thunder_client.search.return_value = {
            "items": [{"id": "1", "title": "X"}],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        title = result["titles"][0]
        assert title["is_abridged"] is None
        assert title["hours"] is None


class TestEstimatedWaitDaysCorrection:
    """FR-D3: estimated_wait_days folded correction (AC-D3.3)."""

    def test_available_title_wait_forced_zero(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D3.3: available_copies > 0 → estimated_wait_days == 0, even with a
        nonzero raw estimatedWaitDays."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # item 5555555: availableCopies 1, raw estimatedWaitDays 7 → corrected 0
        assert titles["5555555"]["available_copies"] == 1
        assert titles["5555555"]["estimated_wait_days"] == 0

    def test_not_available_title_wait_passthrough(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D3.3: not available → raw value passes through."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # item 1234567: availableCopies 0, raw estimatedWaitDays 14 → unchanged
        assert titles["1234567"]["available_copies"] == 0
        assert titles["1234567"]["estimated_wait_days"] == 14

    def test_not_available_absent_wait_is_none(self, thunder_client: MagicMock) -> None:
        """AC-D3.3: not available + absent estimatedWaitDays → None (v2 rule kept)."""
        thunder_client.search.return_value = {
            "items": [
                {"id": "1", "title": "X", "ownedCopies": 2, "holdsCount": 1, "availableCopies": 0}
            ],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert result["titles"][0]["estimated_wait_days"] is None

    def test_keys_on_copies_not_is_available(self, thunder_client: MagicMock) -> None:
        """AC-D3.4: correction keys on available_copies, not isAvailable alone."""
        thunder_client.search.return_value = {
            "items": [
                {
                    "id": "1",
                    "title": "X",
                    "ownedCopies": 2,
                    "holdsCount": 1,
                    "availableCopies": 3,
                    "isAvailable": False,
                    "estimatedWaitDays": 9,
                }
            ],
            "totalItems": 1,
            "links": {"self": {"page": 1}, "last": {"page": 1}},
        }
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        # available_copies > 0 → corrected to 0 regardless of isAvailable=False
        assert result["titles"][0]["estimated_wait_days"] == 0


class TestSlugNormalization:
    """FR-D2: library slugs normalized at the input boundary (AC-D2.1–D2.4)."""

    def test_single_slug_lowercased_in_response_and_link(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D2.2: handle receives an already-normalized slug (lowercase host)."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        assert result["library_slug"] == "lcpl"
        for t in result["titles"]:
            assert t["link"].startswith("https://lcpl.overdrive.com/media/")

    def test_dedupe_after_normalization(self) -> None:
        """AC-D2.4: dedupe on already-normalized values collapses LCPL/lcpl → one."""
        # dedupe operates on normalized values; the handler normalizes upstream.
        assert search_titles.dedupe_library_slugs(["lcpl", "lcpl"]) == ["lcpl"]


# ---------------------------------------------------------------------------
# Group D — Honest is_abridged (FR-D4)
#
# D4 replaces the v2 "primary_fmt.get('isAbridged')" read (which fabricated
# nothing but relied on a field absent from the live API) with an honest
# _derive_abridged(primary_fmt, edition): the format-entry boolean wins when
# present, else an "unabridged"/"abridged" token in the edition string decides,
# else None (unknown) — never a fabricated False (AC-D4.1/D4.2).
# ---------------------------------------------------------------------------


class TestHonestAbridged:
    """FR-D4: is_abridged is honest — derived or None, never a fabricated constant."""

    def test_format_bool_true(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D4.1: a format-entry isAbridged=true yields True."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 1234567 has formats[0].isAbridged == true.
        assert titles["1234567"]["is_abridged"] is True

    def test_format_bool_false(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D4.1: a format-entry isAbridged=false yields False."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 9876543 has formats[0].isAbridged == false.
        assert titles["9876543"]["is_abridged"] is False

    def test_edition_derived_unabridged(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D4.1: with no format bool, an 'Unabridged' edition string yields False."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 8888888: edition 'Unabridged', no isAbridged on the format.
        assert titles["8888888"]["is_abridged"] is False

    def test_edition_derived_abridged(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D4.1: with no format bool, an 'Abridged' edition string yields True."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 9999999: edition 'Abridged Edition', no isAbridged on the format.
        assert titles["9999999"]["is_abridged"] is True

    def test_no_signal_is_none(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D4.2: no format bool and no edition token -> None (never False)."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        titles = {t["id"]: t for t in result["titles"]}
        # Item 5555555 (no edition) and item 7777777 (edition: null) -> None.
        assert titles["5555555"]["is_abridged"] is None
        assert titles["7777777"]["is_abridged"] is None

    def test_never_fabricated_constant_across_fixture(
        self, thunder_client: MagicMock, sample_search_response: dict
    ) -> None:
        """AC-D4.4: is_abridged is not a hardcoded constant across the fixture set."""
        thunder_client.search.return_value = sample_search_response
        result = search_titles.handle({"library_slug": "lcpl"}, thunder_client)
        values = {t["is_abridged"] for t in result["titles"]}
        # The mixed fixture must produce more than one distinct value — a
        # fabricated constant (always False / always None) would collapse this.
        assert values == {True, False, None}

    def test_derive_abridged_unit(self) -> None:
        """AC-D4.1/D4.2: _derive_abridged resolves each source cleanly."""
        d = search_titles._derive_abridged
        # format-entry bool wins over everything
        assert d({"isAbridged": True}, "Unabridged") is True
        assert d({"isAbridged": False}, "Abridged") is False
        # edition string (case-insensitive) when no format bool; 'unabridged'
        # is checked before 'abridged' (substring containment order).
        assert d({}, "Unabridged") is False
        assert d({}, "UNABRIDGED Edition") is False
        assert d({}, "Abridged") is True
        assert d({}, "abridged") is True
        # unknown -> None, never fabricated False
        assert d({}, None) is None
        assert d({}, "") is None
        assert d({}, "Standard Edition") is None
        assert d({"isAbridged": "yes"}, None) is None  # non-bool format value ignored


# ---------------------------------------------------------------------------
# Group D — Exact-title search mode (FR-D6)
# ---------------------------------------------------------------------------


class TestExactTitleSearch:
    """FR-D6: exact=true returns only exact (case-insensitive) title matches."""

    @staticmethod
    def _page(items: list[dict], total: int, page: int, last: int) -> dict:
        return {
            "items": items,
            "totalItems": total,
            "links": {"self": {"page": page}, "last": {"page": last}},
        }

    def test_exact_filters_to_exact_match(self, thunder_client: MagicMock) -> None:
        """AC-D6.2/D6.3: exact=true keeps only the exact (case-insensitive) title."""
        thunder_client.search.return_value = self._page(
            [
                {"id": "1", "title": "The Mom Test"},
                {"id": "2", "title": "The Mom Test Workbook"},
                {"id": "3", "title": "Mom Test Notes"},
            ],
            total=3,
            page=1,
            last=1,
        )
        result = search_titles.handle(
            {"library_slug": "lcpl", "query": "the mom test", "exact": True}, thunder_client
        )
        ids = [t["id"] for t in result["titles"]]
        assert ids == ["1"]
        assert result["filtered_count"] == 1

    def test_exact_case_insensitive(self, thunder_client: MagicMock) -> None:
        """AC-D6.2: comparison is title.strip().lower() == query.strip().lower()."""
        thunder_client.search.return_value = self._page(
            [{"id": "1", "title": "The Mom Test"}, {"id": "2", "title": "Other"}],
            total=2,
            page=1,
            last=1,
        )
        result = search_titles.handle(
            {"library_slug": "lcpl", "query": "  THE MOM TEST  ", "exact": True}, thunder_client
        )
        assert [t["id"] for t in result["titles"]] == ["1"]

    def test_exact_requests_relevance_and_perpage_100(self, thunder_client: MagicMock) -> None:
        """AC-D6.7: exact=true requests perPage=100 and sort=relevance (not availableFirst)."""
        thunder_client.search.return_value = self._page(
            [{"id": "1", "title": "The Mom Test"}], total=1, page=1, last=1
        )
        search_titles.handle(
            {"library_slug": "lcpl", "query": "the mom test", "exact": True}, thunder_client
        )
        call_params = thunder_client.search.call_args[0][1]
        assert call_params["perPage"] == "100"
        assert call_params.get("sortBy") == "relevance"
        assert "availableFirst" not in call_params

    def test_exact_false_preserves_fuzzy(self, thunder_client: MagicMock) -> None:
        """AC-D6.5: exact=false keeps every fuzzy match (v2 behavior)."""
        thunder_client.search.return_value = self._page(
            [
                {"id": "1", "title": "The Mom Test"},
                {"id": "2", "title": "The Mom Test Workbook"},
            ],
            total=2,
            page=1,
            last=1,
        )
        result = search_titles.handle(
            {"library_slug": "lcpl", "query": "the mom test", "exact": False}, thunder_client
        )
        assert sorted(t["id"] for t in result["titles"]) == ["1", "2"]

    def test_exact_omitted_preserves_fuzzy(self, thunder_client: MagicMock) -> None:
        """AC-D6.5: omitting exact keeps every fuzzy match and uses availableFirst."""
        thunder_client.search.return_value = self._page(
            [
                {"id": "1", "title": "The Mom Test"},
                {"id": "2", "title": "The Mom Test Workbook"},
            ],
            total=2,
            page=1,
            last=1,
        )
        result = search_titles.handle(
            {"library_slug": "lcpl", "query": "the mom test"}, thunder_client
        )
        assert len(result["titles"]) == 2
        call_params = thunder_client.search.call_args[0][1]
        assert call_params.get("availableFirst") == "true"

    def test_exact_match_on_page_two(self, thunder_client: MagicMock) -> None:
        """AC-D6.7: an exact match beyond page 1 is still found (paging up to 3)."""
        page1 = self._page(
            [{"id": f"p1-{i}", "title": f"Mom Test Variant {i}"} for i in range(3)],
            total=5,
            page=1,
            last=2,
        )
        page2 = self._page(
            [
                {"id": "exact", "title": "The Mom Test"},
                {"id": "p2-x", "title": "Mom Test Appendix"},
            ],
            total=5,
            page=2,
            last=2,
        )
        thunder_client.search.side_effect = [page1, page2]
        result = search_titles.handle(
            {"library_slug": "lcpl", "query": "The Mom Test", "exact": True}, thunder_client
        )
        assert [t["id"] for t in result["titles"]] == ["exact"]
        # Two page fetches occurred (page 1 then page 2).
        assert thunder_client.search.call_count == 2

    def test_exact_stops_at_three_pages(self, thunder_client: MagicMock) -> None:
        """AC-D6.7: paging is bounded at 3 pages; no match -> empty, <=3 fetches."""
        no_match = self._page([{"id": "x", "title": "No Match Here"}], total=1000, page=1, last=50)
        thunder_client.search.return_value = no_match
        result = search_titles.handle(
            {"library_slug": "lcpl", "query": "The Mom Test", "exact": True}, thunder_client
        )
        assert result["titles"] == []
        assert thunder_client.search.call_count <= 3

    def test_exact_no_extra_fetch_when_single_page(self, thunder_client: MagicMock) -> None:
        """AC-D6.7: when the catalog has a single page, no further fetch is attempted."""
        thunder_client.search.return_value = self._page(
            [{"id": "x", "title": "Not It"}], total=1, page=1, last=1
        )
        result = search_titles.handle(
            {"library_slug": "lcpl", "query": "The Mom Test", "exact": True}, thunder_client
        )
        assert result["titles"] == []
        assert thunder_client.search.call_count == 1

    def test_exact_paging_error_surfaces(self, thunder_client: MagicMock) -> None:
        """AC-D6.7: an error fetching a later page surfaces as an error result."""
        page1 = self._page([{"id": "x", "title": "Not It"}], total=5, page=1, last=3)
        err = {"error": "Library catalog temporarily unavailable — try again"}
        thunder_client.search.side_effect = [page1, err]
        result = search_titles.handle(
            {"library_slug": "lcpl", "query": "The Mom Test", "exact": True}, thunder_client
        )
        assert "error" in result
        assert thunder_client.search.call_count == 2


class TestExactTitleSearchMultiLibrary:
    """FR-D6: exact filtering applies on the multi-library (library_slugs) path too."""

    async def test_exact_filters_each_library_block(self) -> None:
        """AC-D6.6: exact=true prunes each per-library block to exact matches."""
        mapping = {
            "lcpl": {
                "items": [
                    {"id": "a1", "title": "The Mom Test"},
                    {"id": "a2", "title": "The Mom Test Workbook"},
                ],
                "totalItems": 2,
                "links": {"self": {"page": 1}, "last": {"page": 1}},
            },
            "fairfax": {
                "items": [
                    {"id": "b1", "title": "Mom Test Notes"},
                    {"id": "b2", "title": "the mom test"},
                ],
                "totalItems": 2,
                "links": {"self": {"page": 1}, "last": {"page": 1}},
            },
        }

        client = MagicMock()

        async def _search_async(slug, params, async_client):
            return mapping[slug]

        client.search_async.side_effect = _search_async

        result = await search_titles.handle_multi(
            {
                "library_slugs": ["lcpl", "fairfax"],
                "query": "The Mom Test",
                "exact": True,
                "include_facets": False,
            },
            client,
        )
        blocks = {b["library_slug"]: b for b in result["libraries"]}
        assert [t["id"] for t in blocks["lcpl"]["titles"]] == ["a1"]
        assert [t["id"] for t in blocks["fairfax"]["titles"]] == ["b2"]

    async def test_exact_page_two_within_library(self) -> None:
        """AC-D6.7 (multi): a per-library exact match on page 2 is still found."""
        pages = {
            "lcpl": [
                {
                    "items": [{"id": "p1", "title": "The Mom Test Workbook"}],
                    "totalItems": 2,
                    "links": {"self": {"page": 1}, "last": {"page": 2}},
                },
                {
                    "items": [{"id": "p2", "title": "The Mom Test"}],
                    "totalItems": 2,
                    "links": {"self": {"page": 2}, "last": {"page": 2}},
                },
            ]
        }
        client = MagicMock()

        async def _search_async(slug, params, async_client):
            # page is a string in the Thunder params; pop from the per-slug queue.
            page = int(params.get("page", "1"))
            return pages[slug][page - 1]

        client.search_async.side_effect = _search_async

        result = await search_titles.handle_multi(
            {"library_slugs": ["lcpl"], "query": "The Mom Test", "exact": True},
            client,
        )
        block = result["libraries"][0]
        assert [t["id"] for t in block["titles"]] == ["p2"]
        assert client.search_async.call_count == 2

    async def test_exact_paging_error_isolated_to_block(self) -> None:
        """AC-B1.5/D6.6: an error while paging a library surfaces as that block's error."""
        client = MagicMock()

        async def _search_async(slug, params, async_client):
            page = int(params.get("page", "1"))
            if page == 1:
                return {
                    "items": [{"id": "p1", "title": "Mom Test Notes"}],
                    "totalItems": 5,
                    "links": {"self": {"page": 1}, "last": {"page": 3}},
                }
            return {"error": "OverDrive service error (HTTP 500)", "status": 500}

        client.search_async.side_effect = _search_async

        result = await search_titles.handle_multi(
            {"library_slugs": ["lcpl"], "query": "The Mom Test", "exact": True},
            client,
        )
        block = result["libraries"][0]
        assert "error" in block
        assert "titles" not in block
