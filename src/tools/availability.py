"""get_availability tool — raw availability data retrieval.

Fetches copy/hold counts for specific titles via the Thunder API
library-scoped availability endpoint. Returns factual data only —
no recommendations, rankings, or strategic advice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.thunder_client import ThunderClient


def handle(params: dict[str, Any], client: "ThunderClient") -> dict[str, Any]:
    """Get availability data for specific titles at a library.

    Args:
        params: Must contain 'library_slug' and 'title_ids' (non-empty array).
        client: ThunderClient instance for API calls.

    Returns:
        Availability response: {library_slug, titles: [...]} or {error: "..."}.
    """
    library_slug = params.get("library_slug")
    title_ids = params.get("title_ids")

    # Validate required params
    if not library_slug:
        return {"error": "library_slug is required"}
    if title_ids is None:
        return {"error": "title_ids is required"}
    if not isinstance(title_ids, list) or len(title_ids) == 0:
        return {"error": "title_ids must be a non-empty array"}

    # Coerce all IDs to strings
    title_ids_str = [str(tid) for tid in title_ids]

    # Fetch availability from Thunder API
    response = client.get_availability(library_slug, title_ids_str)

    # Check for client-level errors
    if "error" in response:
        return response

    # Map response items to our schema
    items = response.get("items", [])
    items_by_id = {str(item.get("id", "")): item for item in items}

    titles = []
    for tid in title_ids_str:
        if tid in items_by_id:
            item = items_by_id[tid]
            titles.append(
                {
                    "title_id": tid,
                    "copies_owned": item.get("ownedCopies", 0),
                    "copies_available": item.get("availableCopies", 0),
                    "holds_count": item.get("holdsCount", 0),
                    "estimated_wait_days": item.get("estimatedWaitDays", 0),
                }
            )
        else:
            titles.append(
                {
                    "title_id": tid,
                    "error": "not_found",
                }
            )

    return {
        "library_slug": library_slug,
        "titles": titles,
    }
