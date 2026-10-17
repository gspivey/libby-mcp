"""get_availability tool — raw availability data retrieval (v3).

Fetches copy/hold counts for specific titles via the Thunder API
library-scoped availability endpoint. Returns factual data only —
no recommendations, rankings, or strategic advice.

v3 changes:
- D1: a requested title ID that the library does not own (absent from an
  otherwise-successful ``items[]``) is classified ``{"title_id": ...,
  "status": "not_owned"}`` instead of failing the batch. A non-numeric ID is
  classified ``not_owned`` *before* any Thunder call. A ``400``/``404`` on the
  availability call probes ``GET /v2/libraries/{slug}`` to tell a bad slug from a
  bad title; a valid slug with multiple IDs retries each ID individually so
  owned titles are not masked. ``401``/``403``/``408``/``429``/``5xx``/timeout are
  always a top-level error (never probed).
- D3: ``estimated_wait_days == 0`` whenever ``copies_available > 0``.
- D7: ``get_availability`` fans out across ``library_slugs`` (grouped
  ``{libraries: [...]}`` response), concurrently via ``asyncio.gather``, with
  per-library failure isolation. The fan-out reuses the D1 async helpers — it
  adds no new Thunder method.

The single-library ``handle()`` and the (D7) multi-library ``handle_multi()``
both route through the one always-async ``classify_and_fetch`` so the rules
cannot diverge.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from src.thunder_client import TIMEOUT_SECONDS, USER_AGENT

if TYPE_CHECKING:
    from src.thunder_client import ThunderClient

# HTTP statuses on the availability call that are ambiguous between a bad title
# and a bad library slug, and therefore trigger the slug probe (HIGH-5). Every
# other status (401/403/408/429, 5xx) and a missing status (timeout) stays a
# top-level error without probing.
_PROBE_STATUSES = (400, 404)


def _normalize_title_ids(title_ids: Any) -> list[str]:
    """Coerce every element to a stripped string (HIGH-4 / AC-D1.8).

    Runs before any ``isinstance``/``isdigit`` check so an int ID (``123``), a
    padded ``" 123 "``, or a non-str value is handled without raising. Blank
    (empty-after-strip) entries are dropped and never sent to Thunder.
    """
    out: list[str] = []
    for tid in title_ids or []:
        s = str(tid).strip()
        if s:
            out.append(s)
    return out


def _split_numeric(title_ids: list[str]) -> tuple[list[str], list[str]]:
    """Partition normalized IDs into numeric and non-numeric (AC-D1.6)."""
    numeric = [t for t in title_ids if t.isdigit()]
    non_numeric = [t for t in title_ids if not t.isdigit()]
    return numeric, non_numeric


def _shape_owned(tid: str, item: dict[str, Any]) -> dict[str, Any]:
    """Shape a single owned title's availability data (D1 fields + D3 rule)."""
    available = item.get("availableCopies", 0) or 0
    raw_wait = item.get("estimatedWaitDays", 0) or 0
    return {
        "title_id": tid,
        "copies_owned": item.get("ownedCopies", 0),
        "copies_available": available,
        "holds_count": item.get("holdsCount", 0),
        # D3 (AC-D3.1/D3.2): on the shelf → no wait, else pass through the raw value.
        "estimated_wait_days": 0 if available > 0 else raw_wait,
    }


def _shape_items(
    numeric_ids: list[str], non_numeric: list[str], items_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the per-title list for a successful availability response."""
    titles: list[dict[str, Any]] = [
        {"title_id": tid, "status": "not_owned"}
        for tid in non_numeric  # HIGH-3
    ]
    for tid in numeric_ids:
        item = items_by_id.get(tid)
        if item is None:
            titles.append({"title_id": tid, "status": "not_owned"})  # D1: absent → not_owned
        else:
            titles.append(_shape_owned(tid, item))
    return titles


async def _classify_one_id(
    slug: str, tid: str, client: "ThunderClient", async_client: httpx.AsyncClient
) -> dict[str, Any]:
    """Classify a single numeric ID (HIGH-5 per-ID retry).

    A ``200`` with the ID present → full data; absent or a per-ID ``400``/``404``
    → ``not_owned``; a transient/5xx on the retry → a per-title ``error`` so one
    bad ID is isolated without masking the owned titles in the same batch.
    """
    resp = await client.get_availability_async(slug, [tid], async_client)
    if isinstance(resp, dict) and resp.get("error"):
        status = resp.get("status")
        if isinstance(status, int) and status in _PROBE_STATUSES:
            return {"title_id": tid, "status": "not_owned"}
        return {"title_id": tid, "error": resp["error"]}
    item = {str(i.get("id")): i for i in resp.get("items", [])}.get(tid)
    if item is None:
        return {"title_id": tid, "status": "not_owned"}
    return _shape_owned(tid, item)


async def _retry_each_individually(
    slug: str, numeric_ids: list[str], client: "ThunderClient", async_client: httpx.AsyncClient
) -> list[dict[str, Any]]:
    """Retry each numeric ID alone (concurrently) on a multi-ID 400/404 (HIGH-5)."""
    return list(
        await asyncio.gather(
            *[_classify_one_id(slug, tid, client, async_client) for tid in numeric_ids]
        )
    )


async def classify_and_fetch(
    slug: str, title_ids: Any, client: "ThunderClient", async_client: httpx.AsyncClient
) -> dict[str, Any]:
    """Shared always-async availability classifier (single + multi paths, HIGH-4).

    Returns either ``{"titles": [...]}`` (to be shaped into the flat single or the
    per-library multi block) or a top-level ``{"error": ...}``.
    """
    # HIGH-4: normalize first, then enforce the non-empty rule on BOTH paths.
    norm = _normalize_title_ids(title_ids)
    if not norm:
        return {"error": "title_ids is required (non-empty)"}

    numeric_ids, non_numeric = _split_numeric(norm)
    if not numeric_ids:
        # AC-D1.6: no numeric IDs → make NO Thunder call; everything is not_owned.
        return {"titles": [{"title_id": t, "status": "not_owned"} for t in non_numeric]}

    resp = await client.get_availability_async(slug, numeric_ids, async_client)
    if isinstance(resp, dict) and resp.get("error"):
        status = resp.get("status")
        if isinstance(status, int) and status in _PROBE_STATUSES:
            # HIGH-5: probe the slug ONLY on 400/404 to tell a bad slug from a bad title.
            lib = await client.get_library_async(slug, async_client)
            if isinstance(lib, dict) and lib.get("error"):
                return {"error": f"unknown library slug: {slug}"}
            # Valid slug → the availability call rejected the IDs, not the library.
            if len(numeric_ids) == 1:
                base = [{"title_id": numeric_ids[0], "status": "not_owned"}]
            else:
                base = await _retry_each_individually(slug, numeric_ids, client, async_client)
            return {"titles": base + [{"title_id": t, "status": "not_owned"} for t in non_numeric]}
        # 401/403/408/429, 5xx, timeout (no/other status) → top-level error.
        return {"error": resp["error"]}

    items_by_id = {str(i.get("id")): i for i in resp.get("items", [])}
    return {"titles": _shape_items(numeric_ids, non_numeric, items_by_id)}


async def handle(params: dict[str, Any], client: "ThunderClient") -> dict[str, Any]:
    """Get availability data for specific titles at a library (single-library).

    Async (HIGH-3): creates a short-lived ``AsyncClient`` and awaits the shared
    ``classify_and_fetch``. The slug is already normalized by the handler wrapper
    (D2); the classifier re-validates the title IDs defensively.

    Returns the flat v2 shape ``{library_slug, titles}`` (AC-D7.6) or
    ``{library_slug, error}`` on a top-level failure.
    """
    library_slug = params.get("library_slug")
    if not library_slug:
        return {"error": "library_slug is required"}
    title_ids = params.get("title_ids")

    async with httpx.AsyncClient(
        headers={"user-agent": USER_AGENT}, timeout=TIMEOUT_SECONDS
    ) as async_client:
        block = await classify_and_fetch(library_slug, title_ids, client, async_client)

    if block.get("error"):
        return {"library_slug": library_slug, "error": block["error"]}
    return {"library_slug": library_slug, **block}


async def _availability_one(
    slug: str, title_ids: Any, client: "ThunderClient", async_client: httpx.AsyncClient
) -> dict[str, Any]:
    """Fetch one library's availability block for the multi-library fan-out (D7).

    Routes through the SAME ``classify_and_fetch`` as the single-library path
    (HIGH-4), so title-id normalization, the non-empty rule, the ``400``/``404``
    slug probe, and the multi-ID per-ID retry (HIGH-5) behave identically here.
    A per-library failure (bad slug, timeout, or an unexpected exception) is
    isolated to this block as an ``error`` field (AC-D7.5 / NFR-3.1).

    Returns the flat block tagged with its slug: either
    ``{library_slug, titles}`` or ``{library_slug, error}``.
    """
    try:
        block = await classify_and_fetch(slug, title_ids, client, async_client)
    except Exception as exc:  # noqa: BLE001 — isolate per-library failures (AC-D7.5)
        return {"library_slug": slug, "error": str(exc)}
    if block.get("error"):
        return {"library_slug": slug, "error": block["error"]}
    return {"library_slug": slug, **block}


async def handle_multi(params: dict[str, Any], client: "ThunderClient") -> dict[str, Any]:
    """Multi-library ``get_availability`` fan-out (FR-D7).

    Assumes ``params['library_slugs']`` has already been normalized, validated,
    and deduplicated by the handler wrapper. Runs the per-library availability
    calls concurrently with ``asyncio.gather`` so N libraries complete in roughly
    the time of the slowest one (AC-D7.7 / NFR-2.1), and returns the grouped
    ``{libraries: [block, ...]}`` shape where each block carries its own
    ``library_slug`` (AC-D7.3).
    """
    slugs: list[str] = params["library_slugs"]
    title_ids = params.get("title_ids")

    async with httpx.AsyncClient(
        headers={"user-agent": USER_AGENT}, timeout=TIMEOUT_SECONDS
    ) as async_client:
        results = await asyncio.gather(
            *[_availability_one(slug, title_ids, client, async_client) for slug in slugs]
        )
    return {"libraries": list(results)}
