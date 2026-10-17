"""Tests for get_availability tool — raw availability data retrieval (v3).

Covers D1 (`not_owned` per-title status, non-numeric filtering, 400/404 slug
probe + multi-ID per-ID retry, non-empty validation, int/padded normalization)
and D3 (`estimated_wait_days == 0` when a copy is available). The single-library
`handle()` is async (HIGH-3) and routes through the shared, always-async
`classify_and_fetch`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

availability = pytest.importorskip(
    "src.tools.availability", reason="Implementation not yet written (TDD red phase)"
)


def _async_client() -> MagicMock:
    """A ThunderClient mock whose async helpers are AsyncMocks.

    By default the availability call returns an empty items envelope and the
    library probe reports a valid slug.
    """
    client = MagicMock()
    client.get_availability_async = AsyncMock(return_value={"items": []})
    client.get_library_async = AsyncMock(return_value={"id": 1, "preferredKey": "lcpl"})
    return client


async def _handle(params: dict[str, Any], client: MagicMock) -> dict[str, Any]:
    return await availability.handle(params, client)


class TestRequiredParams:
    """title_ids non-empty validation (AC-D1.9)."""

    async def test_missing_title_ids_returns_error(self) -> None:
        client = _async_client()
        result = await _handle({"library_slug": "lcpl"}, client)
        assert "error" in result
        client.get_availability_async.assert_not_called()

    async def test_empty_title_ids_returns_error(self) -> None:
        """AC-D1.9: an empty list is rejected with the non-empty message."""
        client = _async_client()
        result = await _handle({"library_slug": "lcpl", "title_ids": []}, client)
        assert result.get("error") == "title_ids is required (non-empty)"
        client.get_availability_async.assert_not_called()

    async def test_whitespace_only_ids_rejected_as_empty(self) -> None:
        """AC-D1.8/D1.9: a list that is empty after dropping blanks is rejected."""
        client = _async_client()
        result = await _handle({"library_slug": "lcpl", "title_ids": ["   ", ""]}, client)
        assert result.get("error") == "title_ids is required (non-empty)"
        client.get_availability_async.assert_not_called()


class TestNormalization:
    """HIGH-4: int / padded / non-str IDs coerced via str(tid).strip() (AC-D1.8)."""

    async def test_integer_id_coerced_to_string(self) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"items": [{"id": "123", "availableCopies": 1, "ownedCopies": 2}]}
        )
        result = await _handle({"library_slug": "lcpl", "title_ids": [123]}, client)
        sent_ids = client.get_availability_async.call_args[0][1]
        assert sent_ids == ["123"]
        assert result["titles"][0]["title_id"] == "123"

    async def test_padded_id_stripped(self) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"items": [{"id": "123", "availableCopies": 0, "ownedCopies": 1}]}
        )
        result = await _handle({"library_slug": "lcpl", "title_ids": [" 123 "]}, client)
        sent_ids = client.get_availability_async.call_args[0][1]
        assert sent_ids == ["123"]
        assert result["titles"][0]["title_id"] == "123"

    async def test_blank_entry_dropped_but_others_kept(self) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"items": [{"id": "123", "availableCopies": 1, "ownedCopies": 1}]}
        )
        result = await _handle({"library_slug": "lcpl", "title_ids": ["123", "  "]}, client)
        sent_ids = client.get_availability_async.call_args[0][1]
        assert sent_ids == ["123"]
        assert [t["title_id"] for t in result["titles"]] == ["123"]


class TestNotOwned:
    """D1: absent and non-numeric IDs are `not_owned`, never a crash."""

    async def test_absent_id_is_not_owned(self, sample_availability_response: dict) -> None:
        """AC-D1.1: a requested ID absent from items[] → status not_owned."""
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value=sample_availability_response["partial"]
        )
        result = await _handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "1234567"]}, client
        )
        titles = {t["title_id"]: t for t in result["titles"]}
        assert titles["9876543"].get("copies_owned") == 3
        assert titles["1234567"] == {"title_id": "1234567", "status": "not_owned"}

    async def test_only_absent_id_returns_success(self) -> None:
        """AC-D1.2: a batch whose only ID is not owned returns a flat success block."""
        client = _async_client()
        client.get_availability_async = AsyncMock(return_value={"items": []})
        result = await _handle({"library_slug": "lcpl", "title_ids": ["42"]}, client)
        assert "error" not in result
        assert result == {
            "library_slug": "lcpl",
            "titles": [{"title_id": "42", "status": "not_owned"}],
        }

    async def test_non_numeric_id_not_owned_without_thunder_call(self) -> None:
        """AC-D1.6: a non-numeric ID is not_owned WITHOUT any Thunder call."""
        client = _async_client()
        result = await _handle({"library_slug": "lcpl", "title_ids": ["bad-title-id"]}, client)
        assert result == {
            "library_slug": "lcpl",
            "titles": [{"title_id": "bad-title-id", "status": "not_owned"}],
        }
        client.get_availability_async.assert_not_called()

    async def test_mixed_numeric_and_non_numeric(self) -> None:
        """A non-numeric ID is dropped from the Thunder query; numeric IDs still fetched."""
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"items": [{"id": "9876543", "availableCopies": 2, "ownedCopies": 3}]}
        )
        result = await _handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "bad-title-id"]}, client
        )
        sent_ids = client.get_availability_async.call_args[0][1]
        assert sent_ids == ["9876543"]
        titles = {t["title_id"]: t for t in result["titles"]}
        assert titles["9876543"].get("copies_owned") == 3
        assert titles["bad-title-id"] == {"title_id": "bad-title-id", "status": "not_owned"}

    async def test_legacy_not_found_shape_removed(self, sample_availability_response: dict) -> None:
        """AC-D1.5: per-title key is `status`, never the legacy `{"error": "not_found"}`."""
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value=sample_availability_response["partial"]
        )
        result = await _handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "1234567"]}, client
        )
        for t in result["titles"]:
            assert t.get("error") != "not_found"


class TestOwnedData:
    """AC-D1.3: owned titles return full availability data."""

    async def test_owned_title_full_data(self, sample_availability_response: dict) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(return_value=sample_availability_response)
        result = await _handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "1234567"]}, client
        )
        owned = {t["title_id"]: t for t in result["titles"]}["9876543"]
        assert owned["copies_owned"] == 3
        assert owned["copies_available"] == 2
        assert owned["holds_count"] == 0
        assert "estimated_wait_days" in owned

    async def test_library_slug_in_response(self) -> None:
        client = _async_client()
        result = await _handle({"library_slug": "nypl", "title_ids": ["bad"]}, client)
        assert result["library_slug"] == "nypl"


class TestTopLevelErrors:
    """AC-D1.4 / AC-D1.7 / HIGH-5: genuine upstream failures stay top-level errors."""

    async def test_timeout_is_top_level_error(self) -> None:
        """A timeout (no status) is a top-level error, never probed."""
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"error": "Library catalog temporarily unavailable — try again"}
        )
        result = await _handle({"library_slug": "lcpl", "title_ids": ["123"]}, client)
        assert "error" in result
        client.get_library_async.assert_not_called()

    async def test_5xx_is_top_level_error_no_probe(self) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"error": "OverDrive service error (HTTP 500)", "status": 500}
        )
        result = await _handle({"library_slug": "lcpl", "title_ids": ["123"]}, client)
        assert "error" in result
        client.get_library_async.assert_not_called()

    @pytest.mark.parametrize("status", [429, 401, 403, 408])
    async def test_non_probed_4xx_statuses(self, status: int) -> None:
        """HIGH-5: 429/401/403/408 are top-level errors WITHOUT a slug probe."""
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"error": f"OverDrive service error (HTTP {status})", "status": status}
        )
        result = await _handle({"library_slug": "lcpl", "title_ids": ["123"]}, client)
        assert "error" in result
        client.get_library_async.assert_not_called()


class TestSlugProbe:
    """AC-D1.7 / HIGH-5: 400/404 on a valid slug reclassifies; bad slug → error."""

    @pytest.mark.parametrize("status", [400, 404])
    async def test_single_id_valid_slug_400_404_is_not_owned(self, status: int) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"error": f"OverDrive service error (HTTP {status})", "status": status}
        )
        client.get_library_async = AsyncMock(return_value={"id": 1, "preferredKey": "lcpl"})
        result = await _handle({"library_slug": "lcpl", "title_ids": ["123"]}, client)
        client.get_library_async.assert_awaited_once()
        assert result == {
            "library_slug": "lcpl",
            "titles": [{"title_id": "123", "status": "not_owned"}],
        }

    @pytest.mark.parametrize("status", [400, 404])
    async def test_bad_slug_is_top_level_error(self, status: int) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value={"error": f"OverDrive service error (HTTP {status})", "status": status}
        )
        client.get_library_async = AsyncMock(
            return_value={"error": "unknown library slug: badslug", "status": 404}
        )
        result = await _handle({"library_slug": "badslug", "title_ids": ["123"]}, client)
        assert result.get("error") == "unknown library slug: badslug"

    async def test_multi_id_valid_slug_400_retries_each(self) -> None:
        """HIGH-5: a 400 on a multi-ID batch with a valid slug retries each ID so
        owned titles survive."""
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            if len(ids) > 1:
                # The whole batch 400s (one poisoned ID).
                return {"error": "OverDrive service error (HTTP 400)", "status": 400}
            (tid,) = ids
            if tid == "9876543":
                return {"items": [{"id": "9876543", "availableCopies": 2, "ownedCopies": 3}]}
            # The other ID is genuinely unknown → per-ID 404.
            return {"error": "OverDrive service error (HTTP 404)", "status": 404}

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        client.get_library_async = AsyncMock(return_value={"id": 1, "preferredKey": "lcpl"})
        result = await _handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "7777777"]}, client
        )
        titles = {t["title_id"]: t for t in result["titles"]}
        assert titles["9876543"]["copies_owned"] == 3
        assert titles["7777777"] == {"title_id": "7777777", "status": "not_owned"}

    async def test_multi_id_valid_slug_400_per_id_transient_error_isolated(self) -> None:
        """HIGH-5: a transient (5xx) failure on a per-ID retry is isolated as a
        per-title error; the owned title in the same batch still returns full data."""
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            if len(ids) > 1:
                return {"error": "OverDrive service error (HTTP 400)", "status": 400}
            (tid,) = ids
            if tid == "9876543":
                return {"items": [{"id": "9876543", "availableCopies": 2, "ownedCopies": 3}]}
            return {"error": "OverDrive service error (HTTP 500)", "status": 500}

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        client.get_library_async = AsyncMock(return_value={"id": 1, "preferredKey": "lcpl"})
        result = await _handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "5000000"]}, client
        )
        titles = {t["title_id"]: t for t in result["titles"]}
        assert titles["9876543"]["copies_owned"] == 3
        assert titles["5000000"].get("error") == "OverDrive service error (HTTP 500)"

    async def test_multi_id_valid_slug_400_per_id_absent_is_not_owned(self) -> None:
        """HIGH-5: a per-ID retry that succeeds (200) but omits the ID → not_owned."""
        client = _async_client()

        async def fake_avail(slug: str, ids: list[str], ac: Any) -> dict[str, Any]:
            if len(ids) > 1:
                return {"error": "OverDrive service error (HTTP 400)", "status": 400}
            (tid,) = ids
            if tid == "9876543":
                return {"items": [{"id": "9876543", "availableCopies": 2, "ownedCopies": 3}]}
            # 200 OK but the ID is simply absent (library does not own it).
            return {"items": []}

        client.get_availability_async = AsyncMock(side_effect=fake_avail)
        client.get_library_async = AsyncMock(return_value={"id": 1, "preferredKey": "lcpl"})
        result = await _handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "6000000"]}, client
        )
        titles = {t["title_id"]: t for t in result["titles"]}
        assert titles["9876543"]["copies_owned"] == 3
        assert titles["6000000"] == {"title_id": "6000000", "status": "not_owned"}


class TestEstimatedWaitDaysCorrection:
    """D3: estimated_wait_days == 0 when copies_available > 0 (AC-D3.1/D3.2)."""

    async def test_available_title_wait_zero(self, sample_availability_response: dict) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(
            return_value=sample_availability_response["d3_available_with_nonzero_wait"]
        )
        result = await _handle(
            {"library_slug": "lcpl", "title_ids": ["9876543", "1234567"]}, client
        )
        titles = {t["title_id"]: t for t in result["titles"]}
        # available (1 copy) but raw estimatedWaitDays == 7 → corrected to 0
        assert titles["9876543"]["copies_available"] == 1
        assert titles["9876543"]["estimated_wait_days"] == 0
        # not available → raw value passed through
        assert titles["1234567"]["copies_available"] == 0
        assert titles["1234567"]["estimated_wait_days"] == 21


class TestNoRecommendations:
    """Fact-finding only — no advisory fields."""

    async def test_no_ranking_or_verdict_fields(self, sample_availability_response: dict) -> None:
        client = _async_client()
        client.get_availability_async = AsyncMock(return_value=sample_availability_response)
        result = await _handle({"library_slug": "lcpl", "title_ids": ["9876543"]}, client)
        for title in result["titles"]:
            for banned in ("recommendation", "ranking", "verdict", "strategy"):
                assert banned not in title
