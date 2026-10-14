# Design: Libby MCP Server v2

## Overview

v2 layers ten improvements onto the deployed v1 server without changing its
architecture (AWS Lambda + API Gateway HTTP API, FastMCP Streamable HTTP, public
Thunder API, no server-side recommendations). All changes land in four modules:

- `src/thunder_client.py` — 404 error message, an async fan-out path
- `src/tools/search_titles.py` — `_normalize_item` enrichment, facets opt-in,
  multi-library grouping, folded availability, `library_slug` echo
- `src/tools/availability.py` — unchanged behavior; `library_slug` echo verified
- `src/handler.py` — tool schema changes, retire `get_deep_link`

The v1 fact-finding contract is preserved: tools return raw data; the LLM caller
reasons. All title-object additions are additive (NFR-1).

```
                         ┌──────────────────────────────────────────────┐
MCP Client ─▶ API GW ─▶  │ Lambda (FastMCP, Mangum)                       │
                         │  search_titles (async def)                     │
                         │    ├─ single slug  ─▶ ThunderClient.search     │
                         │    └─ multi slugs  ─▶ await asyncio.gather(    │
                         │                         search_async(slug) …)  │
                         │  get_availability ─▶ ThunderClient.availability│
                         └──────────────────────────────────────────────┘
                                        │
                                        ▼
                             thunder.api.overdrive.com (public, no auth)
```

---

## Group A — Quick Fixes

### A1: Correct Library Slug Examples (FR-A1)

Mechanical string change. Every occurrence of `fairfaxcounty` in tool and
parameter descriptions in `handler.py` becomes `fairfax`. The canonical example
list everywhere is `lcpl, fairfax, nypl`.

Affected locations (from current `handler.py`):
- `search_titles` `library_slug` Field description: `"Library identifier (e.g., lcpl, fairfaxcounty, nypl)"` → `"...e.g., lcpl, fairfax, nypl"`.
- Any other description referencing `fairfaxcounty`.

**Compliance:** `scripts/check_compliance.py` (or a dedicated test) asserts no
`fairfaxcounty` literal remains in `src/`.

### A2: Human-Readable 404 Errors (FR-A2)

The 404 message is produced in `thunder_client._execute_get`, which is where the
`HTTPStatusError` branch currently lives. Because `_execute_get` only sees the
URL, the slug is threaded through so the message can name it.

Design decision: pass the slug (or the already-known human context) into
`_execute_get` rather than parsing it back out of the URL. Add an optional
`slug: str | None = None` argument to `_execute_get`, supplied by both
`search()` and `get_availability()` (both already receive `slug`).

```python
except httpx.HTTPStatusError as e:
    status = e.response.status_code
    logger.warning("thunder_http_error", extra={"url": url, "status": status, ...})
    if status == 404 and slug is not None:
        return {
            "error": (
                f"unknown library slug: {slug} — check the slug is correct "
                "(e.g. lcpl, fairfax, nypl)"
            )
        }
    return {"error": f"OverDrive service error (HTTP {status})"}
```

- Non-404 statuses keep the existing message (AC-A2.3).
- Timeout / unexpected branches are untouched (AC-A2.4).
- The slug is interpolated as-is; it is a plain string, no format spec, so empty
  or odd values do not crash (AC-A2.5).

Note: this is distinct from v1's "best-effort invalid slug heuristic" in
`search_titles.handle` (HTTP 200 + empty items + no narrowing filters). Thunder
historically returned 200 for some bad slugs; where it returns a true 404, the
new message is strictly better. Both paths may coexist.

### A3: Echo library_slug in Every Tool Response (FR-A3)

- `get_availability` already returns `library_slug` — keep it (AC-A3.2).
- `search_titles` single-library response gains a top-level `library_slug` equal
  to the queried slug (AC-A3.1).
- Multi-library response tags each per-library block with its own `library_slug`
  (AC-B1.4 / AC-A3.4).

### A4: Facets Opt-In (FR-A4)

Add `include_facets: bool = False` to the `search_titles` tool signature and
thread it into `search_titles.handle`. In `handle`, only attach `facets` when
the flag is true:

```python
result = {
    "library_slug": library_slug,
    "titles": titles,
    "total_items": total_items,
    "page": current_page,
    "total_pages": last_page,
    "filtered_count": len(titles),
}
if params.get("include_facets"):
    result["facets"] = response.get("facets", {})
return result
```

Default `False` means the `facets` key is **absent** (not empty) by default
(AC-A4.2). This is a documented behavior change from v1 (AC-A4.4) — the tool
description notes facets are opt-in.

---

## Group B — Multi-Library Search + Hold Intelligence

### B1: Multi-Library Fan-Out (FR-B1)

**Tool signature.** In `handler.py`, `library_slug` becomes
`Optional[str] = None` (it is no longer a required positional). The one-of check
below replaces the old "library_slug is required" error. The handler builds the
`args` dict passed into `handle` by including only non-`None` values, so a
`library_slugs`-only call never carries a stray `library_slug=None` through the
validation.

```python
async def search_titles(
    library_slug: Optional[str] = None,
    library_slugs: Optional[list[str]] = None,
    include_facets: bool = False,
    # … all other v1 params …
) -> dict[str, Any]:
    # Build args dict (non-None values only)
    args: dict[str, Any] = {}
    if library_slug is not None:
        args["library_slug"] = library_slug
    if library_slugs is not None:
        args["library_slugs"] = library_slugs
    args["include_facets"] = include_facets
    # … add other non-None params …

    # One-of validation (AC-B1.2)
    has_slug = "library_slug" in args
    has_slugs = "library_slugs" in args
    if has_slug and has_slugs:
        raise ToolError("provide either library_slug or library_slugs, not both")
    if not has_slug and not has_slugs:
        raise ToolError("library_slug or library_slugs is required")

    # library_slugs validation (AC-B1.9–B1.12): empty list, blank entries,
    # dedupe preserving first-seen order, max 10. Runs only on the multi path
    # and mutates args["library_slugs"] to the cleaned/deduped list.
    # (see the library_slugs validation table below)

    # Dispatch
    if has_slugs:
        result = await handle_multi(args, _thunder_client)  # async
    else:
        result = handle(args, _thunder_client)  # sync, unchanged v1 signature

    if "error" in result:
        raise ToolError(result["error"])
    return result
```

`handle()` stays **synchronous** with its unchanged v1 signature
(`def handle(params, client) -> dict`); existing v1 tests call it directly as a
sync function. `handle_multi()` is **async**. The one-of validation and the
`library_slugs` validation run in the wrapper **before** dispatch, and the
`ToolError` raise happens in the wrapper **after** dispatch — both `handle()` and
`handle_multi()` assume the `args` dict has already been validated.

**Validation (one-of):** performed in `handler.py`'s `search_titles` wrapper
(before dispatch), not inside `handle`/`handle_multi`:

| `library_slug` | `library_slugs` | Behavior |
|----------------|-----------------|----------|
| set | unset | v1 single-library path, grouped=false |
| unset | set | fan-out path, grouped response (after `library_slugs` validation below) |
| set | set | error: `provide either library_slug or library_slugs, not both` |
| unset | unset | error: `library_slug or library_slugs is required` |

**`library_slugs` validation** (applied before fan-out, in order; AC-B1.9–B1.12):

| Condition | Behavior |
|-----------|----------|
| empty list | error: `library_slugs must not be empty` |
| any entry empty / whitespace-only | error: `library_slugs contains invalid entry: empty slug` |
| duplicate slugs | deduplicate preserving first-seen order, drop later occurrences |
| more than 10 entries | error: `library_slugs exceeds maximum of 10 libraries` |

Order note: validate non-empty and reject blank entries first, then deduplicate,
then enforce the max-of-10 cap on the deduplicated list.

**Async client path.** The v1 `ThunderClient` is synchronous (`httpx.Client`).
To satisfy the brief's `asyncio.gather` requirement, add an async search method.
The `AsyncClient` is **not** stored on the `ThunderClient` instance — on warm
Lambda invocations a client created against a previous event loop would be bound
to a closed loop and its connection pool would be unusable. Instead a fresh
`AsyncClient` is created per fan-out, scoped to the current event loop:

```python
class ThunderClient:
    def __init__(self) -> None:
        self._client = httpx.Client(...)          # existing, for sync callers
        # No self._async_client — see per-fan-out client below.

    async def search_async(
        self, slug, params, async_client: httpx.AsyncClient
    ) -> dict[str, Any]:
        url = self._build_search_url(slug, params)
        return await self._execute_get_async(url, async_client, slug=slug)
```

`_execute_get_async` mirrors `_execute_get` (same logging, same error mapping
including the 404 message from A2) but takes the per-fan-out `async_client`. The
sync `search`/`get_availability` paths remain for `get_availability` and
single-slug search; reusing the existing sync path for the single-slug case keeps
v1 behavior byte-for-byte.

**Fan-out orchestration.** The FastMCP `search_titles` tool is declared
`async def` (FastMCP + Mangum run inside an existing event loop, so calling
`asyncio.run()` from a sync tool would raise `RuntimeError: asyncio.run() cannot
be called from a running event loop`). The handler awaits the fan-out directly
and creates the `AsyncClient` inside `_fan_out`, so its lifetime matches the
current loop:

```python
async def _fan_out(slugs, params, client):
    async with httpx.AsyncClient(
        headers=..., timeout=TIMEOUT_SECONDS
    ) as async_client:
        tasks = [_search_one(slug, params, client, async_client) for slug in slugs]
        return await asyncio.gather(*tasks)  # exceptions handled in _search_one

async def handle_multi(params, client):
    slugs = params["library_slugs"]              # already validated/deduped
    results = await _fan_out(slugs, params, client)
    return {"libraries": results}  # see response shape below
```

The single-slug path stays synchronous as in v1 (`handle` calls the sync
`client.search`); only the multi-slug branch is async. `_search_one` wraps a
single library search, catches/normalizes failures, and always returns a dict
with `library_slug` set (never raises), so one bad slug does not poison `gather`
(AC-B1.5). Design detail: use `asyncio.gather(..., return_exceptions=True)` OR
catch inside `_search_one`; catching inside `_search_one` is preferred so each
block is uniformly shaped.

**End-to-end test note.** Because the async/sync boundary is where Mangum breaks,
unit tests that call `_fan_out` directly are insufficient. Add an end-to-end test
that drives the fan-out through the actual FastMCP app (invoking the registered
`search_titles` tool via the MCP app, not calling `handle_multi` in isolation) to
confirm the async tool runs correctly under a running event loop.

**Multi-library response shape** (grouped, AC-B1.4):

```json
{
  "libraries": [
    {
      "library_slug": "lcpl",
      "titles": [ /* Title Objects */ ],
      "total_items": 843,
      "page": 1,
      "total_pages": 9,
      "filtered_count": 24
    },
    {
      "library_slug": "fairfax",
      "titles": [ /* Title Objects */ ],
      "total_items": 512,
      "page": 1,
      "total_pages": 6,
      "filtered_count": 18
    }
  ]
}
```

A failed library block (AC-B1.5):

```json
{ "library_slug": "badslug", "error": "unknown library slug: badslug — check the slug is correct (e.g. lcpl, fairfax, nypl)" }
```

`facets` appear per-library only when `include_facets=True`. A single-entry
`library_slugs` still yields this grouped shape (AC-B1.8). The singular
`library_slug` path keeps the v1 flat shape plus additive fields (AC-B1.7).

> **Alternative considered:** thread-pool fan-out with the existing sync client
> (`concurrent.futures.ThreadPoolExecutor`). Rejected because the brief
> explicitly calls for `asyncio.gather`, and an `AsyncClient` keeps I/O
> non-blocking within the Lambda invocation.

### B2: Fold Availability Into Search (FR-B2)

Add two fields to every Title Object:

- `holds_per_copy`: `round(holdsCount / ownedCopies, 1)`, computed safely.
- `estimated_wait_days`: from `estimatedWaitDays`, default `None` (unknown).

**`holds_per_copy` semantics (AC-B2.2).** The ratio is defined precisely so that
"no contention" is not conflated with "library has holds but owns nothing":

| `owned` | `holds` | `holds_per_copy` |
|---------|---------|------------------|
| `> 0` | any | `round(holds / owned, 1)` |
| `0` (or missing) | `0` | `0.0` (no contention) |
| `0` (or missing) | `> 0` | `None` (ratio not meaningful) |

```python
def _holds_per_copy(holds: int, owned: int) -> float | None:
    if owned:
        return round(holds / owned, 1)
    # owned == 0 (or falsy)
    return 0.0 if not holds else None
```

**Data source decision (AC-B2.4, NFR-2.3):** The Thunder `/media` search
response carries `holdsCount` and `ownedCopies` per item (confirmed in v1
`_normalize_item`, which already reads both). v2 derives `holds_per_copy`
directly from the search item — **no extra per-title availability call is
required**. This keeps the fan-out cost at one Thunder call per library.

> **Data gap — `estimatedWaitDays`.** `estimatedWaitDays` has only been observed
> on the `/availability` endpoint response; it is **not confirmed present** on
> the `/media` search response. v2 does **not** add an extra availability call to
> fill it (NFR-2.3). If the field is absent from the search item,
> `estimated_wait_days` is returned as `None` (unknown), **not** `0`. Returning
> `0` would wrongly imply "no wait". Callers must treat `None` as unknown — if
> they need a confirmed wait estimate they can call `get_availability`. The key
> is always present on every title (value `None` when unknown).

### B3: Retire get_deep_link (FR-B3)

- Delete the `@mcp.tool get_deep_link` registration and the
  `from src.tools import deep_link as deep_link_impl` import from `handler.py`.
- Remove `src/tools/deep_link.py` and `tests/test_deep_link.py`.
- Guarantee `_normalize_item` always sets a non-empty, well-formed `link`
  (`https://{library_slug}.overdrive.com/media/{id}`). v1 already builds this;
  v2 adds a test asserting `link` is well-formed for every item that is returned
  (AC-B3.3).
- **Missing/`None` id rule (AC-B3.3).** An item with no `id` (missing or `None`)
  would produce a broken link such as `.../media/` or `.../media/None`. Such
  items are **excluded from results** with a `logger.warning` naming the dropped
  item (and library slug) for observability; `link` is never set to `None` or an
  empty-id value. In the normalization flow, `_normalize_item` returns a sentinel
  (e.g. `None`) for id-less items and the caller filters them out before building
  the `titles` list, so only items with a valid id — and therefore a well-formed
  `link` — reach the response.
- Add a handler-level test asserting `get_deep_link` is absent from the tool set
  (AC-B3.1 / NFR-4.3).

---

## Group C — Content Enrichment

All Group C fields are added to `_normalize_item` in `search_titles.py`, so they
flow through both single and multi-library responses. Group C depends on B's
enrichment work landing in the same normalizer.

> **Field paths are unverified assumptions.** The paths below (`series.name`,
> `series.readingOrder`, `narrators[].name`, `publisher.name`, `edition`,
> `isAbridged`) are based on OverDrive/Thunder documentation, not a captured live
> `/media` item. Before implementing each of C1–C3, the actual field paths MUST
> be verified against `docs/references/thunder_api_reference.md` or a captured
> live media item. If a path differs, update both `_normalize_item` and the test
> fixtures to match the real response. The isinstance guards below also protect
> against the real shape differing from these assumptions.

### C1: Series (FR-C1)

```python
series = item.get("series")
if not isinstance(series, dict):       # series may be absent, or a bare string
    series = {}
series_name = series.get("name")               # str | None
raw_order = series.get("readingOrder")
series_position = _to_float(raw_order)         # float | None
```

`_to_float` returns `None` for missing/non-numeric values (AC-C1.4). When there
is no series object (or it is a non-dict such as a plain string), both resolve to
`None` (keys present, AC-C1.3).

### C2: Narrator, Publisher, Edition (FR-C2)

```python
nars = item.get("narrators") or []
narrator = [n.get("name", "") for n in nars if isinstance(n, dict) and n.get("name")]

pub = item.get("publisher")
publisher = pub.get("name") if isinstance(pub, dict) else None   # str | None

edition = item.get("edition")                                    # str | None
```

- `narrator` is always a list (empty when absent, or when entries are bare
  strings instead of `{name}` objects) (AC-C2.1).
- `publisher`/`edition` are `None` when absent — including when `publisher`
  arrives as a bare string rather than a `{name}` object (AC-C2.2/3). Note: v1
  design docs showed `publisher` as a nested `{name}` object — v2 flattens to the
  name string (or `None`), matching the brief.
- The isinstance guards ensure malformed types (series as string, publisher as
  string, narrators as a list of strings) resolve to the documented default
  rather than raising (AC-C2.4).

### C3: Duration + Abridged (FR-C3)

- `hours` already exists via `_get_hours`; v2 adds a test guaranteeing the key is
  always present (float or `None`) (AC-C3.1).
- `is_abridged` reads `isAbridged` from the same format entry used for format /
  duration detection:

```python
fmt = formats[0] if formats and isinstance(formats[0], dict) else {}
is_abridged = fmt.get("isAbridged")   # True / False / None (absent = unknown)
```

Absent `isAbridged` resolves to `None` (unknown), never `False` (AC-C3.2/4).

---

## Data Models

### Title Object (v2 — search_titles response item)

```json
{
  "id": "12345",
  "title": "Dungeon Crawler Carl",
  "creator": "Matt Dinniman",
  "format": "audiobook",
  "available": true,
  "available_copies": 2,
  "owned_copies": 3,
  "holds_count": 0,
  "holds_per_copy": 0.0,
  "estimated_wait_days": null,
  "hours": 12.6,
  "is_abridged": false,
  "bisac_codes": ["FIC129000", "FIC009000"],
  "series_name": "Dungeon Crawler Carl",
  "series_position": 1.0,
  "narrator": ["Jeff Hays"],
  "publisher": "Podium Audio",
  "edition": "Unabridged",
  "link": "https://lcpl.overdrive.com/media/12345"
}
```

New in v2: `holds_per_copy`, `estimated_wait_days`, `is_abridged`, `series_name`,
`series_position`, `narrator`, `publisher`, `edition`. All v1 fields retained
(NFR-1.1).

### Search Response — Single Library (v2)

```json
{
  "library_slug": "lcpl",
  "titles": [ /* Title Objects */ ],
  "total_items": 843,
  "page": 1,
  "total_pages": 9,
  "filtered_count": 47
}
```

`facets` present only when `include_facets=true`. `library_slug` is new (A3).

### Search Response — Multi Library (v2)

```json
{
  "libraries": [
    { "library_slug": "lcpl", "titles": [ ... ], "total_items": 843, "page": 1, "total_pages": 9, "filtered_count": 47 },
    { "library_slug": "fairfax", "error": "unknown library slug: fairfax — ..." }
  ]
}
```

### Availability Response (unchanged from v1)

```json
{
  "library_slug": "lcpl",
  "titles": [
    { "title_id": "12345", "copies_owned": 3, "copies_available": 2, "holds_count": 0, "estimated_wait_days": 0 }
  ]
}
```

---

## MCP Tool Surface (v2)

Two tools after v2 (down from three):

### search_titles (updated)

New/changed parameters:

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `library_slug` | string | — | One-of with `library_slugs`. Example updated to `lcpl, fairfax, nypl`. |
| `library_slugs` | array[string] | — | One-of with `library_slug`. Fan-out, grouped response. |
| `include_facets` | boolean | `false` | Opt-in facets (A4). |

All other v1 parameters (`subjects`, `format`, `available`, `creator`, `bisac`,
`series_id`, `under_hours`, `per_page`, `page`, `maturity_level`, `sort_by`,
`query`) are unchanged.

Description notes: facets are now opt-in; results include folded availability
(`holds_per_copy`, `estimated_wait_days`) and enrichment (series, narrator,
publisher, edition, is_abridged); the deep link is in each title's `link` field.

### get_availability (unchanged)

Behavior and schema unchanged. Keeps `library_slug` in the response.

### get_deep_link (REMOVED)

Retired. The `link` field in search results replaces it.

---

## Error Handling (v2 additions)

| Scenario | Where | Result |
|----------|-------|--------|
| Thunder HTTP 404 | `thunder_client._execute_get[_async]` | `unknown library slug: <slug> — check the slug is correct (e.g. lcpl, fairfax, nypl)` |
| Both `library_slug` + `library_slugs` | `search_titles.handle` / handler | ToolError: `provide either library_slug or library_slugs, not both` |
| Neither provided | `search_titles.handle` / handler | ToolError: `library_slug or library_slugs is required` |
| Per-library failure in fan-out | `_search_one` | Block with `library_slug` + `error`; other libraries unaffected |

Single-library errors still raise `ToolError` (whole call fails). Multi-library
errors are per-block and never raise for an individual library (AC-B1.5).

---

## Backward Compatibility & Migration

- Single-library callers see only additive fields plus the facets opt-out. If a
  caller depended on facets always being present, it must now pass
  `include_facets=true` (documented breaking-ish change, AC-A4.4).
- Callers of `get_deep_link` must switch to the `link` field already present on
  every search result.
- `get_availability` callers are unaffected.

---

## Testing Strategy

Mirrors the v1 harness (`tests/`, `conftest.py`, fixtures, `make preflight`).

- **Fixtures:** extend `tests/fixtures/search_response.json` with items carrying
  `series`, `narrators`, `publisher`, `edition`, `isAbridged`, `estimatedWaitDays`,
  `holdsCount`, `ownedCopies`. Add a 404 error fixture to `error_responses.json`.
- **thunder_client:** test the 404 message for both endpoints; test `search_async`
  returns the same shape as `search`; test non-404 keeps the old message.
- **search_titles:** `include_facets` on/off; `library_slug` echo; one-of
  validation; multi-library grouping; per-library failure isolation;
  `holds_per_copy` math incl. zero-copy; `estimated_wait_days`; all enrichment
  fields incl. missing-field defaults; `link` always present; `hours` always
  present.
- **handler:** `get_deep_link` no longer registered; the two remaining tools
  still callable.
- Remove `tests/test_deep_link.py`.
- Keep TDD red-phase convention (`pytest.importorskip`) where modules/behaviors
  are not yet implemented (NFR-4.5).
