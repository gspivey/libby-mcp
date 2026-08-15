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
        return self._execute_get(url)

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
        return self._execute_get(url)

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

    def _execute_get(self, url: str) -> dict[str, Any]:
        """Execute a GET request with error handling.

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
            logger.warning(
                "thunder_http_error",
                extra={
                    "url": url,
                    "status": e.response.status_code,
                    "duration_ms": duration * 1000,
                },
            )
            return {"error": f"OverDrive service error (HTTP {e.response.status_code})"}
        except Exception as e:
            duration = time.monotonic() - start
            logger.error(
                "thunder_unexpected_error",
                extra={"url": url, "error": str(e), "duration_ms": duration * 1000},
            )
            return {"error": "Unexpected error communicating with library catalog"}
