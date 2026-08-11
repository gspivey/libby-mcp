# Libby MCP Server - Spec Review Summary

**Date:** 2026-08-07
**Status:** ✅ SHIPPABLE (after 5 revision rounds)
**Reviewer:** Claude opus-5 with ultrathink

## Revision History

| Round | Issues Found | Issues Fixed |
|-------|-------------|--------------|
| Initial | 2 CRITICAL, 6 HIGH | - |
| Round 1 | - | C1, C2, H1-H6 |
| Round 2 | 2 HIGH, 5 MEDIUM | H1-2, M1-4 |
| Round 3 | 3 HIGH | H3-5 |
| Round 4 | 9 HIGH | H1-9 |
| Round 5 | 3 HIGH | H1-3 |
| Final | 0 CRITICAL, 0 HIGH | - |

## CRITICAL Issues (All Fixed)

### C1 — get_availability used wrong endpoint
- **Problem:** Spec routed to `/v2/media/bulk` which returns no availability data
- **Fix:** Changed to `/v2/libraries/{slug}/media/availability` which returns copies_owned, copies_available, holds_count, estimated_wait_days

### C2 — Lambda timeout equals Thunder timeout
- **Problem:** Both were 30s, so graceful error path was unreachable
- **Fix:** Thunder client 10s, Lambda 30s

## HIGH Issues (All Fixed)

Key fixes across 5 rounds:
- Pagination metadata reflects pre-filter state (documented behavior)
- Subject IDs are library-scoped (facets added to response)
- Title ID types standardized to string (coerce from integer)
- MCP result envelope specified (text content blocks)
- HTTP codes for notifications (202 Accepted)
- Error envelope standardized to isError:true for tool failures
- Duration extraction clarified (first non-empty across formats)
- hms_to_hours guards added
- bisac sent to server AND filtered client-side
- API key auto-generated via GenerateSecretString
- Invalid slug detection with filter qualifier
- Template-based scaffolding (Task 0 added)

## Remaining MEDIUM (Non-blocking)

1. `isAvailable` field missing from get_availability (has copies_available but not the boolean)
2. `total_pages` has no fallback for single-page results
3. NFR-1.2 (500ms warm) unreachable with 10s upstream calls
4. title_ids has no max-length or chunking

## Spec Files

- `requirements.md` (14KB)
- `design.md` (28KB)
- `tasks.md` (28KB)
- `context/` (reference files)

## MCP Tools (Final)

1. **search_titles(library_slug, ...)** — catalog search with filters, returns titles + facets
2. **get_availability(library_slug, title_ids[])** — raw availability data only
3. **get_deep_link(title_id, library_slug)** — HTTPS deep link to OverDrive

## Next Steps

1. Review spec files (optional - Claude says shippable)
2. Clone agent-router-template repo
3. Implement per tasks.md
4. Cut PR with spec + ROADMAP entry
