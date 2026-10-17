"""Thunder API HTTP client for OverDrive catalog queries.

Public API — no authentication required. Provides search and availability
methods with timeout handling and structured error responses.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://thunder.api.overdrive.com"
TIMEOUT_SECONDS = 10
USER_AGENT = "libby-mcp/1.0"


class ThunderClient:
    """HTTP client for the OverDrive Thunder API.

    Constructs URLs, sets proper headers, handles timeouts and errors.
    Reusable across Lambda warm invocations (module-level instantiation).
    """

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"user-agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )

    def search(self, slug: str, params: dict[str, Any]) -> dict[str, Any]:
        """Search a library's catalog via Thunder API.

        Args:
            slug: Library identifier (e.g., 'lcpl', 'nypl').
            params: Query parameters to send. Supports repeated params
                    via list values (e.g., {"subject": ["24", "80"]}).

        Returns:
            Parsed JSON response on success, or a dict with "error" key on failure.
        """
        url = self._build_search_url(slug, params)
        return self._execute_get(url, slug=slug)

    async def search_async(
        self, slug: str, params: dict[str, Any], async_client: httpx.AsyncClient
    ) -> dict[str, Any]:
        """Async variant of :meth:`search` for multi-library fan-out (FR-B1).

        The ``AsyncClient`` is passed in (created per fan-out, scoped to the
        current event loop) rather than stored on the instance: a client bound
        to a previous, now-closed loop would have an unusable connection pool on
        warm Lambda invocations.

        Args:
            slug: Library identifier.
            params: Query parameters (already built for the Thunder API).
            async_client: An ``httpx.AsyncClient`` scoped to the current loop.

        Returns:
            Parsed JSON response on success, or a dict with "error" key on failure.
        """
        url = self._build_search_url(slug, params)
        return await self._execute_get_async(url, async_client, slug=slug)

    def get_availability(self, slug: str, title_ids: list[str]) -> dict[str, Any]:
        """Get availability data for specific titles at a library.

        Uses the library-scoped availability endpoint:
        GET /v2/libraries/{slug}/media/availability?titleIds=...

        Args:
            slug: Library identifier.
            title_ids: List of title ID strings.

        Returns:
            Parsed JSON response with items array, or dict with "error" key.
        """
        url = self._build_availability_url(slug, title_ids)
        return self._execute_get(url, slug=slug)

    async def get_availability_async(
        self, slug: str, title_ids: list[str], async_client: httpx.AsyncClient
    ) -> dict[str, Any]:
        """Async bulk availability lookup (FR-D1 / HIGH-3).

        Awaited by ``availability.classify_and_fetch`` for BOTH the single-library
        ``handle()`` (now async) and the multi-library fan-out. The async client is
        passed in (created per fan-out, scoped to the current loop) for the same
        reason as :meth:`search_async`.

        Args:
            slug: Library identifier.
            title_ids: List of (numeric) title ID strings.
            async_client: An ``httpx.AsyncClient`` scoped to the current loop.

        Returns:
            Parsed JSON response with items array, or an error dict carrying the
            numeric ``status`` on an HTTP error.
        """
        url = self._build_availability_url(slug, title_ids)
        return await self._execute_get_async(url, async_client, slug=slug)

    async def get_library_async(self, slug: str, async_client: httpx.AsyncClient) -> dict[str, Any]:
        """Probe ``GET /v2/libraries/{slug}`` to tell a bad slug from a bad title.

        Used by the availability layer only on a ``400``/``404`` from the bulk
        availability call (HIGH-5): a 4xx here means the slug is bad; a 2xx means
        the slug is valid and the title IDs were rejected.
        """
        url = self._build_library_url(slug)
        return await self._execute_get_async(url, async_client, slug=slug)

    def get_library(self, slug: str) -> dict[str, Any]:
        """Sync mirror of :meth:`get_library_async`.

        Not used by the availability layer (the async helper is canonical); kept
        for non-async callers and parity.
        """
        url = self._build_library_url(slug)
        return self._execute_get(url, slug=slug)

    def list_libraries(self, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        """Fetch one page of the library directory (FR-D5).

        Calls ``GET /v2/libraries?page={page}&perPage={per_page}`` (public, no
        auth). ``perPage`` caps at 100 on the Thunder side. The directory has no
        free-text name search, so name matching is done client-side over the
        cached pages (see :mod:`src.slug_directory`).

        Args:
            page: 1-based page number.
            per_page: Items per page (Thunder max 100).

        Returns:
            A paged ``PagedResponse<LibraryResponse>`` dict (``items``,
            ``totalItems``, ``links``) on success, or ``{"error": ...}`` on failure.
        """
        url = self._build_libraries_url(page, per_page)
        return self._execute_get(url)

    async def list_libraries_async(
        self, page: int, per_page: int, async_client: httpx.AsyncClient
    ) -> dict[str, Any]:
        """Async variant of :meth:`list_libraries` for concurrent paging (FR-D5).

        The ``SlugDirectory`` loader fetches page 1 first (to read ``totalItems``)
        then fans out the remaining pages with ``asyncio.gather``. The async
        client is passed in (created per load, scoped to the current loop) for the
        same reason as :meth:`search_async`.
        """
        url = self._build_libraries_url(page, per_page)
        return await self._execute_get_async(url, async_client)

    def _build_libraries_url(self, page: int, per_page: int) -> str:
        """Build the library-directory endpoint URL with paging params."""
        base = f"{BASE_URL}/v2/libraries"
        query_string = urlencode({"page": page, "perPage": per_page})
        return f"{base}?{query_string}"

    def _build_search_url(self, slug: str, params: dict[str, Any]) -> str:
        """Build the full search URL with query parameters."""
        base = f"{BASE_URL}/v2/libraries/{slug}/media"
        if not params:
            return base
        query_string = self._encode_params(params)
        return f"{base}?{query_string}" if query_string else base

    def _build_availability_url(self, slug: str, title_ids: list[str]) -> str:
        """Build the availability endpoint URL with titleIds."""
        base = f"{BASE_URL}/v2/libraries/{slug}/media/availability"
        # titleIds are repeated params: titleIds=123&titleIds=456
        parts = [("titleIds", tid) for tid in title_ids]
        query_string = urlencode(parts)
        return f"{base}?{query_string}"

    def _build_library_url(self, slug: str) -> str:
        """Build the single-library endpoint URL (slug-validity probe)."""
        return f"{BASE_URL}/v2/libraries/{slug}"

    def _encode_params(self, params: dict[str, Any]) -> str:
        """Encode params dict to query string, handling repeated values."""
        parts: list[tuple[str, str]] = []
        for key, value in params.items():
            if isinstance(value, list):
                for v in value:
                    parts.append((key, str(v)))
            else:
                parts.append((key, str(value)))
        return urlencode(parts)

    def _execute_get(self, url: str, slug: str | None = None) -> dict[str, Any]:
        """Execute a GET request with error handling.

        Args:
            url: The fully-constructed request URL.
            slug: The library slug the request targets, used to produce a
                  human-readable message on HTTP 404 (FR-A2). When ``None``,
                  a 404 falls back to the generic service-error message.

        Returns parsed JSON on success, or {"error": "..."} on failure.
        """
        start = time.monotonic()
        try:
            response = self._client.get(url)
            duration = time.monotonic() - start
            logger.info(
                "thunder_request",
                extra={"url": url, "status": response.status_code, "duration_ms": duration * 1000},
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
        except httpx.TimeoutException:
            duration = time.monotonic() - start
            logger.warning(
                "thunder_timeout",
                extra={"url": url, "duration_ms": duration * 1000},
            )
            return {"error": "Library catalog temporarily unavailable — try again"}
        except httpx.HTTPStatusError as e:
            duration = time.monotonic() - start
            status = e.response.status_code
            logger.warning(
                "thunder_http_error",
                extra={
                    "url": url,
                    "status": status,
                    "duration_ms": duration * 1000,
                },
            )
            if status == 404 and slug is not None:
                return {
                    "error": (
                        f"unknown library slug: {slug} — check the slug is correct "
                        "(e.g. lcpl, fairfax, nypl)"
                    ),
                    "status": status,
                }
            return {"error": f"OverDrive service error (HTTP {status})", "status": status}
        except Exception as e:
            duration = time.monotonic() - start
            logger.error(
                "thunder_unexpected_error",
                extra={"url": url, "error": str(e), "duration_ms": duration * 1000},
            )
            return {"error": "Unexpected error communicating with library catalog"}

    async def _execute_get_async(
        self, url: str, async_client: httpx.AsyncClient, slug: str | None = None
    ) -> dict[str, Any]:
        """Async mirror of :meth:`_execute_get` for fan-out searches (FR-B1).

        Same logging and error mapping (including the FR-A2 404 message), but
        awaits the request on the per-fan-out ``async_client``.
        """
        start = time.monotonic()
        try:
            response = await async_client.get(url)
            duration = time.monotonic() - start
            logger.info(
                "thunder_request",
                extra={"url": url, "status": response.status_code, "duration_ms": duration * 1000},
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
        except httpx.TimeoutException:
            duration = time.monotonic() - start
            logger.warning(
                "thunder_timeout",
                extra={"url": url, "duration_ms": duration * 1000},
            )
            return {"error": "Library catalog temporarily unavailable — try again"}
        except httpx.HTTPStatusError as e:
            duration = time.monotonic() - start
            status = e.response.status_code
            logger.warning(
                "thunder_http_error",
                extra={"url": url, "status": status, "duration_ms": duration * 1000},
            )
            if status == 404 and slug is not None:
                return {
                    "error": (
                        f"unknown library slug: {slug} — check the slug is correct "
                        "(e.g. lcpl, fairfax, nypl)"
                    ),
                    "status": status,
                }
            return {"error": f"OverDrive service error (HTTP {status})", "status": status}
        except Exception as e:
            duration = time.monotonic() - start
            logger.error(
                "thunder_unexpected_error",
                extra={"url": url, "error": str(e), "duration_ms": duration * 1000},
            )
            return {"error": "Unexpected error communicating with library catalog"}
