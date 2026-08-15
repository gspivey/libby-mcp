"""get_deep_link tool — generates collision-proof OverDrive links.

Constructs a direct URL using the numeric title ID to avoid same-title
collisions (e.g., two different books both called "Dragon Rider").
No API call needed — pure URL construction.
"""

from __future__ import annotations

from typing import Any


def handle(params: dict[str, Any]) -> dict[str, Any]:
    """Generate a deep link URL for a specific title at a library.

    Args:
        params: Must contain 'title_id' and 'library_slug'.

    Returns:
        Deep Link Object: {url, title_id, library_slug} or {error: "..."}.
    """
    title_id = params.get("title_id")
    library_slug = params.get("library_slug")

    # Validate required params
    if not title_id and title_id != 0:
        return {"error": "title_id is required"}
    if not library_slug:
        return {"error": "library_slug is required"}

    # Coerce title_id to string (accepts string or integer)
    title_id_str = str(title_id)

    url = f"https://{library_slug}.overdrive.com/media/{title_id_str}"

    return {
        "url": url,
        "title_id": title_id_str,
        "library_slug": library_slug,
    }
