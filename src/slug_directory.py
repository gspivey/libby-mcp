"""In-process TTL cache of the Thunder library directory (FR-D5).

``lookup_library_slug`` matches a library name/partial-name against the full
OverDrive directory. ``GET /v2/libraries`` has no free-text search parameter, so
the whole directory is fetched, cached in-process with a 7-day TTL (it changes
slowly), and matched client-side.

Design points:
- The loader fetches **page 1 first** to read ``totalItems``, computes the page
  count, then fetches pages ``2..N`` **concurrently** with ``asyncio.gather``
  (AC-D5.5) so a large directory stays well inside the 30s Lambda timeout.
- A module-level ``SlugDirectory`` instance is reused across warm Lambda
  invocations (like the module-level ``ThunderClient``), so the directory is
  fetched at most once per TTL window (NFR-2.2).
- A per-instance ``asyncio.Lock`` guards the (re)load so concurrent callers in a
  cold window share **one** in-flight fetch rather than each walking the whole
  directory (AC-D5.9 / NFR-2.2).
- On a fetch failure a **stale-but-present** cache is served if available
  (NFR-3.3); on a cold failure with no cache, a bundled ``fallback_slugs.json``
  snapshot is served and callers can surface a ``warning`` (AC-D5.8 / AC-D5.10).

The directory holds **raw directory rows only** (slug + display name) — no
ranking or recommendation (NFR-5.1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from src.thunder_client import TIMEOUT_SECONDS, USER_AGENT

if TYPE_CHECKING:
    from src.thunder_client import ThunderClient

logger = logging.getLogger(__name__)

# Thunder caps perPage at 100.
_PER_PAGE = 100
# 7 days — the directory changes slowly (AC-D5.5).
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600
# Cap concurrent/total pages so a pathological totalItems cannot fan out
# unbounded; 100 pages * 100 = 10k libraries comfortably covers the directory.
_MAX_PAGES = 100

_FALLBACK_PATH = Path(__file__).resolve().parent / "fallback_slugs.json"


def _row_from_library(lib: dict[str, Any]) -> dict[str, str] | None:
    """Extract a ``{slug, name}`` row from a Thunder ``LibraryResponse``.

    The slug is ``preferredKey`` (falling back to ``id``). A row with neither a
    usable slug nor a name is dropped.
    """
    slug = lib.get("preferredKey") or lib.get("id")
    name = lib.get("name")
    if not slug:
        return None
    return {"slug": str(slug), "name": str(name) if name else ""}


def _load_fallback_rows() -> list[dict[str, str]]:
    """Load the bundled offline directory snapshot (AC-D5.10).

    Returns an empty list if the bundled file is missing or malformed — the
    caller then surfaces a directory-unavailable error.
    """
    try:
        data = json.loads(_FALLBACK_PATH.read_text())
    except (OSError, ValueError):
        logger.error("slug_directory_fallback_unreadable", extra={"path": str(_FALLBACK_PATH)})
        return []
    rows: list[dict[str, str]] = []
    for entry in data.get("libraries", []):
        slug = entry.get("slug")
        if not slug:
            continue
        rows.append({"slug": str(slug), "name": str(entry.get("name") or "")})
    return rows


class SlugDirectory:
    """TTL-cached, concurrently-loaded view of the Thunder library directory."""

    def __init__(self, client: "ThunderClient", ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._rows: list[dict[str, str]] | None = None
        self._fetched_at: float = 0.0
        self._from_fallback: bool = False
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._rows is not None and (time.monotonic() - self._fetched_at) < self._ttl

    async def entries(self) -> list[dict[str, str]]:
        """Return the cached directory rows, (re)loading if stale (AC-D5.5/D5.9).

        Concurrent callers in a cold window share a single in-flight fetch via
        the per-instance lock (double-checked after awaiting it). On a fetch
        failure, a stale cache is served if present; otherwise the bundled
        fallback snapshot is loaded.
        """
        if self._fresh():
            assert self._rows is not None
            return self._rows
        async with self._lock:
            # Double-check: another coroutine may have loaded while we waited.
            if self._fresh():
                assert self._rows is not None
                return self._rows
            try:
                rows = await self._load_all_pages_concurrent()
                self._rows = rows
                self._fetched_at = time.monotonic()
                self._from_fallback = False
            except Exception as exc:  # noqa: BLE001 — serve stale/fallback, never raise
                logger.warning("slug_directory_fetch_failed", extra={"error": str(exc)})
                if self._rows is not None:
                    return self._rows  # NFR-3.3: serve stale rows on failure
                # Cold failure, no cache → bundled fallback snapshot (AC-D5.10).
                self._rows = _load_fallback_rows()
                self._fetched_at = time.monotonic()
                self._from_fallback = True
            assert self._rows is not None
            return self._rows

    def is_fallback(self) -> bool:
        """True when the currently-cached rows came from the bundled snapshot."""
        return self._from_fallback

    async def _load_all_pages_concurrent(self) -> list[dict[str, str]]:
        """Fetch page 1, then pages 2..N concurrently (AC-D5.5)."""
        async with httpx.AsyncClient(
            headers={"user-agent": USER_AGENT}, timeout=TIMEOUT_SECONDS
        ) as async_client:
            first = await self._client.list_libraries_async(1, _PER_PAGE, async_client)
            if isinstance(first, dict) and first.get("error"):
                raise RuntimeError(first["error"])

            rows = self._rows_from_page(first)
            total_items = int(first.get("totalItems") or 0)
            page_count = min(_MAX_PAGES, max(1, math.ceil(total_items / _PER_PAGE)))
            if page_count <= 1:
                return rows

            pages = await asyncio.gather(
                *[
                    self._client.list_libraries_async(p, _PER_PAGE, async_client)
                    for p in range(2, page_count + 1)
                ]
            )
            for page in pages:
                if isinstance(page, dict) and page.get("error"):
                    raise RuntimeError(page["error"])
                rows.extend(self._rows_from_page(page))
        return rows

    @staticmethod
    def _rows_from_page(page: dict[str, Any]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for lib in page.get("items", []):
            row = _row_from_library(lib)
            if row is not None:
                rows.append(row)
        return rows
