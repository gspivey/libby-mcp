# Requirements: Libby MCP Server v2

## Overview

libby-mcp v1 is deployed and working — an MCP server exposing the OverDrive
Thunder API as three fact-finding tools (`search_titles`, `get_availability`,
`get_deep_link`). The tools return raw catalog data; the LLM caller does all
reasoning (no recommendations or advisory logic in the server).

v2 is a set of 10 improvements in three groups:

- **Group A — Targeted fixes** (slug examples, 404 errors, slug echo, facets opt-in)
- **Group B — Multi-library search + hold intelligence** (fan-out search, folded
  availability, retire `get_deep_link`)
- **Group C — Content enrichment** (series, narrator/publisher/edition, duration
  + abridged)

Group A and Group B are mostly independent, but Group B depends on Group A (the
multi-library fan-out reuses the FR-A2 404 message and the FR-A4 facets opt-in).
Group C depends on Group B's media/enrichment work (the enriched `_normalize_item`
output). Suggested order: Group A, then Group B, then Group C.

## Reference

- `#[[file:.kiro/specs/libby-mcp-v2/context/search_titles.py]]` — current search tool implementation
- `#[[file:.kiro/specs/libby-mcp-v2/context/availability.py]]` — current availability tool implementation
- `#[[file:.kiro/specs/libby-mcp-v2/context/thunder_client.py]]` — current HTTP client (`_normalize_item` lives in `search_titles.py`; error handling in `thunder_client._execute_get`)
- `#[[file:.kiro/specs/libby-mcp-v2/context/handler.py]]` — current Lambda handler with all tool registrations
- `#[[file:docs/references/thunder_api_reference.md]]` — endpoints, params, response shapes
- `#[[file:docs/references/mcp_spec_excerpt.md]]` — MCP 2025-06-18 protocol details

## Verified Library Slugs

| Slug | Library |
|------|---------|
| `lcpl` | Loudoun County Public Library |
| `fairfax` | Fairfax County Public Library |
| `nypl` | New York Public Library |

v1 used `fairfaxcounty` as an example in tool descriptions; the verified slug is
`fairfax`. All v2 examples use the verified slugs above.

## Scope Note

v2 preserves the v1 fact-finding contract: tools return raw data, no
recommendations, rankings, or strategic advice. All existing v1 acceptance
criteria that are not explicitly changed below remain in force.

---

## Group A — Quick Fixes

### FR-A1: Correct Library Slug Examples

**Description:** Update every tool description and schema example that references
`fairfaxcounty` to use the verified slug `fairfax`.

**Acceptance Criteria:**
- AC-A1.1: The `search_titles` tool description/schema uses `fairfax` (not `fairfaxcounty`) in its `library_slug` example.
- AC-A1.2: The `get_availability` tool description/schema uses `fairfax` in any slug example.
- AC-A1.3: No tool description, parameter description, or schema example in the shipped server references `fairfaxcounty`.
- AC-A1.4: The canonical example slug list is `lcpl`, `fairfax`, `nypl`.

### FR-A2: Human-Readable 404 Errors

**Description:** When OverDrive/Thunder returns HTTP 404, the server returns a
message that identifies the bad slug and suggests valid ones, instead of the
generic `OverDrive service error (HTTP 404)`.

**Acceptance Criteria:**
- AC-A2.1: When a Thunder API request returns HTTP 404, the client returns the error string: `unknown library slug: <slug> — check the slug is correct (e.g. lcpl, fairfax, nypl)` where `<slug>` is the library slug from the failing request.
- AC-A2.2: The 404 message is produced for both the search endpoint and the availability endpoint (any request carrying a library slug).
- AC-A2.3: Non-404 HTTP errors retain their existing behavior (e.g. `OverDrive service error (HTTP <status>)` for other 4xx/5xx).
- AC-A2.4: Timeout and unexpected-error paths are unchanged.
- AC-A2.5: The slug value is interpolated safely (no formatting crash when the slug is empty or contains unusual characters).

### FR-A3: Echo library_slug in Every Tool Response

**Description:** Every tool response includes a `library_slug` field so callers
can correlate results with the queried library (important once multi-library
fan-out exists).

**Acceptance Criteria:**
- AC-A3.1: The `search_titles` single-library response includes a top-level `library_slug` field equal to the queried slug.
- AC-A3.2: The `get_availability` response includes `library_slug` (already present in v1 — must be preserved).
- AC-A3.3: `library_slug` is present on success responses. Error responses (which raise `ToolError`) are exempt.
- AC-A3.4: For the multi-library response shape (FR-B1), each per-library result block carries its own `library_slug` (see AC-B1.4).

### FR-A4: Facets Opt-In

**Description:** Add an `include_facets: bool = False` parameter to
`search_titles`. By default, facets are omitted from the response to reduce
payload size. When `include_facets=True`, the full facets object is returned.

**Acceptance Criteria:**
- AC-A4.1: `search_titles` accepts an optional `include_facets` boolean parameter, defaulting to `False`.
- AC-A4.2: When `include_facets` is `False` or omitted, the response does NOT contain a `facets` key at all (omitted, not empty).
- AC-A4.3: When `include_facets` is `True`, the response contains a `facets` object populated from the Thunder API response (same content as v1's always-on facets).
- AC-A4.4: The `include_facets` default is a behavior change from v1 (v1 always returned facets); this is intentional and documented in the tool description.

---

## Group B — Multi-Library Search + Hold Intelligence

### FR-B1: Multi-Library Fan-Out Search

**Description:** Add a `library_slugs: list[str]` parameter to `search_titles`
alongside the existing `library_slug`. Exactly one of the two must be provided.
When `library_slugs` is provided, the server fans out the search across all slugs
in parallel (asyncio.gather) and returns results grouped by `library_slug`.

**Acceptance Criteria:**
- AC-B1.1: `search_titles` accepts an optional `library_slugs` parameter (array of strings) in addition to the existing `library_slug` (string).
- AC-B1.2: Exactly one of `library_slug` / `library_slugs` must be provided. If both are provided, return a validation error: `provide either library_slug or library_slugs, not both`. If neither is provided, return a validation error: `library_slug or library_slugs is required`.
- AC-B1.3: When `library_slugs` is provided, the server performs the search against each slug in parallel using `asyncio.gather`.
- AC-B1.4: The multi-library response groups results by library slug. Each group is keyed/tagged by `library_slug` and carries that library's own `titles`, pagination metadata, and (if requested) `facets`.
- AC-B1.5: A per-library failure (e.g. a bad slug returning 404, or a timeout) does not fail the entire request. The failing library's block carries an `error` field with the human-readable message (FR-A2); other libraries still return results.
- AC-B1.6: `search_titles` with `library_slugs=['lcpl','fairfax']` returns results from both libraries in a single tool call. (Primary acceptance criterion from the brief.)
- AC-B1.7: When `library_slug` (singular) is provided, the single-library response shape is unchanged from v1 except for the additive fields in FR-A3, FR-A4, FR-B2, and Group C.
- AC-B1.8: `library_slugs` with a single entry is permitted and returns the grouped (multi-library) response shape.
- AC-B1.9: An empty `library_slugs` list returns a validation error: `library_slugs must not be empty`.
- AC-B1.10: Entries that are empty strings or whitespace-only are rejected with a validation error: `library_slugs contains invalid entry: empty slug`.
- AC-B1.11: Duplicate slugs are deduplicated before fan-out (order preserved, later occurrences removed), so each library is queried at most once.
- AC-B1.12: `library_slugs` is limited to a maximum of 10 entries; exceeding it returns a validation error: `library_slugs exceeds maximum of 10 libraries`.

### FR-B2: Fold Availability Signals Into Search Results

**Description:** Each title in search results gains `holds_per_copy` and
`estimated_wait_days`, so callers no longer need a separate availability call to
reason about wait times.

**Acceptance Criteria:**
- AC-B2.1: Each title object includes `holds_per_copy`: `holdsCount / ownedCopies` rounded to 1 decimal place.
- AC-B2.2: `holds_per_copy` is computed without a divide-by-zero crash and distinguishes "no contention" from "holds against zero owned copies":
  - `ownedCopies > 0`: `holds_per_copy` is `round(holdsCount / ownedCopies, 1)`.
  - `ownedCopies == 0` (or missing) AND `holdsCount == 0`: `holds_per_copy` is `0.0` (no contention).
  - `ownedCopies == 0` (or missing) AND `holdsCount > 0`: `holds_per_copy` is `None` (the ratio is not meaningful — the library has holds but owns nothing).
- AC-B2.3: Each title object includes `estimated_wait_days`, sourced from the OverDrive `estimatedWaitDays` field. When the field is absent from the search response the value is `None` (unknown), **not** `0` (a `0` would wrongly imply "no wait").
- AC-B2.4: `holds_per_copy` is derived from `holdsCount`/`ownedCopies` already present in the `/media` search response — no extra per-title availability call is made. `estimatedWaitDays` has only been confirmed on the `/availability` endpoint and is not guaranteed to appear on the search response; v2 does not make an extra call to fill it, so `estimated_wait_days` is `None` when the search item lacks it (callers treat `None` as unknown and may call `get_availability` for a confirmed estimate). Either way both fields are always present as keys on every title.
- AC-B2.5: The fold-in does not break existing fields (`available`, `available_copies`, `owned_copies`, `holds_count`, `hours`, etc.).

### FR-B3: Retire get_deep_link

**Description:** Remove the `get_deep_link` tool. The `link` field is already
present in search results, making the standalone tool redundant.

**Acceptance Criteria:**
- AC-B3.1: The `get_deep_link` tool is no longer registered with the MCP server and does not appear in `tools/list`.
- AC-B3.2: Every title object in `search_titles` results includes a non-empty `link` field in the format `https://{library_slug}.overdrive.com/media/{id}`.
- AC-B3.3: Every title in the results has a well-formed, non-empty `link`; `link` is never `null`, empty, or built from a missing id (e.g. `.../media/` or `.../media/None`). An item with no `id` (missing or `None`) is excluded from the results and a warning is logged naming the dropped item and its library slug; it is never emitted with a malformed link. Thus no caller needs a separate deep-link tool.
- AC-B3.4: The `src/tools/deep_link.py` module and its tests are removed or retired; no dangling import of `deep_link` remains in `handler.py`.
- AC-B3.5: Removing the tool does not break the remaining two tools (`search_titles`, `get_availability`).

---

## Group C — Content Enrichment

> Group C depends on Group B's media/enrichment work. The enriched fields are
> added to the same `_normalize_item` output used by (multi-library) search.

### FR-C1: Series Metadata

**Description:** Add series information to each title result.

**Acceptance Criteria:**
- AC-C1.1: Each title object includes `series_name: str | None`, sourced from the OverDrive series object's `name` field.
- AC-C1.2: Each title object includes `series_position: float | None`, sourced from the OverDrive series object's `readingOrder` field.
- AC-C1.3: When a title has no series object, both `series_name` and `series_position` are `None` (present as keys with null values).
- AC-C1.4: `series_position` is coerced to a float when the source value is numeric; non-numeric or missing reading order yields `None`.

### FR-C2: Narrator, Publisher, Edition

**Description:** Add narrator, publisher, and edition metadata to each title
result.

**Acceptance Criteria:**
- AC-C2.1: Each title object includes `narrator: list[str]`, sourced from the OverDrive `narrators` array (each entry's name). Empty list when absent.
- AC-C2.2: Each title object includes `publisher: str | None`, sourced from the OverDrive `publisher.name` field. `None` when absent.
- AC-C2.3: Each title object includes `edition: str | None`, sourced from the OverDrive `edition` field. `None` when absent.
- AC-C2.4: Missing or malformed source fields never crash normalization; they resolve to the documented default (`[]` or `None`).

### FR-C3: Duration + Abridged

**Description:** Verify duration is always returned, and add an abridged flag.

**Acceptance Criteria:**
- AC-C3.1: Each title object always includes the `hours` field (float for audiobooks with a parseable duration, `None` otherwise — unchanged v1 behavior, verified by test).
- AC-C3.2: Each title object includes `is_abridged: bool | None`, sourced from the OverDrive `isAbridged` field on the format. `None` when the field is absent (unknown), not `False`.
- AC-C3.3: `is_abridged` reads from the format entry used for duration/format detection (consistent with how `hours`/`format` are derived).
- AC-C3.4: Missing `isAbridged` resolves to `None` without crashing normalization.

---

## Non-Functional Requirements

### NFR-1: Backward Compatibility

- NFR-1.1: All v2 title-object additions are additive. No existing field is renamed or removed from the title object.
- NFR-1.2: The single-library `search_titles` response shape is unchanged except for the additive/opt-in changes specified above (`library_slug` echo, facets opt-out, folded availability, enrichment fields).
- NFR-1.3: `get_availability` behavior is unchanged except for the explicit `library_slug` echo requirement (already satisfied in v1).

### NFR-2: Performance

- NFR-2.1: Multi-library fan-out uses `asyncio.gather` so N libraries complete in roughly the time of the slowest single library, not the sum.
- NFR-2.2: Any per-title availability calls introduced by FR-B2 must stay within the Lambda execution budget and respect the existing 10-second Thunder client timeout.
- NFR-2.3: Folding availability into search must not require more Thunder calls than necessary — only data already present in the `/media` search response is used (see design decision for FR-B2). In particular, `estimatedWaitDays` is not confirmed on the search response; the server does NOT make an extra `/availability` call to obtain it, and reports `estimated_wait_days = None` (unknown) when the field is absent rather than fabricating a value.

### NFR-3: Reliability

- NFR-3.1: A single bad slug or upstream failure in a multi-library search degrades gracefully per-library (AC-B1.5) rather than failing the whole request.
- NFR-3.2: All enrichment and derived-field logic handles missing/malformed Thunder fields without raising.
- NFR-3.3: The 404 handling must not swallow or misclassify other HTTP error codes.

### NFR-4: Testing (Harness Engineering)

- NFR-4.1: Tests are updated/added for every v2 behavior change. All existing tests continue to pass.
- NFR-4.2: New tests cover: multi-library fan-out (including per-library failure), the enriched title fields (series, narrator, publisher, edition, is_abridged, holds_per_copy, estimated_wait_days), facets opt-in/opt-out, the `library_slug` echo, and the human-readable 404 message.
- NFR-4.3: Tests for the retired `get_deep_link` tool are removed; a test asserts the tool is no longer registered.
- NFR-4.4: `make preflight` (validate + compliance + ci) passes before merge, including the 80% coverage threshold.
- NFR-4.5: Test files for not-yet-implemented changes follow the repo's TDD red-phase convention (`pytest.importorskip` where applicable) so collection does not break on intermediate commits.

### NFR-5: Observability

- NFR-5.1: Per-library failures in multi-library search are logged with the failing slug and status for debugging (CloudWatch), without leaking into other libraries' result blocks beyond the per-library `error` field.

---

## Acceptance Criteria (Feature-Level, from Brief)

- `search_titles` with `library_slugs=['lcpl','fairfax']` returns results from both in one call (AC-B1.6).
- A 404 from OverDrive produces a human-readable error identifying the bad slug (AC-A2.1).
- `search_titles` default response has no facets block (`include_facets=False`) (AC-A4.2).
- Each title result includes `series_name`, `series_position`, `narrator`, `publisher`, `edition`, `is_abridged` (FR-C1, FR-C2, FR-C3).
- All existing tests pass; new tests cover multi-library, enriched fields, and the 404 message (NFR-4).

## Out of Scope

- Any patron-authenticated endpoints or write operations (hold placement, checkout).
- Recommendation / ranking logic inside the server (callers still reason).
- Replacing the deep-link capability with anything other than the existing `link` field in search results.
- Cross-library deduplication or merged ranking of titles (results are grouped per library, not merged).
- `magazine` format support and `coverUrl` (still deferred from v1).
