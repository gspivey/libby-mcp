"""lookup_library_slug tool — library-name → OverDrive slug lookup (FR-D5).

Given a library name or partial name, returns matching OverDrive slugs. The slug
directory is loaded once per TTL window and cached in-process
(:mod:`src.slug_directory`); matching is a case-insensitive substring over both
the display name and the slug.

Fact-finding only (NFR-5.1): the tool returns raw directory rows (slug + name)
with no ranking, recommendation, or advisory output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.slug_directory import SlugDirectory


async def handle(params: dict[str, Any], directory: "SlugDirectory") -> dict[str, Any]:
    """Return directory rows whose name or slug contains ``name`` (AC-D5.3/D5.6).

    Args:
        params: Tool arguments; ``name`` is a library name or partial name
            (case-insensitive). Already normalized to lowercase at the handler
            boundary, but re-normalized defensively here.
        directory: The in-process :class:`SlugDirectory` cache.

    Returns:
        ``{"query": <name>, "matches": [{"slug", "name"}, ...]}``. ``matches`` is
        an empty list on no hit (not an error, AC-D5.6). When the live directory
        is unreachable and the bundled fallback snapshot is used, a ``warning``
        field is added (AC-D5.10). ``{"error": ...}`` only when neither live,
        cached, nor fallback data is available (AC-D5.8).
    """
    name = str(params.get("name") or "").strip().lower()
    if not name:
        return {"error": "name is required"}

    try:
        entries = await directory.entries()
    except Exception:  # noqa: BLE001 — never crash the tool on a directory error
        return {"error": "library directory temporarily unavailable — try again"}

    if not entries:
        return {"error": "library directory temporarily unavailable — try again"}

    matches = [
        {"slug": e["slug"], "name": e["name"]}
        for e in entries
        if name in (e.get("name") or "").lower() or name in (e.get("slug") or "").lower()
    ]

    result: dict[str, Any] = {"query": name, "matches": matches}
    if directory.is_fallback():
        result["warning"] = (
            "live library directory was unreachable; results are from a bundled "
            "offline snapshot and may be out of date"
        )
    return result
