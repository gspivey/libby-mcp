"""search_titles tool — catalog search with server and client-side filtering.

Builds Thunder API query params from MCP input, fetches results, applies
client-side filters (BISAC, duration), and normalizes the response into
the Title Object schema.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from src.thunder_client import ThunderClient

logger = logging.getLogger(__name__)

# Maximum number of libraries accepted in a single multi-library fan-out (AC-B1.12).
MAX_LIBRARIES = 10

# Format mapping: MCP value -> Thunder API value
FORMAT_MAP = {
    "audiobook": "audiobook-overdrive",
    "ebook": "ebook-overdrive",
}

# Maturity level mapping: MCP value -> Thunder API value
MATURITY_MAP = {
    "general": "generalcontent",
    "juvenile": "juvenile",
    "youngadult": "youngadult",
    "adultonly": "adultonly",
}

# Narrowing filters that indicate a valid empty result (not a bad slug)
NARROWING_FILTERS = {"subjects", "creator", "query", "bisac", "series_id"}


def handle(params: dict[str, Any], client: "ThunderClient") -> dict[str, Any]:
    """Search a library's catalog with optional filters.

    Args:
        params: MCP tool arguments. 'library_slug' is required.
        client: ThunderClient instance for API calls.

    Returns:
        Search response: {library_slug, titles, total_items, page, total_pages,
        filtered_count}. Includes a `facets` object only when
        params["include_facets"] is truthy (FR-A4). Returns {error: "..."} on
        validation/API failure.
    """
    library_slug = params.get("library_slug")
    if not library_slug:
        return {"error": "library_slug is required"}

    # Build Thunder API query params
    thunder_params = _build_thunder_params(params)

    # Call Thunder API
    response = client.search(library_slug, thunder_params)

    result = _build_result_block(library_slug, params, response)

    # Exact-mode paging (FR-D6 / AC-D6.7): the exact filter runs client-side over
    # the fetched page. If page 1 yielded no exact match while the catalog has
    # more results (total_items > 0), fetch further pages — up to a 3-page / 300-
    # item cap — and re-filter, stopping as soon as a match is found. This catches
    # an exact title that sits just beyond page 1 without an unbounded walk.
    if params.get("exact") and params.get("query") and "error" not in result:
        result = _exact_page_through(library_slug, params, client, result)

    return result


# Maximum pages fetched when resolving an exact-title match (AC-D6.7): 3 pages at
# perPage=100 == 300 items.
EXACT_MAX_PAGES = 3


def _exact_page_through(
    library_slug: str,
    params: dict[str, Any],
    client: "ThunderClient",
    first_result: dict[str, Any],
) -> dict[str, Any]:
    """Fetch additional pages to find an exact-title match beyond page 1 (AC-D6.7).

    ``first_result`` is the already-built page-1 block. If it already has a match
    (or the catalog has no further items), it is returned unchanged. Otherwise
    pages 2..``EXACT_MAX_PAGES`` are fetched and re-filtered, returning the first
    page whose block contains a match, or the last fetched (empty) block if the
    cap is reached with no match.
    """
    if first_result.get("titles"):
        return first_result
    if first_result.get("total_items", 0) <= 0:
        return first_result

    last_block = first_result
    for page in range(2, EXACT_MAX_PAGES + 1):
        page_params = {**params, "page": page}
        response = client.search(library_slug, _build_thunder_params(page_params))
        block = _build_result_block(library_slug, page_params, response)
        if "error" in block:
            # A transient error on a later page: keep the page-1 (empty) block
            # rather than surfacing the later-page error.
            return first_result
        if block.get("titles"):
            return block
        last_block = block
    return last_block


def _build_result_block(
    library_slug: str, params: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    """Shape a single library's Thunder response into a result block.

    Shared by the single-library :func:`handle` path and the per-library
    :func:`_search_one` fan-out path so both produce identical shapes. Returns
    ``{error: ...}`` on client-level error or the invalid-slug heuristic; the
    caller decides whether to raise (single) or embed the error in a per-library
    block (multi).
    """
    # Check for client-level errors (includes the FR-A2 404 message)
    if "error" in response:
        return response

    # Invalid slug detection (best-effort heuristic)
    has_narrowing_filters = bool(NARROWING_FILTERS & set(params.keys()))
    items = response.get("items", [])
    total_items = response.get("totalItems", 0)

    if not items and total_items == 0 and not has_narrowing_filters:
        return {"error": f"Library '{library_slug}' not found or unavailable"}

    # Apply client-side filters
    bisac_filter = params.get("bisac")
    under_hours = params.get("under_hours")
    filtered_items = _apply_client_filters(items, bisac_filter, under_hours)

    # Normalize items into Title Objects. Items with a missing/None id are
    # dropped (AC-B3.3): _normalize_item returns None for them so they never
    # surface with a malformed `.../media/` or `.../media/None` link.
    titles = [
        normalized
        for item in filtered_items
        if (normalized := _normalize_item(item, library_slug)) is not None
    ]

    # Exact-title filter (FR-D6): when exact=True and a query is present, keep
    # only titles whose normalized title equals the normalized query. Applied
    # here so it covers both the single-library and multi-library paths
    # (AC-D6.6); filtered_count below reflects the post-exact count.
    if params.get("exact") and params.get("query"):
        q = str(params["query"]).strip().lower()
        titles = [t for t in titles if str(t.get("title", "")).strip().lower() == q]

    # Build pagination metadata (from server response, before client filtering)
    links = response.get("links", {})
    last_page = links.get("last", {}).get("page", 1)
    current_page = links.get("self", {}).get("page", 1)

    result: dict[str, Any] = {
        "library_slug": library_slug,
        "titles": titles,
        "total_items": total_items,
        "page": current_page,
        "total_pages": last_page,
        "filtered_count": len(titles),
    }
    # Facets are opt-in (FR-A4): omit the key entirely unless requested.
    if params.get("include_facets"):
        result["facets"] = response.get("facets", {})
    return result


def validate_library_slugs(slugs: list[str]) -> str | None:
    """Validate a ``library_slugs`` list for multi-library fan-out (FR-B1).

    Returns an error message string if invalid, else ``None``. Does NOT mutate
    the input; callers obtain the cleaned/deduped list from
    :func:`dedupe_library_slugs` after this passes.

    Checks run in order (AC-B1.9–B1.12):
    1. empty list -> ``library_slugs must not be empty``
    2. any blank/whitespace entry -> ``library_slugs contains invalid entry: empty slug``
    3. (dedupe happens separately, see :func:`dedupe_library_slugs`)
    4. more than 10 (after dedupe) -> ``library_slugs exceeds maximum of 10 libraries``
    """
    if not slugs:
        return "library_slugs must not be empty"
    for entry in slugs:
        if not isinstance(entry, str) or entry.strip() == "":
            return "library_slugs contains invalid entry: empty slug"
    if len(dedupe_library_slugs(slugs)) > MAX_LIBRARIES:
        return "library_slugs exceeds maximum of 10 libraries"
    return None


def dedupe_library_slugs(slugs: list[str]) -> list[str]:
    """Deduplicate slugs preserving first-seen order (AC-B1.11)."""
    seen: set[str] = set()
    out: list[str] = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


async def _search_one(
    slug: str,
    params: dict[str, Any],
    client: "ThunderClient",
    async_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Search a single library during fan-out, never raising (AC-B1.5).

    Any failure (bad slug 404, timeout, unexpected error) is normalized into a
    block ``{library_slug, error}`` so one bad library does not poison the
    whole ``asyncio.gather``. On success returns the standard result block.
    """
    thunder_params = _build_thunder_params(params)
    try:
        response = await client.search_async(slug, thunder_params, async_client)
    except Exception as e:  # defensive: search_async already maps known errors
        logger.warning(
            "fan_out_library_error",
            extra={"library_slug": slug, "error": str(e)},
        )
        return {"library_slug": slug, "error": str(e)}

    block = _build_result_block(slug, params, response)
    if "error" in block:
        # Per-library failure: tag with the slug and log for observability
        # (NFR-5.1) without leaking into other libraries' blocks.
        logger.warning(
            "fan_out_library_error",
            extra={"library_slug": slug, "error": block["error"]},
        )
        return {"library_slug": slug, "error": block["error"]}
    return block


async def _fan_out(
    slugs: list[str], params: dict[str, Any], client: "ThunderClient"
) -> list[dict[str, Any]]:
    """Run per-library searches concurrently with ``asyncio.gather`` (NFR-2.1).

    The ``AsyncClient`` is created here (per fan-out) so its connection pool is
    bound to the current event loop and survives warm Lambda invocations.
    """
    from src.thunder_client import TIMEOUT_SECONDS, USER_AGENT

    async with httpx.AsyncClient(
        headers={"user-agent": USER_AGENT}, timeout=TIMEOUT_SECONDS
    ) as async_client:
        tasks = [_search_one(slug, params, client, async_client) for slug in slugs]
        return await asyncio.gather(*tasks)


async def handle_multi(params: dict[str, Any], client: "ThunderClient") -> dict[str, Any]:
    """Multi-library fan-out search (FR-B1).

    Assumes ``params['library_slugs']`` has already been validated and
    deduplicated by the caller (the handler wrapper). Returns the grouped
    response ``{libraries: [block, ...]}`` where each block carries its own
    ``library_slug`` (AC-B1.4).
    """
    slugs: list[str] = params["library_slugs"]
    results = await _fan_out(slugs, params, client)
    return {"libraries": results}


def _build_thunder_params(params: dict[str, Any]) -> dict[str, Any]:
    """Map MCP params to Thunder API query params."""
    thunder: dict[str, Any] = {}

    exact = bool(params.get("exact"))

    # subjects -> repeated subject param (coerced to strings)
    if "subjects" in params:
        thunder["subject"] = [str(s) for s in params["subjects"]]

    # format -> format with value transform
    if "format" in params:
        thunder["format"] = FORMAT_MAP.get(params["format"], params["format"])

    # available=true -> showOnlyAvailable=true; omitted -> availableFirst=true.
    # In exact mode (FR-D6 / AC-D6.7) we never add availableFirst: an exact match
    # must not be pushed off page 1 by availability ordering — relevance ordering
    # (set below) keeps the exact title near the front.
    if params.get("available") is True:
        thunder["showOnlyAvailable"] = "true"
    elif not exact:
        thunder["availableFirst"] = "true"

    # creator -> creator (direct)
    if "creator" in params:
        thunder["creator"] = params["creator"]

    # bisac -> bisacCode (server-side, belt-and-suspenders)
    if "bisac" in params:
        thunder["bisacCode"] = params["bisac"]

    # series_id -> seriesId (direct)
    if "series_id" in params:
        thunder["seriesId"] = str(params["series_id"])

    # maturity_level -> maturityLevel (with mapping)
    if "maturity_level" in params:
        thunder["maturityLevel"] = MATURITY_MAP.get(
            params["maturity_level"], params["maturity_level"]
        )

    # sort_by -> sortBy (direct). Exact mode forces relevance ordering so the
    # exact title ranks near the front rather than being ordered by availability
    # (AC-D6.7); it overrides any caller-supplied sort_by.
    if exact:
        thunder["sortBy"] = "relevance"
    elif "sort_by" in params:
        thunder["sortBy"] = params["sort_by"]

    # query -> query (direct)
    if "query" in params:
        thunder["query"] = params["query"]

    # per_page -> perPage (clamp to max 100, default 24). Exact mode uses the
    # max page size (100) so a single fetch covers more candidates (AC-D6.7).
    if exact:
        per_page = 100
    else:
        per_page = min(int(params.get("per_page", 24)), 100)
    thunder["perPage"] = str(per_page)

    # page -> page (default 1)
    thunder["page"] = str(params.get("page", 1))

    return thunder


def _apply_client_filters(
    items: list[dict[str, Any]], bisac_filter: str | None, under_hours: float | None
) -> list[dict[str, Any]]:
    """Apply client-side filters to Thunder API response items."""
    filtered = items

    if bisac_filter:
        filtered = [
            item for item in filtered if _passes_bisac(item.get("bisacCodes", []), bisac_filter)
        ]

    if under_hours is not None:
        filtered = [item for item in filtered if _passes_duration(item, under_hours)]

    return filtered


def _passes_bisac(item_codes: list[str], keep_code: str) -> bool:
    """True if item has the specified BISAC code."""
    return keep_code in item_codes


def _passes_duration(item: dict[str, Any], max_hours: float) -> bool:
    """True if item should be INCLUDED based on duration filter.

    Items without a duration (e.g., ebooks with empty string) are INCLUDED.
    Only items with a parseable duration exceeding max_hours are excluded.
    """
    hours = _get_hours(item)
    if hours is None:
        return True  # No duration = include (ebooks, etc.)
    return hours <= max_hours


def _get_hours(item: dict[str, Any]) -> float | None:
    """Extract duration in hours from item's formats array.

    Scans all formats[] entries for the first non-empty duration field.
    Parses HH:MM:SS to decimal hours.
    """
    formats = item.get("formats", [])
    for fmt in formats:
        duration_str = fmt.get("duration", "")
        if duration_str and duration_str.strip():
            return _hms_to_hours(duration_str)
    return None


def _hms_to_hours(duration_str: str) -> float | None:
    """Convert HH:MM:SS to decimal hours. Returns None if unparseable."""
    if not duration_str or not duration_str.strip():
        return None
    try:
        parts = [int(p) for p in duration_str.strip().split(":")]
        if not parts or len(parts) > 3:
            return None
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts[-3:]
        return round(h + m / 60 + s / 3600, 1)
    except (ValueError, TypeError):
        return None


def _holds_per_copy(holds: int, owned: int) -> float | None:
    """Holds-to-copies ratio, computed without a divide-by-zero crash (AC-B2.2).

    - ``owned > 0``: ``round(holds / owned, 1)``.
    - ``owned == 0`` (or falsy) and ``holds == 0``: ``0.0`` (no contention).
    - ``owned == 0`` (or falsy) and ``holds > 0``: ``None`` (ratio not
      meaningful — the library has holds but owns nothing).
    """
    if owned:
        return round(holds / owned, 1)
    # owned == 0 (or falsy)
    return 0.0 if not holds else None


def _to_float(value: Any) -> float | None:
    """Coerce a numeric value (or numeric string) to float, else ``None`` (AC-C1.4).

    Used for ``series_position`` from the Thunder ``readingOrder`` field, which is
    a string in the API response (e.g. ``"1"`` or ``"1.5"``). Missing, empty, or
    non-numeric values yield ``None``.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _extract_series(item: dict[str, Any]) -> tuple[str | None, float | None]:
    """Extract ``(series_name, series_position)`` from a Thunder item (FR-C1).

    Field paths verified against ``docs/references/thunder_openapi_v1.json``
    (``MediaDetailsResponse``): the item carries a bare-string ``series`` (the
    series name) plus a ``detailedSeries`` object with ``seriesName`` and
    ``readingOrder``. The spec originally assumed a nested ``series`` object with
    ``name``/``readingOrder``; this reader tolerates both shapes so it is robust
    to either the real API or the spec's assumed shape (AC-C1.1–C1.4).

    When there is no series information both values are ``None`` (keys still
    present on the title, set by the caller).
    """
    detailed = item.get("detailedSeries")
    if not isinstance(detailed, dict):
        detailed = {}

    series = item.get("series")
    # series name: prefer detailedSeries.seriesName; fall back to a bare-string
    # `series`, or a nested `series.name` object (spec's original assumption).
    series_name = detailed.get("seriesName")
    if series_name is None:
        if isinstance(series, str):
            series_name = series or None
        elif isinstance(series, dict):
            series_name = series.get("name")

    # reading order: detailedSeries.readingOrder, else a nested series.readingOrder.
    raw_order = detailed.get("readingOrder")
    if raw_order is None and isinstance(series, dict):
        raw_order = series.get("readingOrder")
    series_position = _to_float(raw_order)

    return series_name, series_position


def _extract_narrators(item: dict[str, Any]) -> list[str]:
    """Extract narrator names from the Thunder ``creators`` array (FR-C2).

    Verified against the OpenAPI reference (``CreatorResponse`` has ``name`` and
    ``role``): there is no top-level ``narrators`` array; narrators are
    ``creators[]`` entries whose ``role`` is ``"Narrator"`` (case-insensitive).
    Entries that are not ``{name, role}`` objects (e.g. bare strings) are
    skipped, so a malformed ``creators`` list never crashes (AC-C2.1/C2.4).
    """
    creators = item.get("creators") or []
    if not isinstance(creators, list):
        return []
    return [
        c["name"]
        for c in creators
        if isinstance(c, dict)
        and isinstance(c.get("role"), str)
        and c["role"].strip().lower() == "narrator"
        and c.get("name")
    ]


def _derive_abridged(primary_fmt: dict[str, Any], edition: str | None) -> bool | None:
    """Honestly derive ``is_abridged`` or return ``None`` (FR-D4).

    Priority (AC-D4.1/D4.2):

    1. A real boolean ``isAbridged`` on the primary format entry wins.
    2. Else an "unabridged"/"abridged" token in the ``edition`` string — checked
       in that order so an "Unabridged" edition is never mis-read as abridged.
    3. Else ``None`` (unknown) — the value is never fabricated as ``False``.

    The field is absent from the Thunder OpenAPI reference, so against the live
    API the result comes from ``edition`` when that carries a signal, else
    ``None`` (AC-D4.3 keeps the field with an honest ``None`` default).
    """
    raw = primary_fmt.get("isAbridged")
    if isinstance(raw, bool):
        return raw
    if isinstance(edition, str):
        e = edition.strip().lower()
        if "unabridged" in e:
            return False
        if "abridged" in e:
            return True
    return None


def _normalize_item(item: dict[str, Any], library_slug: str) -> dict[str, Any] | None:
    """Normalize a Thunder API item into the Title Object schema.

    Returns ``None`` for an item with a missing or ``None`` id (AC-B3.3): such
    an item would produce a broken ``.../media/`` or ``.../media/None`` link, so
    it is dropped (with a logged warning) rather than emitted. Callers filter
    out the ``None`` results before building the ``titles`` list.
    """
    raw_id = item.get("id")
    if raw_id is None or str(raw_id).strip() == "":
        logger.warning(
            "search_item_missing_id",
            extra={"library_slug": library_slug, "title": item.get("title", "")},
        )
        return None
    item_id = str(raw_id)

    # Determine availability: isAvailable AND availableCopies >= 1
    is_available = bool(item.get("isAvailable")) and (item.get("availableCopies", 0) or 0) >= 1

    # Determine format from formats array
    formats = item.get("formats", [])
    primary_fmt = formats[0] if formats and isinstance(formats[0], dict) else {}
    format_value = ""
    if primary_fmt:
        fmt_id = primary_fmt.get("id", "")
        if "audiobook" in fmt_id:
            format_value = "audiobook"
        elif "ebook" in fmt_id:
            format_value = "ebook"
        else:
            format_value = fmt_id

    # Get hours for audiobooks
    hours = _get_hours(item)

    # Content enrichment (Group C). Field paths verified against the OpenAPI
    # reference; see _extract_series / _extract_narrators for the shape notes.
    series_name, series_position = _extract_series(item)
    narrator = _extract_narrators(item)

    # publisher: IdNamePair -> name string; None when absent or a bare string
    # (AC-C2.2/C2.4).
    pub = item.get("publisher")
    publisher = pub.get("name") if isinstance(pub, dict) else None

    # edition: plain string or None (AC-C2.3).
    raw_edition = item.get("edition")
    edition = raw_edition if isinstance(raw_edition, str) else None

    # is_abridged (FR-D4): honest derivation. A real format-entry `isAbridged`
    # boolean wins (AC-C3.3 / AC-D4.1); else an abridged/unabridged token in the
    # `edition` string; else None (unknown) — never a fabricated False
    # (AC-D4.2/D4.4). The field is not in the OpenAPI reference, so on the live
    # API it resolves from `edition` when that carries a signal, else None.
    is_abridged = _derive_abridged(primary_fmt, edition)

    # Folded availability signals (FR-B2): derived from the search item itself,
    # no extra per-title availability call (AC-B2.4 / NFR-2.3).
    holds_count = item.get("holdsCount", 0) or 0
    owned_copies = item.get("ownedCopies", 0) or 0
    holds_per_copy = _holds_per_copy(holds_count, owned_copies)
    # estimatedWaitDays is not guaranteed on the /media search response; when
    # absent the value is None (unknown), never 0 (AC-B2.3 / AC-B2.4). D3
    # correction (AC-D3.3/D3.4): when a copy is available (available_copies > 0),
    # the wait is forced to 0 regardless of the noisy raw value; otherwise the
    # raw value (or None when absent) passes through — keying on copy counts, not
    # isAvailable alone.
    available_copies = item.get("availableCopies", 0) or 0
    if available_copies > 0:
        estimated_wait_days: Any = 0
    else:
        estimated_wait_days = item.get("estimatedWaitDays")

    return {
        "id": item_id,
        "title": item.get("title", ""),
        "creator": item.get("firstCreatorName", ""),
        "format": format_value,
        "available": is_available,
        "available_copies": item.get("availableCopies", 0) or 0,
        "owned_copies": owned_copies,
        "holds_count": holds_count,
        "holds_per_copy": holds_per_copy,
        "estimated_wait_days": estimated_wait_days,
        "hours": hours,
        "is_abridged": is_abridged,
        "bisac_codes": item.get("bisacCodes", []),
        "series_name": series_name,
        "series_position": series_position,
        "narrator": narrator,
        "publisher": publisher,
        "edition": edition,
        "link": f"https://{library_slug}.overdrive.com/media/{item_id}",
    }
