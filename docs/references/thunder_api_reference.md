# Thunder API Reference

Quick reference for the OverDrive Thunder API endpoints used by libby-mcp.
Extracted from the OpenAPI spec and validated testing in `libby_triage.py`.

## Base URL

```
https://thunder.api.overdrive.com
```

No authentication required. Public endpoint. All libraries use the same API.

## Endpoints

### Search Library Catalog

```
GET /v2/libraries/{libraryKey}/media?{params}
```

Returns paginated catalog results for a library.

**Path parameters:**
- `libraryKey` (string, required) -- library slug (e.g., `lcpl`, `fairfaxcounty`, `nypl`)

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string | Full-text search on title+subtitle+description |
| `subject` | string (repeatable) | Subject ID filter (e.g., `subject=24&subject=80`) |
| `creator` | string | Author/narrator name |
| `format` | string | `audiobook-overdrive` or `ebook-overdrive` |
| `showOnlyAvailable` | boolean | Show only available titles (reliable) |
| `availableFirst` | boolean | Sort available to top |
| `maturityLevel` | string | `generalcontent`, `juvenile`, `youngadult`, `adultonly` |
| `bisacCode` | string | BISAC category code (semi-reliable, use belt-and-suspenders) |
| `seriesId` | integer | Filter to specific series |
| `sortBy` | string | `mostpopular-site`, `newlyadded` |
| `perPage` | integer | Results per page (max 100, default 24) |
| `page` | integer | Page number |
| `excludeSubject` | string | Remove a genre from results |

**Response shape:**
```json
{
  "items": [...],
  "totalItems": 843,
  "totalItemsText": "843 titles",
  "links": {"self": {"page": 1}, "last": {"page": 9}},
  "facets": {"subjects": [{"id": 24, "name": "Fantasy", "count": 150}]}
}
```

**Item fields:**
```json
{
  "id": "12345",
  "title": "Book Title",
  "subtitle": "...",
  "firstCreatorName": "Author Name",
  "isAvailable": true,
  "availableCopies": 2,
  "ownedCopies": 3,
  "holdsCount": 0,
  "estimatedWaitDays": 0,
  "publisher": {"name": "Publisher"},
  "bisacCodes": ["FIC129000"],
  "formats": [{"id": "audiobook-overdrive", "duration": "12:34:56"}]
}
```

### Bulk Availability

```
GET /v2/libraries/{libraryKey}/media/availability?titleIds={id1}&titleIds={id2}
```

Returns availability data for multiple titles at a specific library.

**Response shape:**
```json
{
  "items": [
    {
      "id": "12345",
      "availableCopies": 2,
      "ownedCopies": 3,
      "holdsCount": 0,
      "estimatedWaitDays": 0,
      "isAvailable": true
    }
  ]
}
```

### Single Title Details (global)

```
GET /v2/media/{titleId}
```

Returns metadata for a single title (no library context, no availability).

### Bulk Title Metadata (global)

```
GET /v2/media/bulk?titleIds={id1},{id2},...
```

Returns metadata for multiple titles. Does NOT include availability fields.
Use the library-scoped availability endpoint for copy/hold counts.

## Known Quirks

1. **Invalid slugs return 200 with empty results.** Thunder does not return 4xx
   for nonexistent libraries. Detection is best-effort: empty items + totalItems=0
   + no narrowing filters applied = likely bad slug.

2. **`bisacCode` filter is semi-reliable.** Sometimes silently returns 0 results.
   Always apply client-side filter on `bisacCodes[]` as a safety net.

3. **`estimatedWaitDays` is noisy.** Can be nonzero even when copies are free.
   The reliable availability rule is: `isAvailable == true AND availableCopies >= 1`.

4. **Subject IDs are library-scoped.** Fantasy=24 at LCPL may not be Fantasy=24
   at NYPL. Use `facets.subjects` in search responses to discover valid IDs.

5. **`duration` field location.** Found inside `formats[]` entries. Scan all
   format entries for the first non-empty duration string (format: `HH:MM:SS`).
