"""Tests for multi-library get_availability fan-out (Task D7, FR-D7).

`get_availability` fans out across `library_slugs` the same way `search_titles`
already does, returning the grouped `{libraries: [...]}` shape. Both the single
and multi paths route through the shared always-async `classify_and_fetch`
(HIGH-4) so the title-id normalization, the non-empty rule, the 400/404-only
slug probe, and the multi-ID per-ID retry (HIGH-5) cannot diverge between paths.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

availability = pytest.importorskip(
    "src.tools.availability", reason="Implementation not yet written (TDD red phase)"
)


def _async_client() -> MagicMock:
    """A ThunderClient mock whose async helpers are AsyncMocks (empty items by default)."""
    client = MagicMock()
    client.get_availability_async = AsyncMock(return_value={"items": []})
    client.get_library_async = AsyncMock(return_value={"id": 1, "preferredKey": "lcpl"})
    return client


async def _handle_multi(params: dict[str, Any], client: MagicMock) -> dict[str, Any]:
    return await availability.handle_multi(params, client)


class TestGroupedShape:
    """AC-D7.3 / AC-D7.6: results grouped per library, each tagged with its slug."""

    async def test_two_libraries_grouped(self) -> None:
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            return {"items": [{"id": "9876543", "availableCopies": 2, "ownedCopies": 3}]}

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        result = await _handle_multi(
            {"library_slugs": ["lcpl", "fcpl"], "title_ids": ["9876543"]}, client
        )
        assert "libraries" in result
        slugs = [b["library_slug"] for b in result["libraries"]]
        assert slugs == ["lcpl", "fcpl"]
        for block in result["libraries"]:
            assert block["titles"][0]["title_id"] == "9876543"
            assert block["titles"][0]["copies_owned"] == 3

    async def test_block_order_preserves_slug_order(self) -> None:
        client = _async_client()
        result = await _handle_multi(
            {"library_slugs": ["nypl", "lcpl", "fcpl"], "title_ids": ["42"]}, client
        )
        assert [b["library_slug"] for b in result["libraries"]] == ["nypl", "lcpl", "fcpl"]


class TestPerLibraryFailureIsolation:
    """AC-D7.5: a per-library failure isolates to that block; others still return."""

    async def test_bad_slug_isolated(self) -> None:
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            if slug == "badslug":
                return {"error": "OverDrive service error (HTTP 404)", "status": 404}
            return {"items": [{"id": "9876543", "availableCopies": 1, "ownedCopies": 2}]}

        async def fake_lib(slug: str, ac: Any) -> dict[str, Any]:
            if slug == "badslug":
                return {"error": "unknown library slug: badslug", "status": 404}
            return {"id": 1, "preferredKey": slug}

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        client.get_library_async = AsyncMock(side_effect=fake_lib)
        result = await _handle_multi(
            {"library_slugs": ["lcpl", "badslug"], "title_ids": ["9876543"]}, client
        )
        blocks = {b["library_slug"]: b for b in result["libraries"]}
        assert blocks["lcpl"]["titles"][0]["copies_owned"] == 2
        assert "error" in blocks["badslug"]
        assert "titles" not in blocks["badslug"]

    async def test_one_library_raises_isolated_to_block(self) -> None:
        """AC-D7.5 / NFR-3.1: an exception in one library's call becomes that
        block's error without failing the whole request."""
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            if slug == "boom":
                raise RuntimeError("connection reset")
            return {"items": [{"id": "9876543", "availableCopies": 1, "ownedCopies": 2}]}

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        result = await _handle_multi(
            {"library_slugs": ["lcpl", "boom"], "title_ids": ["9876543"]}, client
        )
        blocks = {b["library_slug"]: b for b in result["libraries"]}
        assert blocks["lcpl"]["titles"][0]["copies_owned"] == 2
        assert "error" in blocks["boom"]


class TestSharedClassifierParity:
    """AC-D7.4: title_ids normalization / non-empty / non-numeric behave the same
    on the multi path as on the single path (via the shared classify_and_fetch)."""

    async def test_non_numeric_id_not_owned_per_block_without_call(self) -> None:
        """A non-numeric ID is classified not_owned in each block with no Thunder call."""
        client = _async_client()
        result = await _handle_multi(
            {"library_slugs": ["lcpl", "fcpl"], "title_ids": ["bad-title-id"]}, client
        )
        for block in result["libraries"]:
            assert block["titles"] == [{"title_id": "bad-title-id", "status": "not_owned"}]
        client.get_availability_async.assert_not_called()

    async def test_integer_and_padded_ids_normalized(self) -> None:
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            return {"items": [{"id": "123", "availableCopies": 1, "ownedCopies": 1}]}

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        result = await _handle_multi(
            {"library_slugs": ["lcpl"], "title_ids": [123, " 123 "]}, client
        )
        # Both normalize to "123"; the call carries stripped string IDs.
        sent_ids = client.get_availability_async.call_args[0][1]
        assert all(i == "123" for i in sent_ids)
        assert result["libraries"][0]["titles"][0]["title_id"] == "123"

    async def test_empty_title_ids_rejected_before_fan_out(self) -> None:
        """AC-D1.9 / AC-D7.4: an empty title_ids rejected in every block, no call."""
        client = _async_client()
        result = await _handle_multi({"library_slugs": ["lcpl", "fcpl"], "title_ids": []}, client)
        for block in result["libraries"]:
            assert block.get("error") == "title_ids is required (non-empty)"
        client.get_availability_async.assert_not_called()

    async def test_multi_id_valid_slug_400_retries_each_per_block(self) -> None:
        """HIGH-5: a 400 on a multi-ID batch with a valid slug retries each ID so
        owned titles survive — per library block."""
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            if len(ids) > 1:
                return {"error": "OverDrive service error (HTTP 400)", "status": 400}
            (tid,) = ids
            if tid == "9876543":
                return {"items": [{"id": "9876543", "availableCopies": 2, "ownedCopies": 3}]}
            return {"error": "OverDrive service error (HTTP 404)", "status": 404}

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        client.get_library_async = AsyncMock(return_value={"id": 1, "preferredKey": "lcpl"})
        result = await _handle_multi(
            {"library_slugs": ["lcpl", "fcpl"], "title_ids": ["9876543", "7777777"]}, client
        )
        for block in result["libraries"]:
            titles = {t["title_id"]: t for t in block["titles"]}
            assert titles["9876543"]["copies_owned"] == 3
            assert titles["7777777"] == {"title_id": "7777777", "status": "not_owned"}


class TestD3Correction:
    """D3 applies identically on the multi path (shared classifier)."""

    async def test_available_title_wait_zero(self) -> None:
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "id": "9876543",
                        "availableCopies": 1,
                        "ownedCopies": 2,
                        "estimatedWaitDays": 7,
                    }
                ]
            }

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        result = await _handle_multi({"library_slugs": ["lcpl"], "title_ids": ["9876543"]}, client)
        assert result["libraries"][0]["titles"][0]["estimated_wait_days"] == 0
