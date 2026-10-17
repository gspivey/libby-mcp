"""Library-slug normalization (FR-D2).

A single mechanical helper applied at the input boundary (the handler tool
wrappers) so every slug reaching a Thunder URL, a response field, a deep link,
or an error message is already lowercased and trimmed. Purely mechanical — no
per-library special-casing (NFR-6.1).
"""

from __future__ import annotations


def normalize_slug(slug: str) -> str:
    """Return ``slug`` trimmed of surrounding whitespace and lowercased (AC-D2.1)."""
    return slug.strip().lower()
