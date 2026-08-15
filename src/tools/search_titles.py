"""search_titles tool — catalog search with server and client-side filtering.

Builds Thunder API query params from MCP input, fetches results, applies
client-side filters (BISAC, duration), and normalizes the response into
the Title Object schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.thunder_client import ThunderClient

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
        Search response: {titles, total_items, page, total_pages, filtered_count, facets}
        or {error: "..."} on validation/API failure.
    """
    library_slug = params.get("library_slug")
    if not library_slug:
        return {"error": "library_slug is required"}

    # Build Thunder API query params
    thunder_params = _build_thunder_params(params)

    # Call Thunder API
    response = client.search(library_slug, thunder_params)

    # Check for client-level errors
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

    # Normalize items into Title Objects
    titles = [_normalize_item(item, library_slug) for item in filtered_items]

    # Build pagination metadata (from server response, before client filtering)
    links = response.get("links", {})
    last_page = links.get("last", {}).get("page", 1)
    current_page = links.get("self", {}).get("page", 1)

    return {
        "titles": titles,
        "total_items": total_items,
        "page": current_page,
        "total_pages": last_page,
        "filtered_count": len(titles),
        "facets": response.get("facets", {}),
    }


def _build_thunder_params(params: dict[str, Any]) -> dict[str, Any]:
    """Map MCP params to Thunder API query params."""
    thunder: dict[str, Any] = {}

    # subjects -> repeated subject param (coerced to strings)
    if "subjects" in params:
        thunder["subject"] = [str(s) for s in params["subjects"]]

    # format -> format with value transform
    if "format" in params:
        thunder["format"] = FORMAT_MAP.get(params["format"], params["format"])

    # available=true -> showOnlyAvailable=true; omitted -> availableFirst=true
    if params.get("available") is True:
        thunder["showOnlyAvailable"] = "true"
    else:
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

    # sort_by -> sortBy (direct)
    if "sort_by" in params:
        thunder["sortBy"] = params["sort_by"]

    # query -> query (direct)
    if "query" in params:
        thunder["query"] = params["query"]

    # per_page -> perPage (clamp to max 100, default 24)
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


def _normalize_item(item: dict[str, Any], library_slug: str) -> dict[str, Any]:
    """Normalize a Thunder API item into the Title Object schema."""
    item_id = str(item.get("id", ""))

    # Determine availability: isAvailable AND availableCopies >= 1
    is_available = bool(item.get("isAvailable")) and (item.get("availableCopies", 0) or 0) >= 1

    # Determine format from formats array
    formats = item.get("formats", [])
    format_value = ""
    if formats:
        fmt_id = formats[0].get("id", "")
        if "audiobook" in fmt_id:
            format_value = "audiobook"
        elif "ebook" in fmt_id:
            format_value = "ebook"
        else:
            format_value = fmt_id

    # Get hours for audiobooks
    hours = _get_hours(item)

    return {
        "id": item_id,
        "title": item.get("title", ""),
        "creator": item.get("firstCreatorName", ""),
        "format": format_value,
        "available": is_available,
        "available_copies": item.get("availableCopies", 0) or 0,
        "owned_copies": item.get("ownedCopies", 0) or 0,
        "holds_count": item.get("holdsCount", 0) or 0,
        "hours": hours,
        "bisac_codes": item.get("bisacCodes", []),
        "link": f"https://{library_slug}.overdrive.com/media/{item_id}",
    }
