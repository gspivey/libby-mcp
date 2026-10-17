# Design: Libby MCP Server v3

## Overview

v3 layers three concerns onto the deployed v2 server without changing its core
contract (public Thunder API, no server-side recommendations, FastMCP Streamable
HTTP). The changes touch six modules plus three new ones:

| Module | Change |
|--------|--------|
| `src/tools/availability.py` | `not_owned` per-title status (D1); `estimated_wait_days==0` when available (D3); multi-library fan-out (D7) |
| `src/tools/search_titles.py` | slug normalization (D2); `estimated_wait_days` correction in fold-in (D3); honest `is_abridged` (D4); `exact` filter (D6); reuse slug-list helpers for D7 |
| `src/tools/lookup_library_slug.py` | **new** — slug directory lookup with cache (D5) |
| `src/thunder_client.py` | `list_libraries()` directory fetch + per-library availability async fan-out (D5, D7); `get_library()`/`get_library_async()` slug-probe (D1/HIGH-3); error dicts carry the HTTP `status` code (HIGH-3) |
| `src/slug_directory.py` | **new** — in-process TTL cache of the Thunder library directory (D5) |
| `src/handler.py` | single-codebase auto-detect (E1); register `lookup_library_slug`; slug normalization at the tool boundary; mount OAuth routes (F2) |
| `src/auth.py` | three auth modes in one `AuthMiddleware` (F1); RFC 9728 401 challenge (F3) |
| `src/oauth/` | **new package** — embedded minimal authorization server + resource-server token validation (F2–F5) |
| `infra/`, `Dockerfile`, `docker-compose.yml` | Docker standalone + OAuth env wiring (E, F) |

The v1/v2 fact-finding contract is preserved; all tool additions are additive
except the two documented changes (D1 per-title shape, D4 `is_abridged`
decision).

### Runtime topology (v3)

```
                         AWS_LAMBDA_RUNTIME_API set?
                                   │
                 ┌─────────────────┴──────────────────┐
                 ▼ yes (Lambda)                        ▼ no (Docker / local)
        Mangum(_build_app())                  uvicorn src.handler:_build_app
                 │                                     │
                 └──────────────┬──────────────────────┘
                                ▼
                      _build_app() → AuthMiddleware(router)
                                ▼
        router:  POST/GET /mcp  →  FastMCP Streamable HTTP app
                 GET  /.well-known/oauth-protected-resource   (OAuth mode)
                 GET  /.well-known/oauth-authorization-server  (OAuth mode)
                 POST /register   GET /authorize   POST /token  (OAuth mode)
                                ▼
                     tools: search_titles, get_availability, lookup_library_slug
                                ▼
                   thunder.api.overdrive.com (public, no auth)
```

---

## Group D — Bug Fixes

### D1: `not_owned` Status in `get_availability` (FR-D1)

**Root cause of the D1 crash.** Thunder's bulk-availability endpoint
(`/v2/libraries/{slug}/media/availability?titleIds=...`) **silently omits** any
title it does not know: a requested `title_id` that the library does not own is
simply absent from `items[]` — Thunder does **not** return a per-title 404 for it.
The 500 observed in live usage came from the v2 code treating an absent ID as an
error condition (and, when the *only* requested ID was absent, surfacing the
empty/partial response as a failing request). The fix is a classification rule,
not a Thunder URL change: **after calling `get_availability`, any requested
`title_id` not present in the response `items` is classified as
`status: not_owned`.** A genuine failure (timeout, 5xx, or a 404 on the library
*slug itself*) is still a top-level error (AC-D1.4) — `not_owned` is strictly the
"absent from an otherwise-successful response" signal. v2's
`availability.handle` instead mapped those absent IDs to `{"title_id": tid,
"error": "not_found"}`, which v3 removes.

**Change.** For every requested title ID absent from a *successful* availability
response, emit `{"title_id": tid, "status": "not_owned"}` instead of an `error`
entry. A client-level error on the whole response (timeout, 5xx, or a 404 on the
slug itself) is still returned as a top-level `error` (AC-D1.4) — the `not_owned`
status is strictly a per-title "absent from an otherwise-successful response"
signal.

**One shared classifier for single and multi paths (HIGH-4).** Both the
single-library (`handle`) and multi-library (`_availability_one`, D7) paths MUST
route through **one** `classify_and_fetch(slug, title_ids, client[, async_client])`
helper so the HIGH-3 rules (non-numeric filtering, no-call-when-empty, and the 4xx
slug probe) can never diverge between the two. The v2 bug was partly that the
multi path had its own inline shaping that skipped the non-numeric rejection and
the probe. `classify_and_fetch` returns the per-library block (either
`{"titles": [...]}` to be tagged with the slug, or a top-level `{"error": ...}`):

```python
# availability.py — single shared classifier (used by single AND multi paths)
def _normalize_title_ids(title_ids: list) -> list[str]:
    # HIGH-4: coerce every element to a stripped string BEFORE any isinstance /
    # isdigit check, so an int ID (123), a padded " 123 ", or a non-str never
    # raises AttributeError and is classified consistently. Empty-after-strip
    # entries are dropped here and are not sent to Thunder.
    out = []
    for tid in title_ids:
        s = str(tid).strip()
        if s:
            out.append(s)
    return out

def _split_numeric(title_ids: list[str]) -> tuple[list[str], list[str]]:
    # title_ids here are already normalized strings (see _normalize_title_ids).
    numeric = [t for t in title_ids if t.isdigit()]
    non_numeric = [t for t in title_ids if not t.isdigit()]
    return numeric, non_numeric

def _shape_items(numeric_ids, non_numeric, items_by_id) -> list[dict]:
    titles = [{"title_id": tid, "status": "not_owned"} for tid in non_numeric]  # HIGH-3
    for tid in numeric_ids:
        item = items_by_id.get(tid)
        if item is None:
            titles.append({"title_id": tid, "status": "not_owned"})             # D1
            continue
        available = item.get("availableCopies", 0) or 0
        raw_wait = item.get("estimatedWaitDays", 0) or 0
        titles.append({
            "title_id": tid,
            "copies_owned": item.get("ownedCopies", 0),
            "copies_available": available,
            "holds_count": item.get("holdsCount", 0),
            "estimated_wait_days": 0 if available > 0 else raw_wait,            # D3
        })
    return titles

# Shared by the single-library handle() and the multi-library _availability_one().
# classify_and_fetch is ASYNC and ALWAYS awaits the async Thunder helpers
# (get_availability_async / get_library_async). It never calls a sync method, so
# there is no sync/async fork to drift (HIGH-3). The single path and the multi
# path both pass an httpx.AsyncClient (`ac`); the single path creates its own.
async def classify_and_fetch(slug, title_ids, client, async_client) -> dict:
    # HIGH-4: normalize each ID to a stripped string FIRST (handles int IDs,
    # padding, and non-str), then enforce the non-empty rule. An empty list — or a
    # list that is empty after dropping blank entries — is a validation error on
    # BOTH the single and multi paths (not a silent empty success).
    norm = _normalize_title_ids(title_ids)
    if not norm:
        return {"error": "title_ids is required (non-empty)"}
    numeric_ids, non_numeric = _split_numeric(norm)
    if not numeric_ids:
        # HIGH-4: no numeric IDs → make NO Thunder call; everything is not_owned.
        return {"titles": [{"title_id": t, "status": "not_owned"} for t in non_numeric]}
    resp = await client.get_availability_async(slug, numeric_ids, async_client)
    if isinstance(resp, dict) and resp.get("error"):
        status = resp.get("status")
        if isinstance(status, int) and status in (400, 404):         # HIGH-5: probe slug ONLY on 400/404
            lib = await client.get_library_async(slug, async_client)
            if isinstance(lib, dict) and lib.get("error"):
                return {"error": f"unknown library slug: {slug}"}    # bad slug
            # valid slug → the availability call rejected the IDs, not the library.
            if len(numeric_ids) == 1:
                base = [{"title_id": numeric_ids[0], "status": "not_owned"}]
            else:
                # HIGH-5: a 400/404 on a MULTI-ID batch may be one bad ID poisoning
                # the batch — retry each ID alone so OWNED titles are not masked.
                base = await _retry_each_individually(slug, numeric_ids, client, async_client)
            return {"titles": base + [{"title_id": t, "status": "not_owned"} for t in non_numeric]}
        return {"error": resp["error"]}                              # 401/403/408/429, 5xx, timeout → top-level error
    items_by_id = {str(i.get("id")): i for i in resp.get("items", [])}
    return {"titles": _shape_items(numeric_ids, non_numeric, items_by_id)}

# HIGH-5: per-ID retry for a multi-ID batch that 400/404'd on a VALID slug.
async def _classify_one_id(slug, tid, client, async_client) -> dict:
    resp = await client.get_availability_async(slug, [tid], async_client)
    if isinstance(resp, dict) and resp.get("error"):
        status = resp.get("status")
        if isinstance(status, int) and status in (400, 404):
            return {"title_id": tid, "status": "not_owned"}          # single bad ID
        return {"title_id": tid, "error": resp["error"]}             # transient/5xx → per-title error
    item = {str(i.get("id")): i for i in resp.get("items", [])}.get(tid)
    if item is None:
        return {"title_id": tid, "status": "not_owned"}              # absent from items
    return _shape_items([tid], [], {tid: item})[0]                   # owned → full data (preserved!)

async def _retry_each_individually(slug, numeric_ids, client, async_client) -> list[dict]:
    return list(await asyncio.gather(
        *[_classify_one_id(slug, tid, client, async_client) for tid in numeric_ids]
    ))

# availability.py — single-library handle() is ASYNC (HIGH-3), awaits the SAME
# classify_and_fetch as the multi path, creating a short-lived AsyncClient.
async def handle(params, client) -> dict:
    slug = params["library_slug"]              # already normalized (D2)
    title_ids = params["title_ids"]            # already validated non-empty (HIGH-4)
    async with httpx.AsyncClient(headers=..., timeout=TIMEOUT_SECONDS) as ac:
        block = await classify_and_fetch(slug, title_ids, client, ac)
    if block.get("error"):
        return {"library_slug": slug, "error": block["error"]}
    return {"library_slug": slug, **block}     # flat v2 shape (AC-D7.6)
```

The single path (`handle`) wraps the returned block as the flat
`{"library_slug": slug, **block}`; the multi path tags each block with its slug
inside `{"libraries": [...]}` (D7). Because the exact same (always-async)
`classify_and_fetch` runs in both, a non-numeric ID in a **multi-library** request
is rejected and classified `not_owned` identically to the single-library case
(covered by a dedicated multi-library test, HIGH-4).

> **Single-path async (HIGH-3).** `get_availability`'s single-library handler is
> `async def handle(...)` and awaits `client.get_availability_async(...)` through
> the shared `classify_and_fetch`. The async helper `get_availability_async` (and
> the `get_library_async` slug-probe) therefore land in **Task D1 / PR-D1**, not
> Task D7 — PR-D1 must compile and pass mypy-strict on its own, and it cannot
> import an async helper that D7 adds later. D7 adds only the fan-out wrapper
> (`handle_multi` / `_availability_one`) on top of the already-async D1 core; it
> introduces no new Thunder method.

> The legacy `{"error": "not_found"}` per-title shape is removed (AC-D1.5). This
> is the one deliberate non-additive change in v3 (NFR-1.1); it is documented in
> the data-models section and covered by an updated fixture + test.

**Distinguishing "not-owned" from a genuine error (HIGH-3).** The brief found
that the D1 crash was never root-caused — the v2 code conflated three different
upstream outcomes. v3 names them explicitly and verifies Thunder's real behavior
rather than guessing:

1. **Non-numeric / malformed `title_id`.** OverDrive title IDs are numeric
   strings. A `title_id` that is not all-digits (e.g. the brief's literal
   `'bad-title-id'`) can make Thunder reject the whole batch request. v3 first
   **normalizes each ID to a stripped string** (`tid = str(tid).strip()`) **before
   any `isinstance`/`isdigit` check** (HIGH-4) — so an int ID, a padded `" 123 "`,
   or a non-str is coerced rather than raising — then **classifies a non-numeric
   `title_id` as `{"title_id": tid, "status": "not_owned"}` *before* it is sent to
   Thunder** — it is filtered out of the `titleIds` query and emitted directly in
   the result. This guarantees AC-D1.2 (a request whose only ID is `bad-title-id`
   returns a flat `not_owned` block, never a 500) without depending on how Thunder
   reacts to a malformed ID. The numeric check is purely mechanical
   (`str(tid).strip().isdigit()`), no slug or title special-casing (NFR-6.1).
   `title_ids` itself must be a non-empty list on **both** the single and
   multi-library paths; an empty (or empty-after-strip) list is a validation error
   (`title_ids is required (non-empty)`), never a silent empty success (HIGH-4).

2. **Unknown-but-well-formed `title_id` (library does not own it).** A numeric ID
   the library does not own is simply **absent from `items[]`** in an otherwise
   `200 OK` response (verified behavior, step below) → classified `not_owned`.

3. **A real upstream failure.** A `4xx`/`5xx`/timeout on the availability call
   itself is a top-level `error` (AC-D1.4). The slug probe is used to tell a bad
   **library slug** from a bad **title**, but **only for the two statuses that can
   mean "the request/path was rejected": `400` and `404`** (HIGH-5). Other 4xx
   statuses are *not* ambiguous between title and slug and are top-level errors
   without a probe:
   - **`401`/`403`** — authn/authz on the Thunder call itself (never per-title);
   - **`408`** — request timeout (transient, like `5xx`/timeout);
   - **`429`** — rate limited (transient; retrying as a slug probe would make it
     worse).

   So on a `400` or `404` from the availability call, v3 probes
   `GET /v2/libraries/{slug}`:
   - if the library lookup also fails (`4xx`), the slug is the problem →
     top-level `error` (`unknown library slug: <slug>` for the single form, or the
     per-library `error` block in the multi form, AC-D7.5);
   - if the library lookup **succeeds** (the slug is valid) and **only one** title
     ID was requested, that ID is `not_owned` (the endpoint rejected the single
     ID, not the library);
   - if the library lookup **succeeds** and **multiple** title IDs were requested,
     v3 **retries each ID individually** (concurrently, bounded) rather than
     blanket-classifying the whole batch `not_owned` (HIGH-5). A `400` on a
     multi-ID batch can be caused by a *single* malformed/unknown ID poisoning the
     batch; blanket-classifying would mask the *owned* titles in that same batch.
     Each per-ID retry re-runs the same single-ID classification: a `200` with the
     ID present → full availability; absent-from-`items` or a per-ID `400/404` →
     `not_owned`; a `5xx`/`401`/`403`/`408`/`429`/timeout on a per-ID retry → that
     ID carries a per-title `error`. The owned titles in the batch are therefore
     preserved.

   `401`/`403`/`408`/`429`, `5xx`, and timeouts are always a top-level `error`
   (never reclassified, never probed).

> **Verify-first (HIGH-3).** Task D1 records Thunder's **actual** HTTP status and
> response body for (a) a well-formed-but-unknown `titleId` and (b) a non-numeric
> `titleId`, against the live/reference API, and captures both as fixtures
> (including a **4xx availability fixture** and a 4xx `/v2/libraries/{slug}`
> fixture) before the classification rule is finalized. The rule above is the
> design intent; the recorded statuses confirm which branch (absent-from-`items`
> vs. 4xx) each case actually takes.

**Thunder client changes required for the slug-probe branch (HIGH-3/HIGH-5).**
The v2 client's `_execute_get` / `_execute_get_async` collapse every HTTP error
into a bare `{"error": "<message>"}` dict and **drop the HTTP status code**, so a
caller cannot tell a `400`/`404` (which must trigger the slug probe) from a
`429`/`401`/`403`/`408` or a `5xx` (which must stay a top-level error). Two
concrete changes make AC-D1.7 implementable:

1. **Carry the status code in the error dict.** On an `HTTPStatusError`, the error
   dict gains a numeric `status` field alongside the human-readable `error`
   message, e.g. `{"error": "OverDrive service error (HTTP 400)", "status": 400}`.
   (Timeouts and non-HTTP failures keep returning an `error` with no `status`, so
   the availability code treats a missing/`None` `status` — and any status other
   than `400`/`404` — as "do not probe" and keeps it a top-level error.) A typed
   exception carrying `.status_code` is an acceptable alternative, as long as the
   availability layer can read the numeric status.

2. **Add the async availability + slug-probe methods (land in D1/PR-D1).** The
   always-async `classify_and_fetch` (HIGH-3) calls `get_availability_async(slug,
   title_ids, async_client)` and `get_library_async(slug, async_client)`. Both the
   async availability helper **and** the async slug-probe therefore ship in
   **Task D1**, not D7 — PR-D1's single path is already async and must compile and
   type-check on its own. A sync `get_library(slug)` mirror may also be added for
   non-async callers, but the availability layer never uses it. `get_library_*`
   calls `GET /v2/libraries/{slug}` and returns the parsed `LibraryResponse` on
   success or the same `{"error": ..., "status": ...}` shape on failure. The
   availability branch invokes the probe **only on a `400`/`404`** (HIGH-5): a
   `4xx` from the probe means the **slug** is bad → top-level `error`; a `2xx`
   means the slug is valid and the IDs are classified per the single/multi rule
   above (per-ID retry for a multi-ID batch, HIGH-5).

```python
# thunder_client.py — HIGH-3 additions (sketch); ALL land in Task D1 / PR-D1.
async def get_availability_async(self, slug, title_ids, async_client) -> dict[str, Any]:
    """Async bulk availability — awaited by classify_and_fetch for BOTH the
    single-library handle() and the multi-library fan-out (HIGH-3)."""
    url = f"{BASE_URL}/v2/libraries/{slug}/media/availability"
    return await self._execute_get_async(
        url, async_client, params={"titleIds": ",".join(title_ids)}, slug=slug)

async def get_library_async(self, slug, async_client) -> dict[str, Any]:
    """GET /v2/libraries/{slug} — probe whether a slug is valid (D1/HIGH-5)."""
    url = f"{BASE_URL}/v2/libraries/{slug}"
    return await self._execute_get_async(url, async_client, slug=slug)

def get_library(self, slug: str) -> dict[str, Any]:
    """Sync mirror (not used by the availability layer; async is canonical)."""
    url = f"{BASE_URL}/v2/libraries/{slug}"
    return self._execute_get(url, slug=slug)

# in _execute_get / _execute_get_async, the HTTPStatusError branch now returns:
#     return {"error": f"OverDrive service error (HTTP {status})", "status": status}
# (and the slug-404 message branch likewise includes "status": status)
```

Only a `status` of **`400` or `404`** triggers the slug probe (HIGH-5);
`401`/`403`/`408`/`429`, `status >= 500`, or a missing `status` (timeout) is
always a top-level error, never reclassified or probed.

### D2: Normalize Library Slugs on Input (FR-D2)

A single helper normalizes a slug at the input boundary:

```python
# a shared util (e.g. src/tools/_slugs.py or extend search_titles helpers)
def normalize_slug(slug: str) -> str:
    return slug.strip().lower()
```

Applied in the `handler.py` tool wrappers (before building the `args` dict and
before dedupe) and in the directory lookup, so every slug that reaches a Thunder
URL, a response field, a `link`, or an error message is already lowercased and
trimmed (AC-D2.1). Ordering matters:

1. normalize each incoming slug,
2. run the one-of / empty-entry validation on the normalized values,
3. deduplicate the normalized list (so `['LCPL','lcpl']` → `['lcpl']`, AC-D2.4),
4. enforce the max-10 cap.

`_normalize_item`'s `link` already interpolates `library_slug`; because the slug
is normalized upstream, the host is lowercase (AC-D2.2) with no further change
there. A slug that is empty/whitespace after normalization fails the existing
validation (AC-D2.5).

> **No-hardcoded-slug constraint (NFR-6.1).** `normalize_slug` is purely
> mechanical — it never special-cases `lcpl`/`fairfax`/`fcpl`. The example slugs
> appear only in descriptions, error text, and tests.

### D3: `estimated_wait_days == 0` When Available (FR-D3)

Thunder's `estimatedWaitDays` is noisy (reference quirk #3: can be nonzero while
copies are free). The correction is applied in two places:

- **`get_availability`** (`availability.py`, shown in D1 above): if
  `copies_available > 0` → `0`, else pass through the raw value (AC-D3.1/D3.2).
- **`search_titles` fold-in** (`_normalize_item` in `search_titles.py`): v2 reads
  `estimated_wait_days = item.get("estimatedWaitDays")` (None when absent). v3
  wraps it:

```python
raw_wait = item.get("estimatedWaitDays")          # may be absent (None)
available_copies = item.get("availableCopies", 0) or 0
if available_copies > 0:
    estimated_wait_days = 0                        # D3: on the shelf → no wait
else:
    estimated_wait_days = raw_wait                 # None (unknown) or raw value
```

So the v2 "unknown is `None`, not `0`" rule is preserved for the *not-available*
case, while an available title is always `0` (AC-D3.3). The rule keys on copy
counts, not on `isAvailable` alone (AC-D3.4).

### D4: Honest `is_abridged` (FR-D4)

v2 reads `is_abridged = primary_fmt.get("isAbridged")`, which resolves to `None`
against the live API because `isAbridged` is not in the documented item shape.
The brief forbids a fabricated value.

**Verification-first design.** Before implementation, verify against
`docs/references/thunder_api_reference.md` / `thunder_openapi_v1.json` / a
captured live `/media` item whether a reliable abridged signal exists:

1. **If a real source exists** (format-entry `isAbridged`, or an "Abridged" /
   "Unabridged" token in the `edition` string): derive `is_abridged` from it,
   returning `True`/`False` only when the source is present, and `None` when
   absent (AC-D4.1/D4.2). Example edition-derived reader:

   ```python
   def _derive_abridged(primary_fmt: dict, edition: str | None) -> bool | None:
       raw = primary_fmt.get("isAbridged")
       if isinstance(raw, bool):
           return raw
       if isinstance(edition, str):
           e = edition.strip().lower()
           if "unabridged" in e:
               return False
           if "abridged" in e:
               return True
       return None   # unknown — never fabricated as False
   ```

2. **If no reliable source exists:** drop `is_abridged` from the title schema
   entirely (AC-D4.3), and update the README/data-model so no caller expects it.

Either way, a test asserts `is_abridged` is never a hardcoded constant across a
mixed fixture set (AC-D4.4). The design records the chosen branch in the PR; the
default recommendation is **derive-from-edition-else-None** (keeps the field,
honest `None` when unknown).

### D5: `lookup_library_slug` Tool (FR-D5)

**Data source.** Thunder exposes `GET /v2/libraries` (paginated,
`PagedResponse<LibraryResponse>`; `page`/`perPage` max 100) and `GET
/v2/libraries/{libraryKey}` (single `LibraryResponse`). The list endpoint has
**no free-text name-search parameter** (only `websiteId(s)`/`libraryKeys`/
`accessIds` filters, verified in `thunder_openapi_v1.json`). So name matching is
done **client-side** over a cached directory.

`LibraryResponse` fields of interest: `id` (e.g. `clevnet`), `preferredKey`,
`name`, `status`, `type`, `isConsortium`. The returned slug is `preferredKey`
(falling back to `id`).

**Thunder client.** Add `ThunderClient.list_libraries(page, per_page)` that calls
`/v2/libraries`.

**Cost estimate + concurrent paging (HIGH-7).** `/v2/libraries` is large and
`perPage` caps at 100, so a naive sequential page-by-page walk can exceed the 30s
Lambda timeout. Instead the loader:

1. **Fetches page 1 first** to read `totalItems` from the paged response and
   compute `page_count = ceil(totalItems / 100)`.
2. **Fetches pages 2..N concurrently** with `asyncio.gather` (bounded fan-out),
   then concatenates the `{slug, name}` rows in page order.

**Shared in-flight lock (HIGH-7).** A module-level `asyncio.Lock` in
`slug_directory.py` guards the (re)load so that concurrent callers during a cold
window do not each kick off a full directory fetch — the first caller fetches
while the others await the same in-flight result (double-checked under the lock).

**TTL = 7 days** (configurable via `LIBBY_MCP_SLUG_TTL_SECONDS`). A module-level
`SlugDirectory` is reused across warm Lambda invocations (like the module-level
`ThunderClient`), so the directory is fetched at most once per TTL window
(NFR-2.2, AC-D5.5).

```python
class SlugDirectory:
    def __init__(self, client, ttl_seconds=7*24*3600):    # 7 days, configurable
        self._client = client
        self._ttl = ttl_seconds
        self._rows: list[dict] | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()                        # shared in-flight guard

    def _fresh(self) -> bool:
        return self._rows is not None and (time.monotonic() - self._fetched_at) < self._ttl

    async def entries(self) -> list[dict]:
        if self._fresh():
            return self._rows
        async with self._lock:
            if self._fresh():                              # double-check after awaiting lock
                return self._rows
            try:
                rows = await self._load_all_pages_concurrent()   # page 1 → totalItems → gather 2..N
                self._rows, self._fetched_at = rows, time.monotonic()
            except Exception:
                if self._rows is not None:
                    return self._rows                      # NFR-3.3: serve stale on fetch failure
                return _load_fallback_slugs()              # cold-start fallback (below)
        return self._rows
```

**Cold-start fallback (HIGH-7).** Ship a bundled `src/fallback_slugs.json` with
the top-100 libraries (`{slug, name}` rows). If the **live fetch fails with no
cache present** (e.g. a cold Lambda whose first directory fetch errors), the
directory serves the bundled fallback and the tool result carries a `warning` that
the data is a bundled fallback snapshot, not live. This keeps `lookup_library_slug`
useful on the common libraries even when Thunder is briefly unreachable.

**Tool.** `src/tools/lookup_library_slug.py`:

```python
async def handle(params, directory) -> dict:
    name = (params.get("name") or "").strip().lower()
    if not name:
        return {"error": "name is required"}
    try:
        entries = await directory.entries()          # async: concurrent paging + shared lock
    except Exception:
        return {"error": "library directory temporarily unavailable — try again"}
    matches = [
        {"slug": e["slug"], "name": e["name"]}
        for e in entries
        if name in (e["name"] or "").lower() or name in (e["slug"] or "").lower()
    ]
    return {"query": name, "matches": matches}      # empty list on no match (AC-D5.6)
```

Case-insensitive substring over both name and slug (AC-D5.3); empty list on no
match (AC-D5.6); raw data only, no ranking (AC-D5.7/NFR-5.1). `lookup_library_slug('Fairfax')`
matches the directory row whose name contains "fairfax" and returns its slug
`fairfax` (AC-D5.4).

### D6: Exact-Title Search Mode (FR-D6)

Add `exact: bool = False` to the `search_titles` tool signature. `exact` requires
`query` — the wrapper validates this before dispatch:

```python
if args.get("exact") and "query" not in args:
    raise ToolError("exact requires query")        # AC-D6.4
```

The filter is applied in `_build_result_block` (so it covers both single and
multi-library paths, AC-D6.6), after normalization and client filters, before
pagination counts are finalized:

```python
if params.get("exact") and params.get("query"):
    q = params["query"].strip().lower()
    titles = [t for t in titles if t["title"].strip().lower() == q]
```

`exact=false`/omitted leaves v2 fuzzy behavior intact (AC-D6.5).
`search_titles('The Mom Test', exact=true)` keeps only titles whose title is
exactly "the mom test" case-insensitively (AC-D6.3).

**Paging so an exact match isn't pushed off page 1 (HIGH-12).** The exact filter
runs client-side over whatever Thunder returned for the fetched page. With the v2
defaults (`availableFirst` ordering + a small `perPage`), available titles can
push the single exact match onto page 2 — a page-1-only filter would then return
zero results. So when `exact=true`:

1. Request `perPage=100` and `sort=relevance` (drop `availableFirst`, so the
   relevance-ranked exact title is near the front rather than ordered by
   availability).
2. Run the client-side exact filter over the fetched page.
3. **If the filtered result is empty and `total_items > 0`, fetch the next
   page(s) and re-filter — up to a 3-page maximum (300 items)** — stopping as soon
   as a match is found. This bounds the work while still catching an exact title
   that sits just beyond page 1.

> **Note on `filtered_count`.** When `exact` prunes titles, `filtered_count`
> reflects the post-exact count across the pages fetched (consistent with how
> `under_hours`/`bisac` client filters already affect it).

### D7: Multi-Library `get_availability` (FR-D7)

Mirror the `search_titles` fan-out already shipped in v2. The slug-list helpers
(`validate_library_slugs`, `dedupe_library_slugs`, `MAX_LIBRARIES`) live in
`search_titles.py`; v3 reuses them (import, or lift into the shared
`_slugs` util) so both tools share identical validation and messages (AC-D7.2/D7.4).

**Thunder client.** `ThunderClient.get_availability_async(slug, title_ids,
async_client)` and `get_library_async(slug, async_client)` already landed in
**Task D1** (HIGH-3) — the single-library `handle()` is async and awaits them
through `classify_and_fetch`. D7 adds **no** new Thunder method; it reuses the D1
async helpers for the fan-out.

**Tool.** `availability.py` gains `handle_multi(params, client)`, built on top of
the already-async D1 core (`classify_and_fetch` / `_availability_one`):

```python
async def _availability_one(slug, title_ids, client, async_client) -> dict:
    # HIGH-4/HIGH-5: SAME classify_and_fetch as the single path — so title_id
    # normalization, the non-empty check, the 400/404-only slug probe, and the
    # multi-ID per-ID retry behave identically here.
    try:
        block = await classify_and_fetch(slug, title_ids, client, async_client)
    except Exception as e:
        return {"library_slug": slug, "error": str(e)}
    return {"library_slug": slug, **block}   # block is {"titles": [...]} or {"error": ...}

async def handle_multi(params, client) -> dict:
    slugs = params["library_slugs"]                # normalized + validated + deduped
    title_ids = params["title_ids"]
    async with httpx.AsyncClient(headers=..., timeout=TIMEOUT_SECONDS) as ac:
        results = await asyncio.gather(
            *[_availability_one(s, title_ids, client, ac) for s in slugs]
        )
    return {"libraries": results}
```

**Handler.** The `get_availability` tool wrapper is **already `async def` as of
D1/PR-D1** (HIGH-3) — the single path `await`s the async `handle(...)`. D7 only
adds the `library_slugs: Optional[list[str]] = None` parameter (making
`library_slug` `Optional[str] = None`) and a branch to `await handle_multi(...)`.
The one-of validation and slug-list validation run in the wrapper before dispatch.

**`title_ids` parameter type and int→str coercion in the FastMCP wrapper
(HIGH-4/HIGH-A).** The registered MCP tool declares `title_ids: list[str | int]`
(not `list[str]`), so a client that sends a JSON number (e.g. `9876543`) rather
than a string is accepted by the FastMCP/Pydantic schema instead of being rejected
with a validation error before the tool body ever runs. The int→str coercion
(`str(tid).strip()`) MUST happen **inside the FastMCP tool wrapper, as the very
first step**, before any one-of/slug validation and before dispatch to
`handle(...)` / `handle_multi(...)` — i.e. the wrapper calls
`_normalize_title_ids(title_ids)` (which runs `str(tid).strip()` on every element
and drops blank-after-strip entries) at the top, then rejects an empty result with
`title_ids is required (non-empty)`. Because coercion is at the wrapper boundary,
an integer ID is already a stripped numeric string by the time any validation,
dedupe, or Thunder call sees it. `classify_and_fetch` re-applies the identical
`_normalize_title_ids` defensively, so `handle()` is still safe if called directly,
but the wrapper is the first and authoritative coercion point so the behavior holds
when the tool is driven through the FastMCP app (as a real client does), not only
through `handle()`.
Single-slug `await handle(...)` keeps the flat v2 shape plus the D1/D3 changes
(AC-D7.6), multi-slug `await handle_multi(...)` returns the grouped
`{libraries: [...]}` shape. Per-library failures are isolated (AC-D7.5) and logged
with slug + status (NFR-7.1). Fan-out uses `asyncio.gather` (AC-D7.7, NFR-2.1).

> **Async boundary (HIGH-3).** Because `handle()` is async from D1, the FastMCP
> tool function registered in `handler.py` is an `async def` from PR-D1 onward
> (FastMCP awaits it on the running event loop, exactly as Mangum drives the
> async `search_titles`). This is what lets PR-D1 compile and pass mypy-strict
> without importing anything D7 adds later.

**Response shapes:**

```json
// single (unchanged shape + D1/D3)
{ "library_slug": "lcpl",
  "titles": [
    {"title_id": "9876543", "copies_owned": 3, "copies_available": 2, "holds_count": 0, "estimated_wait_days": 0},
    {"title_id": "bad-title-id", "status": "not_owned"}
  ] }

// multi (grouped, parallel to search_titles)
{ "libraries": [
    {"library_slug": "lcpl", "titles": [ ... ]},
    {"library_slug": "fcpl", "titles": [ ... ]},
    {"library_slug": "badslug", "error": "unknown library slug: badslug — check the slug is correct (e.g. lcpl, fairfax, nypl)"}
  ] }
```

---

## Group E — Docker Standalone

### E1: Single-Codebase Auto-Detect (FR-E1)

`handler.py` keeps its v2 factory `_build_app()` (returns
`AuthMiddleware(router)`) and `lambda_handler(event, context)` unchanged in
signature (AC-E1.5). The only addition is a runtime switch that affects **how**
the app is served, never a code fork (AC-E1.1):

```python
import os

_IS_LAMBDA = "AWS_LAMBDA_RUNTIME_API" in os.environ   # E1 auto-detect

def lambda_handler(event, context):
    # Only reached under Lambda; Mangum wraps the same _build_app() as v2.
    return Mangum(_build_app(), lifespan="on")(event, context)

# Docker / local: uvicorn imports the factory and serves the ASGI app directly.
#   uvicorn src.handler:_build_app --factory --host 0.0.0.0 --port 8000
# No asgi fork: both runtimes build the identical AuthMiddleware(router) app.
```

- Lambda (present `AWS_LAMBDA_RUNTIME_API`): Mangum wraps `_build_app()` exactly
  as v2 (AC-E1.2).
- Docker/local (absent): uvicorn serves `src.handler:_build_app` with `--factory`
  — the exact pattern the existing `Dockerfile.integration` already proves
  (AC-E1.3). Path stays `/mcp` in both (AC-E1.4).

The `_IS_LAMBDA` flag is available for anything that genuinely needs it (e.g.
choosing a default base URL for the OAuth `resource_metadata` host), but tool and
auth logic are identical across runtimes.

### E2 / E3: Docker Auth + Deliverables (FR-E2, FR-E3)

Docker auth is just the existing `AuthMiddleware` reading `LIBBY_MCP_API_KEY`
(AC-E2.5) — no Docker-specific auth. Key set → Bearer required (AC-E2.1/E2.4);
key unset → unauthenticated (AC-E2.2). `curl` behaviors in AC-E2.3/E2.4 follow
directly from `AuthMiddleware`. The `curl` `initialize` body MUST carry a valid
`InitializeRequestParams` (`protocolVersion`, `capabilities`, `clientInfo`) and
an `Accept: application/json, text/event-stream` header (HIGH-2) — a bare
`params:{}` returns JSON-RPC -32602 and a missing `Accept` is rejected by the
Streamable HTTP transport. `GET /mcp` returns HTTP 405 regardless of auth
(the method guard precedes the auth check).

**`Dockerfile` (multi-stage, non-root, root of repo):**

```dockerfile
# ---- builder: install deps into a venv ----
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
 && /opt/venv/bin/pip install --no-cache-dir "uvicorn[standard]>=0.30"

# ---- runtime: copy venv + source, run as non-root ----
FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY src/ ./src/
ENV PATH="/opt/venv/bin:$PATH" PYTHONPATH=/app
USER appuser
EXPOSE 8000
# HIGH-10: honor forwarded scheme/host behind a TLS proxy (--proxy-headers / --forwarded-allow-ips).
# Exec form (JSON array) is required so the process receives signals directly (no shell wrapper).
CMD ["uvicorn", "src.handler:_build_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
```

**Python version (HIGH-11).** The deployed CDK Lambda already targets
`lambda.Runtime.PYTHON_3_12` (`infra/lib/libby-mcp-stack.ts`), and
`Dockerfile.integration` uses `python:3.12-slim`; v3 keeps **runtime 3.12**
everywhere and does **not** downgrade (a downgrade would break NFR-6.3 /
AC-E4.1 — "no behavioral change"). The code's compatibility **floor is 3.11**:
`pyproject.toml` `requires-python = ">=3.11"` and mypy/ruff `target-version = py311`,
so the source stays runnable on 3.11 while the Docker image and the Lambda both
run on 3.12 (NFR-6.4 "minimum 3.11, runtime 3.12"). The root `Dockerfile` above
uses `python:3.12-slim` to match the Lambda runtime and the integration image.
Single worker because
`StreamableHTTPSessionManager.run()` may be called only once per FastMCP instance
and `_build_app()` creates one per factory call (same rationale as the integration
image). uvicorn is installed only in the Docker image, not in `requirements.txt`
(AC-E4.3).

**`docker-compose.yml`:**

```yaml
services:
  libby-mcp:
    build: { context: ., dockerfile: Dockerfile }
    image: libby-mcp:local
    # HIGH-6: bind loopback only by default — auto-approve / OAuth consent must
    # never be reachable on a published port. Operators who truly want to expose
    # the port change this to "8000:8000" AND must set LIBBY_MCP_API_KEY or OAuth.
    ports: ["127.0.0.1:8000:8000"]
    # HIGH-2: use the LIST form so a var that is *unset on the host* is NOT passed
    # into the container at all (no empty-string injection). The `${VAR:-}` MAP
    # form would set the container var to "" (present-but-empty); even though the
    # server normalizes "" → unset (F0/NFR-8.1), the compose file avoids injecting
    # empties so a bare `docker compose up` yields exactly the documented defaults.
    environment:
      - LIBBY_MCP_API_KEY           # unset on host → not passed → no-auth (Docker only)
      - LIBBY_MCP_OAUTH_ENABLED
      - LIBBY_MCP_OAUTH_SIGNING_KEY     # REQUIRED when OAuth on (CRITICAL-2)
      - LIBBY_MCP_OAUTH_OWNER_SECRET    # owner consent gate (CRITICAL-3); defaults to API key. REQUIRED (or API key) when OAuth on — no silent auto-approve (CRITICAL-1)
      - LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE # dev-only; honored only off-Lambda + loopback base URL (CRITICAL-1)
      - LIBBY_MCP_PUBLIC_BASE_URL        # required for non-loopback OAuth (HIGH-10)
      - LIBBY_MCP_OAUTH_ACCESS_TTL_SECONDS   # default 3600 (HIGH-6)
      - LIBBY_MCP_OAUTH_REFRESH_TTL_SECONDS  # default 2592000 (HIGH-6)
      - LIBBY_MCP_OAUTH_CODE_TTL_SECONDS     # default 60 (HIGH-6)
    healthcheck:
      test: ["CMD","python","-c","import urllib.request as u,urllib.error as e,sys;\n\
try:\n u.urlopen(u.Request('http://localhost:8000/mcp'),timeout=3)\n\
except e.HTTPError as x: sys.exit(0 if x.code in (401,405) else 1)\n\
except Exception: sys.exit(1)\nsys.exit(1)"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 5s
```

The default `127.0.0.1:8000:8000` binding keeps the port on loopback so the
dev-only OAuth auto-approve path (F1a / AC-F1.9) can never be reached from off-box
and auto-approve is never co-located with a published port (HIGH-6). Operators who
want to publish `8000:8000` must configure an auth mechanism
(`LIBBY_MCP_API_KEY` or OAuth) first.

Startup <30s is satisfied by the slim image + healthcheck `start_period`
(AC-E3.4). No AWS needed (AC-E3.7). `.dockerignore` is extended to exclude
`.git`, `infra`, `.venv`, `__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache`,
`.ruff_cache`, `cdk.out`, `node_modules`, `tests`, `docs` (AC-E3.5). The README
gains a Docker quickstart + Claude Desktop and Cursor static-header config
examples (AC-E3.6).

> The existing `Dockerfile.integration` / `docker-compose.integration.yml` stay
> for the local-integration harness; the new root `Dockerfile` /
> `docker-compose.yml` are the user-facing standalone artifacts. They share the
> same `src.handler:_build_app` factory, so they cannot drift in how the app is
> served.

### E4: Lambda Deploy Unchanged + OAuth Routes (FR-E4)

When OAuth is **disabled** (the default), there is no change to
`infra/libby-mcp-stack.ts` behavior (AC-E4.1); it still sets `LIBBY_MCP_API_KEY`
from Secrets Manager and routes `POST`/`GET /mcp`. OAuth env vars (F) are added as
**optional** stack parameters defaulting to disabled, so an owner who does not
enable OAuth sees no change and the stack stays cloud-agnostic (AC-E4.4 /
NFR-6.2). The live endpoint keeps working (AC-E4.2 / NFR-6.3). No new mandatory
Lambda dependency (AC-E4.3): uvicorn is Docker-only; OAuth JWT validation uses
only the Python standard library (see F / HIGH-4), so nothing new is added to
`requirements.txt`.

**OAuth route wiring (critical).** The v2 stack routes **only** `POST /mcp` and
`GET /mcp`. The ASGI app that handles the OAuth endpoints (FR-F2) is never
reached unless API Gateway forwards those paths to the Lambda. Therefore, when
`LIBBY_MCP_OAUTH_ENABLED=true`, the CDK stack MUST add the following API Gateway
HTTP API routes, all pointing at the same Lambda integration as `/mcp`:

| Method & path | Purpose |
|---------------|---------|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 PRM |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 AS metadata |
| `POST /register` | RFC 7591 DCR |
| `GET /authorize` | authorization endpoint (consent page) |
| `POST /authorize` | authorization endpoint (consent form submit) |
| `POST /token` | token endpoint |

Both `GET /authorize` (render consent) and `POST /authorize` (submit the consent
form with the owner credential, CRITICAL-3) are routed. When OAuth is disabled,
none of these routes are added, so the stack is byte-for-byte identical to v2
(AC-E4.4). All six routes share the single Lambda integration — the ASGI
`router` dispatches internally by path (design F2), so no new Lambda or
integration is created.

**`LIBBY_MCP_PUBLIC_BASE_URL` on the Lambda (HIGH-10).** The OAuth URL generation
(F5a) refuses to emit spoofable URLs: with OAuth enabled and no
`LIBBY_MCP_PUBLIC_BASE_URL`, every OAuth route returns HTTP 500
`oauth_not_configured`. Nothing in v2 sets this variable, so the CDK stack MUST
set it when OAuth is enabled. The base URL is the API's **origin** (not including
the `/mcp` path): by default `httpApi.apiEndpoint`
(`https://<id>.execute-api.<region>.amazonaws.com`), with an optional
custom-domain override read from CDK context:

```ts
// infra/lib/libby-mcp-stack.ts (sketch, OAuth-enabled branch)
const publicBaseUrl =
  (this.node.tryGetContext("publicBaseUrl") as string | undefined) ??
  httpApi.apiEndpoint;                       // origin only; the /mcp path is NOT appended
handler.addEnvironment("LIBBY_MCP_PUBLIC_BASE_URL", publicBaseUrl);
handler.addEnvironment("LIBBY_MCP_OAUTH_ENABLED", "true");
handler.addEnvironment("LIBBY_MCP_OAUTH_SIGNING_KEY", signingKeySecret.secretValue.unsafeUnwrap());
```

The signing-key secret is generated with explicit, non-ambiguous settings
(HIGH-9) so it is a clean 64-char alphanumeric string:
`generateSecretString: { passwordLength: 64, excludePunctuation: true,
requireEachIncludedType: false }`. A `cdk synth` assertion (Task E4) confirms the
Lambda has `LIBBY_MCP_PUBLIC_BASE_URL` set when OAuth is enabled, absent when
disabled, and that `-c publicBaseUrl=...` overrides the default. The Lambda
runtime stays `PYTHON_3_12` (HIGH-11) — unchanged from the deployed v2 stack.

> Because the embedded AS is stateless (CRITICAL-2 — all client IDs, codes, and
> tokens are signed, not stored), any Lambda instance can serve any of these
> routes regardless of which instance registered the client or minted the code.
> No shared datastore (DynamoDB/ElastiCache) is required in `infra/`.

---

## Group F — OAuth 2.1

> The MCP server is the **resource server**. The default **authorization server
> is embedded, minimal, and stateless** (only the endpoints the brief lists), so
> self-hosters need no Auth0 account. The default path signs/validates HS256 JWTs
> with the Python standard library (no new dependency). Auth0 MAY be configured as
> an optional backend provider via env (`LIBBY_MCP_OAUTH_PROVIDER=auth0` +
> issuer/JWKS) but is never required and is out of scope for the stdlib default;
> if an owner opts into it, any JWKS/RS256 verification support it needs is their
> own add-on and not part of the Lambda mandatory path (brief decision).

### F0: Environment-Variable Reading (unset ≡ empty string) (HIGH-2)

Every config var is read through one helper so that an **unset** var and an
**empty-string** var (`""`) are indistinguishable — both yield `None` and the
documented default applies (NFR-8.1):

```python
def _env(name: str) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None          # "" and "   " normalize to None (unset)
```

All call sites use `_env(...)` rather than `os.environ.get(...)` directly:
`_auth_mode()`, `_oauth_enabled()` (`_env("LIBBY_MCP_OAUTH_ENABLED") == "true"`),
the effective-owner-secret resolution (`_env("LIBBY_MCP_OAUTH_OWNER_SECRET") or
_env("LIBBY_MCP_API_KEY")`), the signing-key / base-URL / TTL readers, and the
`LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE` flag. This is what makes the "set-and-non-empty"
wording in AC-F1.6/F1.8 mechanical: an empty `LIBBY_MCP_API_KEY` is **not** a key,
an empty `LIBBY_MCP_PUBLIC_BASE_URL` is **not** a base URL, and an empty
`LIBBY_MCP_OAUTH_ENABLED` is **not** `true`.

Because of this normalization, a `docker-compose.yml` that still ends up passing
`VAR=""` for an un-provided optional var is safe — the server reads it as unset.
Even so, the compose file prefers **not** to inject empty strings (see E2/E3):
optional OAuth vars are passed conditionally so a bare `docker compose up` yields
exactly the documented defaults (NFR-8.2).

### F1: Three Auth Modes in One Middleware (FR-F1)

`AuthMiddleware` reads two env vars at request time (via `_env`, F0) and composes
the modes:

| `LIBBY_MCP_API_KEY` | `LIBBY_MCP_OAUTH_ENABLED` | Mode | `/mcp` behavior |
|---------------------|---------------------------|------|-----------------|
| unset | not `true` | **No-auth** | Docker: serve unauthenticated (AC-F1.1); Lambda: 500 `authentication_not_configured` (AC-F1.1-lambda) |
| set | not `true` | **Static Bearer** | require `Bearer <key>` (AC-F1.2) |
| unset | `true` | **OAuth** | require valid JWT access token (AC-F1.3) |
| set | `true` | **Static Bearer + OAuth** | accept Bearer if it equals the static key **or** validates as a JWT (AC-F1.4) |

Decision order for a presented Bearer token (AC-F1.4): constant-time compare
against `LIBBY_MCP_API_KEY` first (cheap, no network); on mismatch, if OAuth is
enabled, validate as a JWT; else reject. The POST-only guard and `/mcp` scoping
from v2 are preserved; the OAuth well-known/registration/authorize/token routes
are mounted outside the `/mcp` auth gate (they must be reachable unauthenticated).

**Middleware order (HIGH-1).** `AuthMiddleware.__call__` applies these checks in
this exact order so GET OAuth-discovery requests and the consent page are not
accidentally 405'd or 401'd:

1. **Non-HTTP scope** (`scope["type"] != "http"`, e.g. the ASGI `lifespan`
   message) passes straight through — the auth gate never touches lifespan.
2. **OAuth-public paths bypass auth entirely** when OAuth is on: `/.well-known/*`,
   `/register`, `/authorize` (both GET and POST), and `/token`. These are reached
   by unauthenticated clients and by browsers, so the auth gate must not run on
   them. (The owner-consent check on `/authorize` is enforced *inside* the
   authorize handler, F1a — not by `AuthMiddleware`.)
3. **Non-POST on `/mcp` → HTTP 405.** `GET /mcp` (and other non-POST methods) is
   rejected with 405 before any auth logic — this is why the Docker curl ACs use
   `POST` (AC-E2.3/E2.4, HIGH-1).
4. **Auth check on `POST /mcp`** per the selected mode (none / static Bearer /
   OAuth / both), including the Lambda fail-closed rule (HIGH-13).

```python
async def __call__(self, scope, receive, send):
    # (1) non-http (lifespan) passes through
    if scope["type"] != "http":
        return await self._app(scope, receive, send)
    path = scope.get("path", "")
    # (2) OAuth metadata + flow endpoints are public (owner gate lives in the handler)
    if _oauth_enabled() and _is_oauth_public_path(path):
        return await self._app(scope, receive, send)
    # (3) non-POST on /mcp → 405 (before any auth)
    if path.rstrip("/") == "/mcp" and scope.get("method") != "POST":
        return await self._method_not_allowed(send)      # HTTP 405
    # (4) auth check on /mcp
    mode = _auth_mode()                       # from env
    if _IS_LAMBDA and mode == "none":
        # HIGH-13: Lambda must not fail open; log once, 500 on first request
        return await self._auth_not_configured(send)     # HTTP 500 authentication_not_configured
    if mode == "none":
        return await self._app(scope, receive, send)      # Docker no-auth only
    headers = _decode_headers(scope)
    if _authorized(headers, mode):            # static key and/or JWT
        return await self._app(scope, receive, send)
    return await self._unauthorized(send, mode, scope)   # 401 (+ RFC 9728 in OAuth mode)
```

Same middleware runs under Lambda and Docker (AC-F1.5). README documents the
table (AC-F1.7).

> **Lambda fail-closed (HIGH-13).** No-auth mode (no `LIBBY_MCP_API_KEY`, no
> OAuth) is permitted **in Docker only**. On Lambda (`AWS_LAMBDA_RUNTIME_API`
> set), serving `/mcp` with no auth mechanism would silently expose the server to
> anyone with the URL. So when `_IS_LAMBDA` is true and neither `LIBBY_MCP_API_KEY`
> nor OAuth is configured, `/mcp` requests return HTTP 500
> `authentication_not_configured`. To keep health checks working, this is enforced
> **on the first request** (logged as an error) rather than crashing at import, so
> a mis-wired deploy fails closed instead of fail-open (AC-F1.1-lambda).

### F1a: Owner Consent Gate on `/authorize` (CRITICAL-3)

Without an owner check, anyone who can reach the public URL could register a
client, drive `/authorize`, and mint a valid token — the embedded AS has no user
accounts, so "consent" would otherwise be a rubber stamp. v3 gates `/authorize`
on an **owner secret**:

- `GET /authorize` renders a minimal consent page containing a single secret
  field (plus the opaque OAuth params carried as hidden fields). Every reflected
  value on this page — including the attacker-controlled `client_name` from DCR —
  is HTML-escaped, and the response carries `Content-Security-Policy:
  frame-ancestors 'none'` and `Cache-Control: no-store` (XSS/clickjacking
  hardening, HIGH-C — see the "Consent-page output safety" note under F2).
- `POST /authorize` (the form submit) requires the owner secret; it is compared in
  constant time against `LIBBY_MCP_OAUTH_OWNER_SECRET` using `hmac.compare_digest`.
  On mismatch or absence, the server returns **HTTP 401 before issuing any code**
  (AC-F1.6). Only on a correct secret does it mint the signed authorization code
  and redirect.
- **Resolving the owner secret.** The effective owner secret is
  `LIBBY_MCP_OAUTH_OWNER_SECRET` if set (non-empty), else `LIBBY_MCP_API_KEY` if
  set (non-empty), so a single-owner self-host needs only one secret.

**Owner-secret gate — no silent auto-approve (CRITICAL-1).** The v2 draft fell
back to an unconditional "auto-approve" when no owner secret was configured, which
reopened the security hole: in OAuth mode with no credential, `/authorize` would
mint a token for anyone who reached the URL. v3 closes this:

- **Refuse to start when the owner secret is missing in OAuth mode.** When
  `LIBBY_MCP_OAUTH_ENABLED=true` and the effective owner secret is unset **or an
  empty string** (both `LIBBY_MCP_OAUTH_OWNER_SECRET` and `LIBBY_MCP_API_KEY`
  unset/empty), the server **refuses to start** with a clear configuration error
  (the same fail-fast behavior as the missing signing key, AC-F6.2). It MUST NOT
  auto-approve consent as a fallback.
- **Auto-approve is opt-in and loopback-only, and the loopback origin must be
  declared explicitly (HIGH-6).** The dev auto-approve path is reachable **only**
  when **all three** conditions hold:
  1. `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` is explicitly set, **and**
  2. the process is **not** on Lambda (`AWS_LAMBDA_RUNTIME_API` is absent), **and**
  3. `LIBBY_MCP_PUBLIC_BASE_URL` is **explicitly set (set-and-non-empty)** to a
     **loopback origin** whose host is exactly `127.0.0.1`, `localhost`, or `::1`
     (e.g. `http://127.0.0.1[:port]`, `http://localhost[:port]`,
     `http://[::1][:port]`). An **unset/empty** base URL does **not** count as
     loopback for auto-approve — there is no implicit loopback default here.

  **Refuse to start when auto-approve is requested but the loopback origin is not
  declared (HIGH-6 / AC-F1.10).** If `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` and
  `LIBBY_MCP_PUBLIC_BASE_URL` is unset/empty, `_build_app()` **refuses to start**
  with a config error naming `LIBBY_MCP_PUBLIC_BASE_URL` as required when
  auto-approve is requested — it does **not** silently fall back to requiring the
  owner secret and does **not** silently default the base URL to loopback. This
  forces the operator to declare `http://127.0.0.1:8000` (or similar) on purpose,
  so auto-approve can never be the accidental state of a deployment whose base URL
  was never configured.

  When auto-approve is active, the server emits a clearly-documented startup
  warning (`WARNING: /authorize is auto-approving consent (DEV mode) — never
  enable LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE on an exposed deployment`). If
  `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` is set but the Lambda check fails, or the
  base URL is set-but-non-loopback, auto-approve is **refused** — the server still
  requires the owner secret (startup fails if none is set, per the first bullet).
  Auto-approve is never the state of a non-loopback or Lambda deployment.
- **The owner-secret startup check (first bullet) applies unconditionally,
  including when auto-approve is enabled (AC-F1.8).** Enabling
  `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` never waives the "refuse to start with no
  effective owner secret" rule. Auto-approve only skips the interactive consent
  **form UI** at request time — it does **not** let the server run in OAuth mode
  without a configured owner secret. So in every mode (plain OAuth, or OAuth with
  auto-approve), a server with no effective owner secret refuses to start; there is
  no code path in which auto-approve runs against an unconfigured owner secret.
- **Never with a published port (HIGH-6 / AC-F1.11).** Auto-approve must never be
  used behind a published (non-loopback-bound) port. The shipped
  `docker-compose.yml` binds `127.0.0.1:8000:8000` by default so a bare
  `docker compose up` cannot expose an auto-approving `/authorize` off-box; the
  README documents that publishing `0.0.0.0:8000` together with the auto-approve
  flag is an unsupported misconfiguration. The loopback-origin requirement on
  `LIBBY_MCP_PUBLIC_BASE_URL` and the loopback port bind are complementary: the
  former gates the code path, the latter gates network reachability.

This is why E4/CRITICAL-1 routes **both** `GET /authorize` (render) and
`POST /authorize` (submit) to the Lambda.

### F2: OAuth Endpoints (FR-F2)

The endpoints are mounted on the same ASGI app that serves `/mcp` (so a single
host serves everything). An `src/oauth/` package provides the handlers and a tiny
router that `_build_app()` composes in front of the FastMCP app when
`LIBBY_MCP_OAUTH_ENABLED=true`:

```
src/oauth/
  __init__.py
  metadata.py     # PRM (RFC 9728) + AS metadata (RFC 8414) documents
  registration.py # POST /register (RFC 7591 DCR) — mints a SIGNED client_id
  authorize.py    # GET/POST /authorize (owner consent, PKCE S256, signed code)
  token.py        # POST /token (code→token, refresh rotation, RFC 6749 errors)
  jwt.py          # STATELESS signed tokens: sign/verify (HS256, stdlib hmac)
  router.py       # maps paths → handlers; mounted by _build_app()
```

> **No `store.py`.** The embedded AS keeps **no server-side state** — see the
> "Stateless, signed-token AS" section below (CRITICAL-2). Client IDs,
> authorization codes, access tokens, and refresh tokens are all self-contained
> signed tokens, so any Lambda instance validates artifacts minted by any other
> instance using the shared signing key. There is no in-memory client/code/token
> table to lose on a cold start.

Routing table (all additive to the v2 `/mcp` route):

| Method & path | RFC | Handler | Notes |
|---------------|-----|---------|-------|
| `GET /.well-known/oauth-protected-resource` | 9728 | `metadata.protected_resource` | points to AS; identifies resource |
| `GET /.well-known/oauth-authorization-server` | 8414 | `metadata.authorization_server` | `code_challenge_methods_supported: ["S256"]`; grants: `authorization_code`, `refresh_token` |
| `POST /register` | 7591 | `registration.register` | **public client, no `client_secret`** (HIGH-8); HTTP 201 with a **signed** `client_id` (stateless, F2a); 400 `invalid_redirect_uri` on bad redirect |
| `GET /authorize` | 6749/PKCE | `authorize.authorize_page` | render consent page; require `code_challenge` + `code_challenge_method=S256`; exact-match `redirect_uri` (F4a) |
| `POST /authorize` | 6749/PKCE | `authorize.authorize_submit` | owner-secret consent gate (F1a); on approval mint signed 60s code + redirect |
| `POST /token` | 6749/PKCE | `token.token` | form-encoded; code→token; refresh rotation (stateless) |

**AS metadata document (AC-F2.2):**

```json
{
  "issuer": "https://<host>",
  "authorization_endpoint": "https://<host>/authorize",
  "token_endpoint": "https://<host>/token",
  "registration_endpoint": "https://<host>/register",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"]
}
```

**PRM document (AC-F2.1):**

```json
{
  "resource": "https://<host>/mcp",
  "authorization_servers": ["https://<host>"],
  "bearer_methods_supported": ["header"]
}
```

`<host>` (the base URL) comes from `LIBBY_MCP_PUBLIC_BASE_URL` in non-loopback
deployments (F5a / HIGH-10), and only from the request scheme + Host header when
the resolved base is loopback (local dev). This makes the generated URLs correct
for the Lambda endpoint and a Docker host without being spoofable via the Host
header (AC-F3.2, AC-F10.2). The two documents are kept consistent — the PRM's
`authorization_servers[0]` is exactly the AS metadata `issuer` (AC-F3.3).

**Authorize (AC-F2.4):** reject when `code_challenge` is missing or
`code_challenge_method != S256`; validate the `client_id` HMAC and exact-match the
`redirect_uri` against the registered value (HIGH-9); require the owner credential
on the consent form (CRITICAL-3). On approval, mint a signed JWT authorization
`code` (`typ: "code"`, configurable code TTL — 60s default) embedding the
`code_challenge`, `redirect_uri`, `client_id`, and the **canonical** bound
`resource` (F4b — a non-canonical `resource` param is rejected with
`invalid_target` *before* a code is minted; a missing one defaults to canonical;
F2a — no server-side code table); redirect to the registered `redirect_uri` with
`code` (+ `state`).

**Consent-page output safety — XSS hardening (HIGH-C).** The `GET /authorize`
consent page reflects request- and registration-derived values back into HTML:
`state`, `redirect_uri`, `client_id`, the `code_challenge`, the `resource`, and
the registered `client_name`. **`client_name` is attacker-controlled** — any party
can run DCR (`POST /register` is public, no auth) and register a client whose
`client_name` contains markup like `<script>…</script>`; that value is embedded in
the signed `client_id` and surfaces on the consent page. To prevent
reflected/stored XSS:

- **HTML-escape every reflected value.** Every value interpolated into the consent
  HTML — `state`, `client_name`, `redirect_uri`, `client_id`, `code_challenge`,
  `resource`, and any other request- or registration-sourced field — MUST be
  passed through `html.escape(value, quote=True)` (stdlib) before it is written
  into the page, in **both** element-text and attribute contexts (the hidden-input
  `value="…"` attributes that carry the opaque OAuth params included). No reflected
  value is ever concatenated raw into the markup; only the fixed form skeleton is a
  static literal.
- **Security headers on the HTML consent response.** The `GET /authorize`
  consent-page response (and any `POST /authorize` response that renders HTML, e.g.
  the owner-mismatch / bad-`redirect_uri` error pages) sets:
  - `Content-Security-Policy: frame-ancestors 'none'` — the consent page cannot be
    framed, blocking clickjacking of the owner-secret form; and
  - `Cache-Control: no-store` — the owner-secret form and any reflected parameters
    are never cached by the browser or an intermediary.

  These headers are applied to the HTML consent/error responses specifically; the
  JSON OAuth endpoints (`/register`, `/token`, metadata) are unaffected.

This hardening is purely output-side: it does not change which clients are accepted
or the DCR validation — an attacker-controlled `client_name` is still *registered*,
it is merely rendered inert. A test asserts a `client_name` containing `<script>`
appears escaped (`&lt;script&gt;`) in the rendered consent HTML and is not executed
(F3).

**Token (AC-F2.5/F2.6):** `application/x-www-form-urlencoded`. For
`grant_type=authorization_code`: verify the authorization code with
`verify(code, expected_typ="code", ...)` — signature + `exp` + `typ` (it is a
configurable-TTL signed JWT, 60s default, F2a) — read the embedded PKCE
`code_challenge`, verify `S256(verifier) == code_challenge`, then issue a signed
JWT access token (`typ: "access"`, access TTL) + a signed JWT refresh token
(`typ: "refresh"`, refresh TTL), binding `aud` on **both** to the **canonical
resource** `<base>/mcp` (F4b, AC-F11.5) — the code's bound `resource` is already
canonical, and any `resource` param on `/token` is re-validated with
`_resolve_resource` (non-canonical → `invalid_target`, AC-F11.3). For
`grant_type=refresh_token`: verify the refresh JWT with
`verify(refresh, expected_typ="refresh", ...)` — so an **access token or a code
replayed here is rejected** (HIGH-5) — then **rotate** it (issue a new refresh JWT
with a fresh `jti`/`iat`) per OAuth 2.1 for public clients, and issue a new access
token. No server-side lookups — all state rides in the signed tokens (F2a).
Errors follow RFC 6749 / RFC 8707: unknown/expired/invalid/wrong-`typ` code or
refresh token → `invalid_grant`; bad client (`client_id` fails HMAC check) →
`invalid_client`; a non-canonical `resource` → **`invalid_target`** (F4b,
AC-F11.3); malformed → `invalid_request` (AC-F5.2).

**JWT validation on `/mcp` (AC-F5.3, NFR-2.3):** `jwt.py` calls
`verify(token, expected_typ="access", expected_iss=<base_url>)`, which pins the
header `alg` to `HS256` (rejecting `none`/other algs), checks the signature with
`hmac.compare_digest`, and validates `exp`, `typ == "access"`, `iss`, and
`aud == canonical_resource` (`<base>/mcp`, F4b / AC-F11.6) locally using the
shared signing key — no per-request introspection call. A token whose `aud` is
not the canonical resource is rejected (401). Because `typ` is enforced, a
**refresh token or an authorization code presented as a Bearer on `/mcp` is
rejected** (HIGH-5). The
implementation is **Python-stdlib-only** (HIGH-4): HS256 computed with
`hmac.digest(key, header_b64 + '.' + payload_b64, 'sha256')`, base64url via
`base64`, and JSON via `json`. No PyJWT or other third-party JWT library is used,
so the Lambda path adds **zero new packages** to `requirements.txt` (AC-E4.3 /
NFR-6.4). See the stateless design below.

### F2a: Stateless, Signed-Token AS (CRITICAL-2)

On Lambda, each execution environment has its own memory, and cold starts create
fresh instances. An AS that stored clients/codes/tokens in process memory would
hand a client a `client_id` on one instance and then reject it with "unknown
client" on the next. The embedded AS therefore holds **no server-side state** —
every artifact it issues is a self-contained signed token verifiable by any
instance that shares the signing key.

**Signing key (required when OAuth is on).** A new env var
`LIBBY_MCP_OAUTH_SIGNING_KEY` is a secret string of **at least 32 bytes**. There
is **one decode path and no encoding rule** (HIGH-9): the HMAC key is
`hashlib.sha256(secret.encode("utf-8")).digest()` — a fixed 32-byte key derived
from the raw secret. The old "32-byte key, hex- or base64-encoded" wording was
ambiguous (64 hex chars are also valid base64, so a length check could not tell
which was meant and `base64.b64decode` of a hex string silently yields the wrong
key); SHA-256-of-the-raw-bytes removes the ambiguity entirely and accepts any
sufficiently-long secret. When `LIBBY_MCP_OAUTH_ENABLED=true` and the secret is
unset **or shorter than 32 bytes**, **the server MUST refuse to start** (fail fast
with a clear configuration error) — it must never fall back to a random
per-instance key, because that would reintroduce the cross-instance mismatch.

In Lambda the secret is injected from Secrets Manager, auto-generated with
**explicit** `generateSecretString` settings so it is a clean 64-char
alphanumeric string (not a punctuation-heavy password that could fail a length or
decode check): `generateSecretString: { passwordLength: 64,
excludePunctuation: true, requireEachIncludedType: false }`. 64 ASCII chars = 64
bytes ≥ 32, and SHA-256 of those bytes is the HMAC key. In Docker the secret is
passed via the environment.

**Typed artifacts — no token confusion (HIGH-5).** Because one HMAC key signs
every artifact (client IDs, authorization codes, access tokens, refresh tokens), a
token of one kind must never be accepted where another kind is expected (e.g. a
refresh token presented as a Bearer on `/mcp`, or an access token replayed at
`/token`). `jwt.py` enforces this with two mandatory claims on every signed JWT
and a pinned algorithm:

- **`typ` claim** — one of `"access"`, `"refresh"`, `"code"`. The `client_id`
  signed token carries `typ: "client"`. Each verifier is called with the exact
  `typ` it expects and **rejects any token whose `typ` does not match**:
  `verify(token, expected_typ="access")` on `/mcp`, `expected_typ="code"` for the
  `authorization_code` grant, `expected_typ="refresh"` for the `refresh_token`
  grant. A refresh token or a code presented on `/mcp` fails (401); an access
  token presented at `/token` fails (`invalid_grant`).
- **`iss` claim** — set to the AS issuer (the `LIBBY_MCP_PUBLIC_BASE_URL`-derived
  base URL); verifiers reject a token whose `iss` is absent or does not match.
- **Pinned algorithm.** The JWT header `alg` MUST be `HS256`. `verify` rejects a
  token whose header `alg` is `none` or anything other than `HS256` (defeats the
  classic alg-confusion / `alg:none` attack) **before** computing the HMAC.
- **Constant-time comparison.** Every signature check and every secret comparison
  (`client_id` HMAC, authorization-code signature, access/refresh signature, owner
  secret, static API key) uses `hmac.compare_digest`.

**1. Client IDs are signed tokens.** `POST /register` does not persist the client.
It returns a `client_id` that is itself a signed token:

```
client_id = base64url(payload) + "." + base64url(HMAC_SHA256(signing_key, base64url(header)+"."+base64url(payload)))
header    = {"alg": "HS256", "typ": "JWT"}
payload   = {"typ": "client", "iss": "<base_url>", "redirect_uris": [...], "client_name": ..., "iat": ...}
```

Any instance validates a presented `client_id` by recomputing the HMAC over its
header+payload with `hmac.compare_digest` — no shared client table needed. **All
registered clients are public (PKCE, no `client_secret`)** — see F2b (HIGH-8); the
HMAC covers only the public payload above.

**2. Authorization codes are short-lived signed JWTs.** `/authorize` issues a code
that is a signed JWT with `typ: "code"`, `iss`, and a configurable
**code TTL (`LIBBY_MCP_OAUTH_CODE_TTL_SECONDS`, default 60s)** carrying the PKCE
`code_challenge`, the `redirect_uri`, the `client_id`, and the bound `resource`.
No server-side code table — `/token` validates the code's signature, `exp`, and
`typ == "code"` and reads the challenge straight out of the verified payload.

**3. Refresh tokens are signed JWTs (stateless).** A refresh token is a signed JWT
with `typ: "refresh"`, `iss`, and a configurable **refresh TTL
(`LIBBY_MCP_OAUTH_REFRESH_TTL_SECONDS`, default 2592000s = 30 days)**. Rotation
(OAuth 2.1) is achieved by issuing a **new** refresh JWT with a fresh `jti`/`iat`;
the old one remains valid until its own `exp` (see the documented deviation in
AC-F2.6/F2.9/F2.10 — a stateless AS has no replay-detection list).

**4. Access tokens are signed JWTs.** `typ: "access"`, `iss`, `aud`/`resource`
bound to this MCP server, `exp` from a configurable **access TTL
(`LIBBY_MCP_OAUTH_ACCESS_TTL_SECONDS`, default 3600s)**, and `iat`.

**5. Cross-instance validation.** Because every artifact is signed with the shared
`LIBBY_MCP_OAUTH_SIGNING_KEY`, a token minted by instance A validates on instance
B with no shared datastore — this is exactly what makes the AS correct on Lambda
(AC-CRITICAL-2). The signing/verify helpers live in `jwt.py` and are the only
place HMAC is computed; `verify(token, expected_typ, expected_iss)` performs the
alg pin, signature check (`hmac.compare_digest`), `exp`, `typ`, and `iss`
validation in one place.

### F2b: Public-Client DCR Response (HIGH-8)

RFC 7591 defaults an unspecified `token_endpoint_auth_method` to
`client_secret_basic`, which would make the client confidential and require a
secret. That contradicts the AS metadata (`token_endpoint_auth_methods_supported:
["none"]`, AC-F2.3) and the PKCE-only model. v3 therefore **registers every client
as public** and says so explicitly in both the metadata and the DCR response —
there is **no `client_secret`** anywhere (no issuance, no storage, nothing folded
into the signed `client_id`).

`POST /register` returns **HTTP 201** with exactly this body:

```json
{
  "client_id": "<signed stateless token (F2a)>",
  "client_id_issued_at": 1760000000,
  "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none"
}
```

No `client_secret` / `client_secret_expires_at` field appears. Validation at
registration (HIGH-9 / F4a): a missing/empty `redirect_uris`, or any entry that
violates the scheme rules (non-loopback `http://`, or an unsupported scheme), is
rejected with **HTTP 400 and `{"error": "invalid_redirect_uri"}`** (RFC 7591
§3.2.2) — a registration never silently accepts an unusable redirect URI. The
accepted `redirect_uris` are embedded in the signed `client_id` so `/authorize`
can exact-match them without a server-side client table (F2a).

### F3: RFC 9728 Discovery (FR-F3)

In OAuth mode, the 401 for an unauthenticated/invalid-token `/mcp` request carries:

```
WWW-Authenticate: Bearer resource_metadata="<base_url>/.well-known/oauth-protected-resource"
```

(AC-F3.1). The URL is absolute and **computed from `LIBBY_MCP_PUBLIC_BASE_URL`**
(not the request Host header), so it is correct and non-spoofable for both the
Lambda endpoint and a Docker host (AC-F3.2, HIGH-7/HIGH-10). The base URL is
resolved by the shared helper used for all generated OAuth URLs (F5a): it returns
`LIBBY_MCP_PUBLIC_BASE_URL` verbatim when set; the request scheme + Host header is
used **only** when the resolved base is loopback (local dev). When
`LIBBY_MCP_PUBLIC_BASE_URL` is unset and the request Host is non-loopback, the
OAuth routes return HTTP 500 `oauth_not_configured` (AC-F10.1) rather than emit a
spoofable URL. In no-auth and static-Bearer-only modes the v2
`WWW-Authenticate: Bearer` header is unchanged (no `resource_metadata`), so
existing static-Bearer clients are unaffected.

### F4: Per-Client Compatibility (FR-F4)

The embedded AS accepts each target client's redirect URI via RFC 7591 Dynamic
Client Registration (DCR) and enforces PKCE S256 for all of them. **CIMD is not
used** (HIGH-8): the MCP authorization spec (2025-06-18) does not include CIMD, so
ChatGPT — like Claude.ai and Cursor — connects via DCR + RFC 9728 discovery. The
only ChatGPT-specific accommodation is tolerating the optional `resource=<url>`
parameter on `/authorize` and `/token`.

| Client | Flow | Redirect URI accepted | Discovery | Notes |
|--------|------|-----------------------|-----------|-------|
| Claude.ai web | Auth Code + PKCE (S256) | `https://claude.ai/api/mcp/auth_callback` | RFC 9728 + DCR | no manual registration (AC-F4.1) |
| ChatGPT | Auth Code + PKCE (S256) | `https://chatgpt.com/connector_platform_oauth_redirect` | RFC 9728 + DCR | tolerate `resource=<url>` on authorize/token (AC-F4.2); no CIMD |
| Cursor | Auth Code + PKCE (S256) | `cursor://anysphere.cursor-mcp/oauth/callback` | DCR | or static header, no OAuth (AC-F4.3) |
| Claude Desktop (stdio) | — | — | — | env-var creds, unchanged (AC-F4.4) |

`client_credentials` is never advertised or accepted (AC-F4.5). The `resource`
parameter sent by ChatGPT is validated against the canonical resource (F4b) and,
when canonical or omitted, bound into the issued token's audience.

### F4b: Canonical Resource / Audience (RFC 8707) (HIGH-7)

The server has exactly **one** resource identity. Defining it once removes the
ambiguity in the earlier draft (which "accepted and bound whatever `resource` the
client sent" — letting a client set a foreign audience).

**Canonical value.** `canonical_resource = f"{base}/mcp"`, where `base` is the
resolved `LIBBY_MCP_PUBLIC_BASE_URL` origin (F5a / HIGH-10; only Host-derived when
the base is loopback). This is exactly the PRM `resource` field (AC-F2.1 / F11.1),
so discovery advertises the same string the server enforces.

**Normalization + comparison.** A single optional trailing slash on the path is
normalized away before comparison, so `…/mcp` and `…/mcp/` are equal; `…/mcp` is
the stored/advertised canonical form. No other variation (different path, added
query/fragment, different host/scheme) is considered equal (AC-F11.2).

```python
def _canonical_resource(base: str) -> str:
    return f"{base.rstrip('/')}/mcp"

def _normalize_resource(value: str) -> str:
    # strip exactly one trailing slash on the path for comparison (AC-F11.2)
    return value.rstrip("/")

def _resolve_resource(sent: str | None, base: str) -> str:
    canonical = _canonical_resource(base)
    if sent is None or sent.strip() == "":
        return canonical                                    # AC-F11.4: default to canonical
    if _normalize_resource(sent.strip()) != _normalize_resource(canonical):
        raise OAuthError("invalid_target")                  # AC-F11.3: reject foreign resource
    return canonical                                        # always store the canonical form
```

**Rules (AC-F11.3–F11.6):**
- Every client **should** send `resource`; a **missing/empty** `resource` on
  `/authorize` or `/token` **defaults to the canonical value** (not an error).
- A `resource` that is not canonical (after trailing-slash normalization) is
  rejected with the RFC 8707 error **`invalid_target`** — `400` on `/token`, and a
  `400` / `invalid_target` error on `/authorize` (never a silently-issued foreign
  token).
- The authorization code's bound `resource`, and the `aud` of **both** the access
  and refresh tokens, are **always** `canonical_resource` — never the raw client
  value. A client cannot widen or redirect the audience; it is canonical or
  rejected.
- `/mcp` access-token validation checks `aud == canonical_resource` alongside
  signature / `exp` / `typ` (AC-F11.6); a mismatched `aud` → 401.

### F4a: Redirect-URI Rules (HIGH-9)

The redirect-URI validation has to reconcile two facts: non-loopback web clients
must use `https`, but Cursor registers a custom app scheme (`cursor://`). The
rules:

**Allowed schemes at registration (`POST /register`) and at `/authorize`:**
- (a) `https://` for any host (Claude.ai, ChatGPT).
- (b) `http://` only for loopback hosts (`127.0.0.1`, `localhost`) — local dev.
- (c) custom app schemes registered at DCR time (e.g. `cursor://...`). A custom
  scheme is accepted as a registered value and is **not** HTTPS-gated the way a
  web `http://` origin is.

**Exact match at `/authorize` (RFC 6749 §4.1.2.1).** The `redirect_uri` presented
to `/authorize` MUST **exact-string-match** a redirect URI registered for that
`client_id` (the registered set is carried in the signed `client_id`, F2a). If
`redirect_uri` is missing or does not exact-match a registered value, `/authorize`
returns an **HTTP 400 error page — not a redirect** (never bounce an error to an
unvalidated URI). Only after the redirect URI is validated does `/authorize`
redirect error/`code` responses back to it (AC-F9.1).

### F5: HTTPS / Transport (FR-F5)

The generated issuer/resource/well-known/`resource_metadata` URLs are all computed
from `LIBBY_MCP_PUBLIC_BASE_URL` (F5a, HIGH-7/HIGH-10) — never from the raw Host
header — so they are not attacker-controllable in a non-loopback deployment. The
`https` rule is defined on that resolved base URL (not on the request host):

- The resolved base URL scheme MUST be `https` **unless** its host is loopback
  (`127.0.0.1`, `::1`, or `localhost`), in which case `http` is permitted for
  development (AC-F5.1). A non-loopback `http://` base URL is treated as
  misconfiguration and the OAuth routes return HTTP 500 `oauth_not_configured`
  (AC-F10.1) rather than advertise an insecure endpoint.
- Custom app schemes (`cursor://`) are redirect-URI schemes, not base-URL schemes,
  and are exempt from the base-URL `https` rule (they are governed by the
  redirect-URI rules in F4a rule c).

This keeps a `docker compose up` + localhost redirect working for development while
requiring `https` for every exposed deployment.

### F5a: Trusted Issuer / Resource Base URL (HIGH-10)

Deriving the issuer, `resource`, and `/.well-known` URLs from the inbound `Host`
header is attackable (a spoofed `Host` makes the server advertise an attacker's
metadata URLs). v3 adds `LIBBY_MCP_PUBLIC_BASE_URL`:

- When set, the server MUST use it verbatim for **all** generated URLs (the two
  `/.well-known` URLs, the AS `issuer`, the PRM `resource`, and the
  `resource_metadata` challenge URL). The Host header is not consulted.
- Host-based derivation is permitted **only** when the resolved base URL is
  loopback (`localhost`/`127.0.0.1`) — i.e. local dev.
- In Docker/Lambda (non-loopback), `LIBBY_MCP_PUBLIC_BASE_URL` MUST be set to the
  public HTTPS URL. When it is unset and the request Host is not loopback, the
  OAuth routes return **HTTP 500 with `error: oauth_not_configured`** (AC-F10.1)
  rather than emit spoofable URLs.

**Behind a TLS proxy** (API Gateway, nginx, etc.) the app sees `http` on the
loopback interface; run uvicorn with `--proxy-headers
--forwarded-allow-ips='*'` so the forwarded scheme/host are honored, and still set
`LIBBY_MCP_PUBLIC_BASE_URL` as the authoritative source for generated URLs.

---

## Data Models

### `get_availability` title entry (v3)

Owned title:
```json
{ "title_id": "9876543", "copies_owned": 3, "copies_available": 2, "holds_count": 0, "estimated_wait_days": 0 }
```
Not-owned title (D1 — replaces v2 `{"error": "not_found"}`):
```json
{ "title_id": "bad-title-id", "status": "not_owned" }
```

### Title Object (search_titles) — v3 deltas

Unchanged from v2 except:
- `estimated_wait_days`: `0` when `available_copies > 0`; else raw value or `null` (D3).
- `is_abridged`: honest `true`/`false`/`null` (derive-from-edition-else-null) **or** removed entirely if no reliable source (D4).

All other v2 fields (`id`, `title`, `creator`, `format`, `available`,
`available_copies`, `owned_copies`, `holds_count`, `holds_per_copy`, `hours`,
`bisac_codes`, `series_name`, `series_position`, `narrator`, `publisher`,
`edition`, `link`) are retained.

### `lookup_library_slug` response (D5)

```json
{
  "query": "fairfax",
  "matches": [ { "slug": "fairfax", "name": "Fairfax County Public Library" } ]
}
```
Empty `matches` on no hit. When the live directory is unreachable and the bundled
`fallback_slugs.json` is used, the response adds a `"warning"` field noting the
data is a bundled snapshot (AC-D5.10). `{"error": "..."}` only when neither live,
cached, nor fallback data is available.

### OAuth documents

PRM (RFC 9728) and AS metadata (RFC 8414) as shown in F2.

---

## MCP Tool Surface (v3)

Three tools (up from two in v2):

| Tool | Change in v3 |
|------|--------------|
| `search_titles` | adds `exact: bool = False`; slug normalization; `estimated_wait_days` correction; honest `is_abridged` |
| `get_availability` | adds `library_slugs` fan-out; `not_owned` per-title status; `estimated_wait_days` correction; slug normalization |
| `lookup_library_slug` | **new** — name/partial-name → matching slugs, cached directory |

---

## Error Handling (v3 additions)

| Scenario | Where | Result |
|----------|-------|--------|
| Title not owned (absent from availability `items`) | `availability` per-title map | `{"title_id": tid, "status": "not_owned"}` (not an error) |
| Non-numeric / malformed `title_id` (HIGH-3) | `availability` pre-call filter | `{"title_id": tid, "status": "not_owned"}` emitted directly; the ID is not sent to Thunder (never a 500, AC-D1.2) |
| Availability endpoint **400/404**, slug valid (`/v2/libraries/{slug}` 200) (HIGH-3/HIGH-5) | `availability` probe branch | single ID → `not_owned`; multi-ID → per-ID retry so owned titles survive (title problem, not slug) |
| Availability endpoint **400/404**, slug invalid (`/v2/libraries/{slug}` 4xx) (HIGH-3/HIGH-5) | `availability` probe branch | top-level `error` `unknown library slug: <slug>` (single) / per-library `error` block (multi, AC-D7.5) |
| Availability endpoint `429`/`401`/`403`/`408`/`5xx`/timeout (HIGH-5) | `availability` error branch | top-level `error`, **no slug probe**, never reclassified |
| `exact=true` without `query` | `search_titles` wrapper | ToolError `exact requires query` |
| `get_availability` both/neither slug param | `get_availability` wrapper | ToolError `provide either library_slug or library_slugs, not both` / `library_slug or library_slugs is required` |
| Per-library availability failure (fan-out) | `_availability_one` | block with `library_slug` + `error`; others unaffected |
| Slug directory fetch fails, no cache | `lookup_library_slug` | `{"error": "library directory temporarily unavailable — try again"}` |
| Slug directory fetch fails, stale cache present | `SlugDirectory.entries` | serve stale rows (NFR-3.3) |
| Unauthenticated `/mcp` in OAuth mode | `AuthMiddleware` | 401 + `WWW-Authenticate: Bearer resource_metadata="…"` |
| Expired/unknown refresh token (bad signature or `exp`) | `oauth/token` | RFC 6749 `invalid_grant` |
| Non-canonical `resource` on `/authorize` or `/token` (HIGH-7) | `oauth/authorize`, `oauth/token` | RFC 8707 `invalid_target` (missing `resource` defaults to canonical, not an error) |
| Access token whose `aud` ≠ canonical `<base>/mcp` (HIGH-7) | `AuthMiddleware` JWT check | 401 (AC-F11.6) |
| OAuth enabled but `LIBBY_MCP_OAUTH_SIGNING_KEY` unset | startup / `_build_app` | refuse to start with a config error (AC-F6.2) |
| OAuth enabled but effective owner secret unset/empty | startup / `_build_app` | refuse to start with a config error (AC-F1.8, CRITICAL-1) |
| `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE=true` but `LIBBY_MCP_PUBLIC_BASE_URL` unset/empty | startup / `_build_app` | refuse to start, base URL required for auto-approve (AC-F1.10, HIGH-6) |
| `/authorize` with no valid owner credential | `oauth/authorize` | HTTP 401 before issuing a code (AC-F1.6, CRITICAL-3) |
| `/authorize` with unregistered/mismatched `redirect_uri` | `oauth/authorize` | HTTP 400 error page, not a redirect (AC-F9.1, HIGH-9) |
| OAuth route, non-loopback host, `LIBBY_MCP_PUBLIC_BASE_URL` unset | `oauth/*` | HTTP 500 `oauth_not_configured` (AC-F10.1, HIGH-10) |
| Lambda mode, no `LIBBY_MCP_API_KEY` and OAuth off | `AuthMiddleware` (first request) | HTTP 500 `authentication_not_configured` (AC-F1.1-lambda, HIGH-13) |

---

## Backward Compatibility & Migration

- `get_availability` callers that keyed on the per-title `{"error": "not_found"}`
  must switch to `{"status": "not_owned"}` (the one documented non-additive
  change, AC-D1.5 / NFR-1.1).
- Static-Bearer, `POST /mcp` on the live Lambda endpoint is byte-for-byte
  preserved (NFR-6.3). Enabling OAuth is strictly opt-in via env; a server with
  OAuth disabled behaves exactly as v2.
- `is_abridged` consumers: if the field is dropped (D4 branch 2), callers must
  stop expecting it; if kept, `null` now means "unknown" (never a fabricated
  `false`).
- New `lookup_library_slug` tool is purely additive.

---

## Testing Strategy

Mirrors the v1/v2 harness (`tests/`, `conftest.py`, fixtures, `make preflight`).

- **Fixtures:** extend `availability_response.json` with a **partial `items`
  array** that simulates Thunder omitting a not-owned title — i.e. the request
  asks for two IDs but `items` contains only the owned one, proving the absent ID
  is classified `not_owned` (D1 root cause). Keep an owned + a not-owned (absent)
  ID and an available title whose raw `estimatedWaitDays` is nonzero (to prove
  D3). Add the **HIGH-3** fixtures: a **4xx availability-endpoint** response and a
  **4xx `/v2/libraries/{slug}`** response (to exercise the bad-title vs. bad-slug
  branch), plus a non-numeric `title_id` case (classified `not_owned` before the
  call). Add a `libraries_response.json` fixture (paginated
  `/v2/libraries`) for the slug directory. Extend `search_response.json` items
  with `edition` strings exercising abridged/unabridged/absent (D4) and exact-vs-
  fuzzy titles (D6). Keep the existing 404 fixture.
- **availability:** `not_owned` mapping (single + batch-with-only-not-owned);
  non-numeric `title_id` classified `not_owned` with **no Thunder call** (single
  **and** multi-library, HIGH-4, via the shared `classify_and_fetch`); the 4xx
  slug-probe branch (bad-title vs. bad-slug) using the client's `status` field and
  `get_library` probe (HIGH-3); `estimated_wait_days==0` when available, raw when
  not; multi-library grouping; per-library failure isolation; one-of + slug-list
  validation reuse.
- **search_titles:** `exact` filter (single + multi; `exact` without `query`
  error); slug normalization (`LCPL`→`lcpl`, trimmed, link host lowercase, dedupe
  across case); `is_abridged` honesty across the mixed-edition fixture (never a
  constant).
- **lookup_library_slug:** match, partial match, no-match empty list, cache hit
  (no second fetch within TTL), directory-fetch failure with and without a stale
  cache.
- **handler:** auto-detect — `AWS_LAMBDA_RUNTIME_API` present → Mangum path;
  absent → `_build_app()` ASGI usable by a test ASGI client; `lookup_library_slug`
  registered; three tools callable.
- **auth:** no-auth passes through; static Bearer accept/reject; OAuth JWT
  accept/reject; static+OAuth coexist (static key OR JWT); 401 carries
  `resource_metadata` only in OAuth mode.
- **oauth:** PRM doc shape (RFC 9728); AS metadata `code_challenge_methods_supported
  == ["S256"]`, grants exclude `client_credentials`, and
  `token_endpoint_auth_methods_supported == ["none"]`; `/register` returns
  **HTTP 201** with a public-client body (`client_id`, `client_id_issued_at`,
  `redirect_uris`, `grant_types`, `response_types`,
  `token_endpoint_auth_method: "none"`) and **no `client_secret`** (HIGH-8), the
  `client_id` embedding the redirect URI(s); `/register` with a missing/invalid
  `redirect_uri` → HTTP 400 `invalid_redirect_uri`; `/authorize` rejects
  missing/non-S256 PKCE, rejects an unregistered `redirect_uri` with HTTP 400
  (not a redirect), rejects a missing owner credential with HTTP 401, and
  redirects with a code for each client's registered redirect URI; `/token` code
  exchange verifies the PKCE verifier and issues a JWT + refresh token; refresh
  rotation issues a new token and `invalid_grant` on an expired/unknown refresh
  token; JWT validation on `/mcp` checks signature/exp/`aud == canonical <base>/mcp`
  (HIGH-7).
- **oauth (canonical resource / audience, HIGH-7):** the PRM `resource` equals
  `<base>/mcp`; a `resource` param equal to canonical (or with a single trailing
  slash) is accepted, a missing one defaults to canonical, and a non-canonical one
  → `invalid_target` on both `/authorize` and `/token`; issued access **and**
  refresh tokens carry `aud == <base>/mcp` regardless of the client-sent value; a
  token minted with a non-canonical `aud` is rejected at `/mcp` (401).
- **oauth (stateless, CRITICAL-2):** JWT signing/verify is stdlib-only (assert no
  PyJWT import); a token signed with a given `LIBBY_MCP_OAUTH_SIGNING_KEY`
  validates in a *second* freshly-constructed app/verifier using the same key
  (simulates a different Lambda instance), and fails under a different key; a
  `client_id` minted by one app validates in another with the same key;
  `_build_app()` raises/refuses when OAuth is enabled and the signing key is
  unset; the authorization code expires after its configured TTL (60s default).
- **oauth (owner-secret gate, CRITICAL-1):** `_build_app()` **refuses to start**
  when OAuth is enabled and the effective owner secret is unset or an empty string
  (AC-F1.8); `POST /authorize` with no/empty/incorrect owner secret → HTTP 401
  before any code is minted (AC-F1.6); dev auto-approve is **refused** unless all
  three conditions hold (AC-F1.9) — explicit test cases for: auto-approve flag set
  but `AWS_LAMBDA_RUNTIME_API` present → refused; flag set but
  `LIBBY_MCP_PUBLIC_BASE_URL` non-loopback → refused; **flag set but
  `LIBBY_MCP_PUBLIC_BASE_URL` unset/empty → `_build_app()` refuses to start,
  naming the base URL as required (HIGH-6, AC-F1.10)**; flag set + non-Lambda +
  explicit loopback base URL (`http://127.0.0.1:8000`) → auto-approve allowed
  (with warning); flag unset → owner secret required.
- **oauth (typed-artifact / token-swap, HIGH-5):** every signed artifact carries a
  `typ` claim (`access` / `refresh` / `code`) and an `iss` claim; each verifier
  rejects the wrong `typ` — a refresh token or an authorization code presented as
  a Bearer on `/mcp` is rejected (401), and an access token presented to `/token`
  as a refresh/code grant is rejected (`invalid_grant`); the JWT header `alg` is
  pinned to `HS256` and a token with `alg: none` or any other alg is rejected; all
  HMAC/secret comparisons use `hmac.compare_digest`.
- **oauth (TTLs, HIGH-6):** access-token, refresh-token, and code TTLs are read
  from env (`LIBBY_MCP_OAUTH_ACCESS_TTL_SECONDS` default 3600,
  `LIBBY_MCP_OAUTH_REFRESH_TTL_SECONDS` default 2592000,
  `LIBBY_MCP_OAUTH_CODE_TTL_SECONDS` default 60); tests assert each default and
  that overriding the env var changes the issued token's `exp`.
- **docker / local-integration:** reuse the existing containerized MCP protocol
  suite against the new root `Dockerfile`; assert MCP initialize with a key, 401
  without a key when set, and <30s readiness.
- **compliance:** a test / `scripts/check_compliance.py` assertion that no
  `lcpl`/`fairfax`/`fcpl` literal appears as a behavioral branch in `src/`
  (allowed only in descriptions/errors/tests), and that exactly one handler module
  exists.
- Keep the TDD red-phase convention (`pytest.importorskip`) for not-yet-created
  modules (`src/oauth/*`, `src/slug_directory.py`, `src/tools/lookup_library_slug.py`).
