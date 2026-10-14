# Tasks: Libby MCP Server v2

## Workflow Policy

**Red-Commit / Green-PR Rule:** Tests-first and implementation land in the SAME
PR. Red (failing tests committed before implementation) is allowed at commit
granularity, never at merge. Every PR must be green before merge — CI enforces
this via required status checks (lint, typecheck, test with 80% coverage).

**Session Contract (see `prompts/agent-router.md`):** branch from `development`,
one ROADMAP item per PR, tick checkboxes before merge, squash-merge to
`development`. Run `make preflight` before every push.

**TDD red-phase convention:** new tests for behavior not yet implemented must not
break collection. Use `pytest.importorskip` for not-yet-existing modules; for
new behavior on existing modules, guard with skips or write the test to fail
cleanly (not error on import).

## Dependency Map

- **Group A** (Tasks A1–A4) and **Group B** (Tasks B1–B3) are **mostly
  independent**, but **PR-B depends on PR-A**: Task B1's `_execute_get_async`
  reuses the A2 404 message (and B1's per-library failure test asserts that exact
  message), and B1's per-library behavior relies on A4's `include_facets` opt-in.
  Merge PR-A before PR-B.
- **Group C** (Tasks C1–C3) depends on **Task B2** (the enriched `_normalize_item`
  and search fixtures). Do not start C until B2 has landed.
- **Task B3** (retire `get_deep_link`) is independent of B1/B2 but belongs to the
  Group B PR for coherence.

Each group is one PR unless noted. Suggested PR order (not parallel — B depends
on A, C depends on B):
1. PR-A: Tasks A1–A4
2. PR-B: Tasks B1–B3 (after PR-A merges)
3. PR-C: Tasks C1–C3 (after PR-B merges)

---

## Group A — Quick Fixes (PR-A)

### Task A1: Correct Library Slug Examples

**Requirements:** FR-A1 (AC-A1.1–A1.4)

**Steps:**
1. Replace every `fairfaxcounty` literal in `src/handler.py` tool/parameter
   descriptions with `fairfax`.
2. Grep `src/` for any remaining `fairfaxcounty`; remove all.
3. Add a test (e.g. `tests/test_handler.py`) asserting no `fairfaxcounty` literal
   is present in the registered tool descriptions, and that the example list is
   `lcpl, fairfax, nypl`.

**Acceptance:** AC-A1.1–A1.4. No `fairfaxcounty` anywhere in `src/`.

### Task A2: Human-Readable 404 Errors

**Requirements:** FR-A2 (AC-A2.1–A2.5)

**Steps (TDD):**
1. Add a 404 fixture to `tests/fixtures/error_responses.json`.
2. Write `tests/test_thunder_client.py` cases: HTTP 404 → `unknown library slug:
   <slug> — check the slug is correct (e.g. lcpl, fairfax, nypl)`; non-404 keeps
   `OverDrive service error (HTTP <status>)`; timeout/unexpected unchanged; empty
   slug does not crash.
3. Implement: thread `slug` into `_execute_get` from both `search()` and
   `get_availability()`; branch on `status == 404 and slug is not None`.
4. Run `make test`; green.

**Acceptance:** AC-A2.1–A2.5.

### Task A3: Echo library_slug

**Requirements:** FR-A3 (AC-A3.1–A3.4)

**Steps (TDD):**
1. Write tests: `search_titles.handle` single-library result includes
   `library_slug`; `get_availability` still returns `library_slug`.
2. Implement: add `library_slug` to the single-library return dict in
   `search_titles.handle`.
3. (Multi-library per-block `library_slug` is covered by Task B1.)

**Acceptance:** AC-A3.1–A3.3 (A3.4 verified in Task B1).

### Task A4: Facets Opt-In

**Requirements:** FR-A4 (AC-A4.1–A4.4)

**Steps (TDD):**
1. Write tests: default response has NO `facets` key; `include_facets=True`
   returns populated `facets`.
2. Implement: add `include_facets: bool = False` to the `search_titles` tool
   signature in `handler.py`; thread into `handle`; attach `facets` only when
   true.
3. Update the tool description to note facets are opt-in.

**Acceptance:** AC-A4.1–A4.4.

**PR-A exit:** `make preflight` green; checkboxes ticked; squash-merge to
`development`.

---

## Group B — Multi-Library + Hold Intelligence (PR-B)

### Task B1: Multi-Library Fan-Out Search

**Requirements:** FR-B1 (AC-B1.1–B1.12), FR-A3 (AC-A3.4), NFR-2.1, NFR-3.1, NFR-5.1

**Steps (TDD):**
1. Write tests:
   - one-of validation (both set → error; neither → error).
   - `library_slugs=['lcpl','fairfax']` returns a `libraries` list with a block
     per slug (AC-B1.6).
   - each block carries its own `library_slug` (AC-B1.4/A3.4).
   - per-library failure (one slug 404) yields an `error` block while the other
     returns titles (AC-B1.5).
   - single-entry `library_slugs` still returns grouped shape (AC-B1.8).
   - single `library_slug` keeps the flat shape (AC-B1.7).
   - `library_slugs` validation: empty list → `library_slugs must not be empty`;
     blank/whitespace entry → `library_slugs contains invalid entry: empty slug`;
     duplicates deduplicated (order preserved, later dropped); >10 entries →
     `library_slugs exceeds maximum of 10 libraries` (AC-B1.9–B1.12).
   - an **end-to-end test** that invokes the registered `search_titles` tool
     through the actual FastMCP app (under a running event loop, as Mangum does),
     not just a direct call to `handle_multi`, to prove the async tool does not
     break on a running loop (CRITICAL-1).
2. Implement `ThunderClient.search_async(slug, params, async_client)`; add
   `_execute_get_async` mirroring `_execute_get` (including the A2 404 message).
   Do **not** store an `AsyncClient` on the instance — see step 4.
3. Implement `_search_one(slug, params, client, async_client)` that
   catches/normalizes failures into a block with `library_slug` + `error`.
4. Implement the fan-out so it runs under the existing event loop (FastMCP +
   Mangum): make the `search_titles` tool `async def` and `await` the fan-out
   directly — do NOT call `asyncio.run()` (it raises inside a running loop).
   Create the `httpx.AsyncClient` per fan-out with
   `async with httpx.AsyncClient(headers=..., timeout=TIMEOUT_SECONDS) as async_client:`
   inside `_fan_out` (not on the `ThunderClient` instance), so its pool is bound
   to the current loop and survives warm invocations. The `async def
   search_titles` wrapper calls `await handle_multi(...)` for the multi-slug path
   and calls `handle(...)` synchronously for the single-slug path. Do **not** make
   `handle()` async — existing v1 tests call it directly as a sync function
   (`handle(args, client) -> dict`). `handle_multi()` is the async multi-slug
   handler. Route from the wrapper based on which of `library_slug` /
   `library_slugs` is set.
5. Make `library_slug` `Optional[str] = None` in the `search_titles` tool
   signature in `handler.py` and add `library_slugs: Optional[list[str]] = None`.
   Build the `args` dict passed to the handlers from non-`None` values only.
   Validation (AC-B1.2, AC-B1.9–B1.12) runs in the wrapper **before** dispatch:
   one-of (both set → `provide either library_slug or library_slugs, not both`;
   neither set → `library_slug or library_slugs is required`, replacing the old
   "library_slug is required" error) followed by the `library_slugs` validation
   from step 1. `handle()` and `handle_multi()` both assume the `args` dict has
   already been validated. The `ToolError` raise happens in the wrapper after
   dispatch (when a handler returns a dict containing `error`).
6. Run `make test`; green.

**Acceptance:** AC-B1.1–B1.12, AC-A3.4.

### Task B2: Fold Availability Into Search

**Requirements:** FR-B2 (AC-B2.1–B2.5), NFR-2.3

**Steps (TDD):**
1. Extend `tests/fixtures/search_response.json` items with `holdsCount`,
   `ownedCopies`, `estimatedWaitDays`.
2. Write tests: `holds_per_copy == round(holdsCount/ownedCopies, 1)` when owned
   > 0; owned 0 (or absent) and holds 0 → `0.0` (no crash); owned 0 (or absent)
   and holds > 0 → `None`; `estimated_wait_days` from `estimatedWaitDays`,
   defaulting to `None` (not `0`) when the field is absent; existing fields
   intact.
3. Implement `_holds_per_copy` helper (returns `float | None`) and add both
   fields in `_normalize_item` (derive from the search item — no extra Thunder
   call; `estimated_wait_days` is `None` when `estimatedWaitDays` is absent).
4. Run `make test`; green.

**Acceptance:** AC-B2.1–B2.5. **(Group C depends on this task.)**

### Task B3: Retire get_deep_link

**Requirements:** FR-B3 (AC-B3.1–B3.5)

**Steps (TDD):**
1. Write a handler test asserting `get_deep_link` is NOT in the registered tool
   set, and the two remaining tools are still callable.
2. Write/keep a `_normalize_item` test asserting `link` is well-formed
   (`https://{slug}.overdrive.com/media/{id}`) for every returned item, and that
   items with a missing/`None` id are excluded (dropped with a logged warning)
   rather than emitted with a `.../media/` or `.../media/None` link.
3. Remove the `get_deep_link` `@mcp.tool` registration and the `deep_link` import
   from `handler.py`.
4. Delete `src/tools/deep_link.py` and `tests/test_deep_link.py`.
5. Run `make test`; green (no dangling imports, no collection errors).

**Acceptance:** AC-B3.1–B3.5, NFR-4.3.

**PR-B exit:** `make preflight` green; checkboxes ticked; squash-merge to
`development`.

---

## Group C — Content Enrichment (PR-C, after PR-B)

> Depends on Task B2 (enriched `_normalize_item` + fixtures).

> **Field paths are assumptions — verify first.** The Group C field paths
> (`series.name`, `series.readingOrder`, `narrators[].name`, `publisher.name`,
> `edition`, `isAbridged`) are based on OverDrive documentation and have not been
> confirmed against a live `/media` item. Before building fixtures for each task
> below, verify the paths against `docs/references/thunder_api_reference.md` or a
> captured live media item; if any path differs, update `_normalize_item` and the
> fixtures to match the real response.

### Task C1: Series Metadata

**Requirements:** FR-C1 (AC-C1.1–C1.4)

**Steps (TDD):**
1. Verify the series field paths against `docs/references/thunder_api_reference.md`
   or a captured live media item; if they differ, update `_normalize_item` and
   fixtures to match. Then add `series` objects (`name`, `readingOrder`) to search
   fixtures; include an item with no series and an item whose `series` is a bare
   string (to exercise the isinstance guard).
2. Write tests: `series_name`/`series_position` populated; no-series item →
   both `None`; non-numeric `readingOrder` → `None`.
3. Implement `_to_float` helper; add `series_name`, `series_position` to
   `_normalize_item`.

**Acceptance:** AC-C1.1–C1.4.

### Task C2: Narrator, Publisher, Edition

**Requirements:** FR-C2 (AC-C2.1–C2.4)

**Steps (TDD):**
1. Verify the `narrators`, `publisher`, and `edition` field paths against
   `docs/references/thunder_api_reference.md` or a captured live media item; if
   they differ, update `_normalize_item` and fixtures to match. Then add
   `narrators`, `publisher.name`, `edition` to fixtures; include an item missing
   each, plus malformed-type cases (publisher as a bare string, narrators as a
   list of strings) to exercise the isinstance guards.
2. Write tests: `narrator` list (empty when absent); `publisher` string or
   `None`; `edition` string or `None`; malformed fields do not crash.
3. Implement the three fields in `_normalize_item`.

**Acceptance:** AC-C2.1–C2.4.

### Task C3: Duration + Abridged

**Requirements:** FR-C3 (AC-C3.1–C3.4)

**Steps (TDD):**
1. Verify the `isAbridged` field path (on the format entry) against
   `docs/references/thunder_api_reference.md` or a captured live media item; if it
   differs, update `_normalize_item` and fixtures to match. Then add `isAbridged`
   to format entries in fixtures; include a format without it.
2. Write tests: `hours` key always present (float or `None`); `is_abridged`
   reflects `isAbridged`; absent `isAbridged` → `None` (not `False`); no crash.
3. Implement `is_abridged` in `_normalize_item` reading from the primary format
   entry.

**Acceptance:** AC-C3.1–C3.4.

**PR-C exit:** `make preflight` green; checkboxes ticked; squash-merge to
`development`.

---

## Cross-Cutting Verification

**Requirements:** NFR-1, NFR-4

Across all PRs:
- All existing v1 tests continue to pass (NFR-1, NFR-4.1).
- New tests cover multi-library fan-out (incl. per-library failure), enriched
  fields, facets opt-in/out, `library_slug` echo, and the 404 message (NFR-4.2).
- `tests/test_deep_link.py` is removed; a test asserts the tool is unregistered
  (NFR-4.3).
- `make preflight` (validate + compliance + ci) is green, including the 80%
  coverage threshold (NFR-4.4).
- Update `README.md` tool list (two tools) and any `AGENTS.md` references to
  `get_deep_link` if present.

## Requirements Traceability

| Task | Requirements |
|------|--------------|
| A1 | FR-A1 (AC-A1.1–A1.4) |
| A2 | FR-A2 (AC-A2.1–A2.5), NFR-3.3 |
| A3 | FR-A3 (AC-A3.1–A3.3) |
| A4 | FR-A4 (AC-A4.1–A4.4) |
| B1 | FR-B1 (AC-B1.1–B1.12), AC-A3.4, NFR-2.1, NFR-3.1, NFR-5.1 |
| B2 | FR-B2 (AC-B2.1–B2.5), NFR-2.3 |
| B3 | FR-B3 (AC-B3.1–B3.5), NFR-4.3 |
| C1 | FR-C1 (AC-C1.1–C1.4) |
| C2 | FR-C2 (AC-C2.1–C2.4) |
| C3 | FR-C3 (AC-C3.1–C3.4) |
| Cross-cutting | NFR-1, NFR-4 |
