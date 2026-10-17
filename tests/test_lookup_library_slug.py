"""Tests for lookup_library_slug tool + SlugDirectory cache (D5).

Covers FR-D5 (AC-D5.1–D5.10): name/partial match, no-match empty list, cache hit
(no re-fetch within TTL), shared in-flight fetch for concurrent cold callers,
concurrent paging, directory-fetch failure serving stale cache or the bundled
fallback snapshot with a warning, and raw-data-only output (no ranking).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

slug_directory = pytest.importorskip(
    "src.slug_directory", reason="Implementation not yet written (TDD red phase)"
)
lookup_library_slug = pytest.importorskip(
    "src.tools.lookup_library_slug", reason="Implementation not yet written (TDD red phase)"
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def libraries_response() -> dict:
    return json.loads((FIXTURES_DIR / "libraries_response.json").read_text())


def _client_single_page(page_response: dict) -> MagicMock:
    """A ThunderClient mock whose async directory fetch returns one page."""
    client = MagicMock()
    client.list_libraries_async = AsyncMock(return_value=page_response)
    return client


class TestSlugDirectoryLoad:
    async def test_entries_returns_rows_with_slug_and_name(self, libraries_response: dict) -> None:
        client = _client_single_page(libraries_response)
        directory = slug_directory.SlugDirectory(client)
        rows = await directory.entries()
        slugs = {r["slug"] for r in rows}
        assert "fairfax" in slugs
        assert "lcpl" in slugs
        assert all("slug" in r and "name" in r for r in rows)

    async def test_slug_falls_back_to_id_when_no_preferred_key(self) -> None:
        page = {
            "items": [{"id": "clevnet", "name": "CLEVNET"}],
            "totalItems": 1,
        }
        client = _client_single_page(page)
        directory = slug_directory.SlugDirectory(client)
        rows = await directory.entries()
        assert rows == [{"slug": "clevnet", "name": "CLEVNET"}]

    async def test_cache_hit_does_not_refetch_within_ttl(self, libraries_response: dict) -> None:
        """AC-D5.5: repeated lookups within the TTL do not re-fetch."""
        client = _client_single_page(libraries_response)
        directory = slug_directory.SlugDirectory(client)
        await directory.entries()
        await directory.entries()
        assert client.list_libraries_async.call_count == 1

    async def test_expired_ttl_refetches(self, libraries_response: dict) -> None:
        client = _client_single_page(libraries_response)
        directory = slug_directory.SlugDirectory(client, ttl_seconds=0)
        await directory.entries()
        await directory.entries()
        assert client.list_libraries_async.call_count >= 2

    async def test_concurrent_cold_callers_share_one_fetch(self, libraries_response: dict) -> None:
        """AC-D5.9: concurrent callers in a cold window trigger a single fetch."""
        call_count = 0

        async def slow_fetch(page: int, per_page: int, async_client: object) -> dict:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return libraries_response

        client = MagicMock()
        client.list_libraries_async = AsyncMock(side_effect=slow_fetch)
        directory = slug_directory.SlugDirectory(client)

        await asyncio.gather(*[directory.entries() for _ in range(5)])
        # Only page 1 fetched, exactly once (totalItems == 3 → single page).
        assert call_count == 1

    async def test_concurrent_paging_fetches_remaining_pages(self) -> None:
        """AC-D5.5: page 1 read first for totalItems, then pages 2..N concurrently."""
        pages = {
            1: {
                "items": [{"preferredKey": "a", "name": "Alpha"}],
                "totalItems": 250,
            },
            2: {"items": [{"preferredKey": "b", "name": "Beta"}], "totalItems": 250},
            3: {"items": [{"preferredKey": "c", "name": "Gamma"}], "totalItems": 250},
        }

        async def fetch(page: int, per_page: int, async_client: object) -> dict:
            return pages[page]

        client = MagicMock()
        client.list_libraries_async = AsyncMock(side_effect=fetch)
        directory = slug_directory.SlugDirectory(client)
        rows = await directory.entries()
        # 250 items / 100 per page → 3 pages.
        assert client.list_libraries_async.call_count == 3
        assert {r["slug"] for r in rows} == {"a", "b", "c"}


class TestSlugDirectoryFailure:
    async def test_fetch_failure_with_no_cache_serves_fallback(self) -> None:
        """AC-D5.10: cold failure → bundled fallback snapshot, is_fallback() True."""
        client = MagicMock()
        client.list_libraries_async = AsyncMock(return_value={"error": "boom"})
        directory = slug_directory.SlugDirectory(client)
        rows = await directory.entries()
        assert rows, "fallback snapshot should not be empty"
        assert directory.is_fallback() is True

    async def test_fetch_failure_with_stale_cache_serves_stale(
        self, libraries_response: dict
    ) -> None:
        """NFR-3.3: a stale-but-present cache is served on a later fetch failure."""
        responses = [libraries_response, {"error": "boom"}]

        async def fetch(page: int, per_page: int, async_client: object) -> dict:
            return responses.pop(0)

        client = MagicMock()
        client.list_libraries_async = AsyncMock(side_effect=fetch)
        directory = slug_directory.SlugDirectory(client, ttl_seconds=0)
        first = await directory.entries()
        second = await directory.entries()
        assert second == first
        assert directory.is_fallback() is False


class TestLookupTool:
    async def test_match_fairfax_returns_fairfax_slug(self, libraries_response: dict) -> None:
        """AC-D5.4: lookup_library_slug('Fairfax') → slugs include 'fairfax'."""
        directory = slug_directory.SlugDirectory(_client_single_page(libraries_response))
        result = await lookup_library_slug.handle({"name": "fairfax"}, directory)
        assert "error" not in result
        assert "fairfax" in {m["slug"] for m in result["matches"]}

    async def test_partial_case_insensitive_match_on_name(self, libraries_response: dict) -> None:
        """AC-D5.3: case-insensitive substring over the name."""
        directory = slug_directory.SlugDirectory(_client_single_page(libraries_response))
        result = await lookup_library_slug.handle({"name": "county"}, directory)
        names = {m["slug"] for m in result["matches"]}
        # Both Fairfax County and Loudoun County match "county".
        assert {"fairfax", "lcpl"} <= names

    async def test_match_on_slug(self, libraries_response: dict) -> None:
        directory = slug_directory.SlugDirectory(_client_single_page(libraries_response))
        result = await lookup_library_slug.handle({"name": "nypl"}, directory)
        assert {m["slug"] for m in result["matches"]} == {"nypl"}

    async def test_no_match_returns_empty_list_not_error(self, libraries_response: dict) -> None:
        """AC-D5.6: a no-match lookup returns an empty list, not an error."""
        directory = slug_directory.SlugDirectory(_client_single_page(libraries_response))
        result = await lookup_library_slug.handle({"name": "zzzznomatch"}, directory)
        assert "error" not in result
        assert result["matches"] == []

    async def test_missing_name_returns_error(self, libraries_response: dict) -> None:
        directory = slug_directory.SlugDirectory(_client_single_page(libraries_response))
        result = await lookup_library_slug.handle({"name": ""}, directory)
        assert "error" in result

    async def test_returns_raw_rows_only_no_ranking(self, libraries_response: dict) -> None:
        """NFR-5.1: matches carry only slug + name — no ranking/recommendation keys."""
        directory = slug_directory.SlugDirectory(_client_single_page(libraries_response))
        result = await lookup_library_slug.handle({"name": "library"}, directory)
        for m in result["matches"]:
            assert set(m.keys()) == {"slug", "name"}

    async def test_fallback_adds_warning(self) -> None:
        """AC-D5.10: a fallback-sourced result carries a warning field."""
        client = MagicMock()
        client.list_libraries_async = AsyncMock(return_value={"error": "boom"})
        directory = slug_directory.SlugDirectory(client)
        result = await lookup_library_slug.handle({"name": "public"}, directory)
        assert "warning" in result
        assert result["matches"], "fallback snapshot should still match 'public'"

    async def test_directory_raise_returns_error(self) -> None:
        """AC-D5.8: an unexpected directory error returns a human-readable error."""

        class Boom:
            async def entries(self) -> list[dict[str, str]]:
                raise RuntimeError("kaboom")

            def is_fallback(self) -> bool:
                return False

        result = await lookup_library_slug.handle({"name": "x"}, Boom())
        assert "error" in result

    async def test_empty_directory_returns_error(self) -> None:
        """AC-D5.8: no rows at all (empty live + empty fallback) → error, not []."""

        class Empty:
            async def entries(self) -> list[dict[str, str]]:
                return []

            def is_fallback(self) -> bool:
                return False

        result = await lookup_library_slug.handle({"name": "x"}, Empty())
        assert "error" in result
