# Design: Libby MCP Server

## Architecture Overview

The Libby MCP Server is a serverless Python application deployed on AWS Lambda behind API Gateway. It implements the MCP 2025-06-18 Streamable HTTP transport, accepting tool invocations from MCP clients (Claude, Kiro) and translating them into OverDrive Thunder API calls.

```
┌─────────────────┐     ┌───────────────┐     ┌──────────────┐     ┌─────────────────────────────────────────┐
│  MCP Client     │────▶│ API Gateway   │────▶│ Lambda       │────▶│ Thunder API (public, no auth)           │
│  (Claude/Kiro)  │◀────│ (HTTP API)    │◀────│ (Python)     │◀────│ thunder.api.overdrive.com               │
└─────────────────┘     └───────────────┘     └──────────────┘     └─────────────────────────────────────────┘
                                                     │
                                                     ▼
                                              ┌──────────────┐
                                              │ Secrets Mgr  │
                                              │ (MCP API Key)│
                                              └──────────────┘
```

## Key Design Principle: Server-Side vs Client-Side Filtering

From validated testing (documented in `libby_triage.py`):

> Server-side FILTER params are unreliable: `showOnlyAvailable` works, but `bisacCode` sometimes silently returns 0 results. So: send `bisacCode` to the server (it does work in many cases) AND ALSO filter client-side on `bisacCodes[]` (belt and suspenders). This guarantees correctness regardless of server-side behavior.

**Reliable server-side params** (use in Thunder API request):
- `subject`, `creator`, `seriesId`, `format`, `showOnlyAvailable`, `availableFirst`, `maturityLevel`, `perPage`, `page`, `sortBy`, `query`

**Belt-and-suspenders params** (send to Thunder API AND filter client-side):
- `bisacCode` — send server-side (works in many cases) AND filter on response `bisacCodes[]` as safety net

**Client-side only params** (apply on response):
- Duration filtering — scan all `formats[]` entries for first non-empty `duration` (HH:MM:SS), convert to hours

## Component Design

### 1. Entry Point / Lambda Handler (`handler.py`)

The Lambda function entry point that:
- Receives HTTP requests from API Gateway
- Validates the MCP API key from the `Authorization` header
- Routes to the MCP protocol handler
- Returns properly formatted HTTP responses
- Reuses `ThunderClient` instance across warm invocations (module-level)

### 2. MCP Protocol Layer (`mcp_server.py`)

Implements the MCP JSON-RPC protocol:
- Handles `initialize` handshake — returns InitializeResult: `{protocolVersion: "2025-06-18", capabilities: {tools: {}}, serverInfo: {name: "libby-mcp", version: "1.0.0"}}`
- Handles `ping` requests — returns empty result `{}` (standard MCP lifecycle method)
- Accepts `notifications/initialized` from client (no-op acknowledgment)
- Responds to `tools/list` with tool definitions
- Dispatches `tools/call` to the appropriate tool handler
- Formats responses per MCP 2025-06-18 spec (Streamable HTTP)
- Tool results use text content blocks: `{"content": [{"type": "text", "text": "<json>"}]}`

#### Streamable HTTP Transport (MVP Subset)

For the Lambda-based MVP, the implementation uses the **request-response subset** of Streamable HTTP:
- Single `POST /mcp` endpoint accepts JSON-RPC requests and returns JSON-RPC responses
- No persistent SSE connections (Lambda is stateless)
- No `Mcp-Session-Id` session management (each request is independent)
- The `initialize` → `notifications/initialized` → `tools/list` → `tools/call` flow works via sequential POST requests
- Full Streamable HTTP (server-initiated SSE, session persistence) is deferred to a future non-Lambda iteration if needed

### 3. Tool Handlers (`tools/`)

Each MCP tool is implemented as a standalone module:

#### `tools/search_titles.py`
- Builds Thunder API query params from MCP input (see mapping table below)
- Calls Thunder API `/v2/libraries/{slug}/media`
- **Invalid slug detection (best-effort heuristic):** If Thunder returns HTTP 200 but `response.items` is empty AND `response.totalItems` is 0 AND no narrowing filters (subjects, creator, query, bisac, series_id) were applied, return a tool error: "Library '{slug}' not found or unavailable". This is best-effort because Thunder returns 200 for bad slugs. If narrowing filters are present, an empty result is a valid search outcome.
- Applies client-side filters (bisac codes, duration/hours)
- Normalizes response items via `normalize_item()` (modeled on `libby_triage.py`)

#### `tools/get_availability.py`
- Accepts an array of title IDs (strings or integers, coerced to strings) and a library slug
- Fetches availability data via the library-scoped bulk availability endpoint: `GET /v2/libraries/{library_slug}/media/availability?titleIds={id1}&titleIds={id2}...`
- Thunder returns a response envelope: `{items: [AvailabilityItem, ...]}` — iterate `response.items` to extract per-title data
- Returns raw factual data per title: copies_owned, copies_available, holds_count, estimated_wait_days
- No recommendations, rankings, or strategic advice — just the facts
- Note: does NOT use `/v2/media/bulk` (that returns title metadata, not availability)

#### `tools/deep_link.py`
- Constructs `https://{library_slug}.overdrive.com/media/{title_id}`
- Validates parameters before generating

> **Design note:** The BRIEF's `get_deep_link` tool uses `https://{slug}.overdrive.com/media/{id}` format. This HTTPS URL works universally in any browser or context, auto-opens the Libby app if installed (via app link registration), and still functions as a web fallback if the app is not installed. A `libby://` scheme would require the app to be installed and fail silently otherwise. HTTPS is a strictly better UX for an MCP tool whose output may appear in various client contexts.

### 4. Thunder API Client (`thunder_client.py`)

A shared HTTP client for the OverDrive Thunder API:
- Base URL for search: `https://thunder.api.overdrive.com/v2/libraries/{slug}/media`
- Availability endpoint: `GET /v2/libraries/{slug}/media/availability?titleIds={id1}&titleIds={id2}...` (returns envelope `{items: [AvailabilityItem, ...]}` with dedicated availability fields per item)
- Bulk metadata endpoint: `GET /v2/media/bulk?titleIds={id1},{id2},...` (returns GlobalMediaResponse — title metadata only, NOT availability)
- No authentication required (public endpoint)
- User-Agent header: `libby-mcp/1.0` (matching existing pattern)
- Request timeout: 10 seconds (shorter than libby.py's 30s to fit within Lambda's 30s execution budget with margin for graceful error handling)
- Structured logging for request metrics
- **Note:** Rate limiting is handled exclusively by API Gateway throttling (10 req/s burst, 5 req/s sustained) — no in-process rate limiting in MVP. This avoids contradictions and is sufficient for single-user personal use.

### 5. Authentication (`auth.py`)

- Validates the **MCP server's own API key** (not a Thunder API key — Thunder is public)
- Reads expected key from `os.environ["LIBBY_MCP_API_KEY"]`
- Validates `Authorization: Bearer <key>` header on incoming MCP requests
- Constant-time comparison via `hmac.compare_digest`

### 6. Infrastructure (`infra/`)

AWS CDK (TypeScript) stack defining:
- Lambda function (Python 3.12, ARM64, 256MB memory, 30s timeout)
- API Gateway HTTP API with `POST /mcp` route
- Secrets Manager secret for MCP API key (auto-generated)
- IAM execution role with least-privilege permissions
- CloudWatch log group with 14-day retention

**Directory structure:**
```
infra/
├── bin/
│   └── libby-mcp.ts        # CDK app entry point
├── lib/
│   └── libby-mcp-stack.ts  # Stack definition
├── cdk.json                # CDK configuration
├── package.json            # CDK dependencies
└── tsconfig.json           # TypeScript config
```

**Deployment:**
```bash
cd infra && npm install && cdk deploy
```

**Key retrieval (post-deploy):**
```bash
aws secretsmanager get-secret-value --secret-id LibbyMcpApiKey --query SecretString --output text
```

## Thunder API Integration

### Primary Endpoint

```
GET https://thunder.api.overdrive.com/v2/libraries/{libraryKey}/media?{params}
```

No authentication. No token. Public endpoint. All libraries use the same API — swap the slug.

### Confirmed Query Parameters

| Parameter | Type | Example | Notes |
|-----------|------|---------|-------|
| `subject` | string (repeatable) | `subject=24&subject=80` | Subject IDs. Clean filter. Best signal-to-noise. |
| `creator` | string | `creator=Will Wight` | Author search. Clean, no keyword pollution. |
| `format` | string | `format=audiobook-overdrive` | Values: `audiobook-overdrive`, `ebook-overdrive` |
| `showOnlyAvailable` | boolean | `showOnlyAvailable=true` | **Confirmed reliable.** Server-side available-now filter. |
| `availableFirst` | boolean | `availableFirst=true` | Sort available to top without hiding waitlisted. |
| `maturityLevel` | string | `maturityLevel=generalcontent` | Values: `generalcontent`, `juvenile`, `youngadult`, `adultonly` |
| `bisacCode` | string | `bisacCode=FIC129000` | **SEMI-RELIABLE** — works in many cases but sometimes returns 0. Send server-side AND filter client-side (belt and suspenders). |
| `seriesId` | integer | `seriesId=307688` | Filter to a specific series. |
| `query` | string | `query=cultivation` | Full-text on title+subtitle+description. Pollutes — use sparingly. |
| `excludeSubject` | string | `excludeSubject=77` | Remove a genre from results. |
| `sortBy` | string | `sortBy=mostpopular-site` | Also: `newlyadded` |
| `perPage` | integer | `perPage=100` | Max 100, default 24. |
| `page` | integer | `page=2` | Pagination. |
| `title` | string | `title=dragon rider` | Title-field search (still collides — deep-link by id). |

### Sibling Endpoints

```
GET /v2/libraries/{slug}/media/availability?titleIds={id1}&titleIds={id2}  # Bulk availability (AvailabilityResponse) — USED by get_availability
GET /v2/libraries/{slug}/media/{titleId}/availability  # Single title availability
GET /v2/libraries/{slug}/subjects          # Full subject-ID list for the library
GET /v2/libraries/{slug}/series/{seriesId} # All books in a series
GET /v2/media/{titleId}                    # Single title details (global, no library context)
GET /v2/media/bulk?titleIds=...            # Multiple titles metadata (GlobalMediaResponse — NO availability fields)
GET /v2/media/search?libraryKey=...&query= # Multi-library search
GET /v2/autocomplete?query=...             # Autocomplete suggestions
```

### Response Structure

The `/media` endpoint returns:

```json
{
  "items": [ ... ],
  "totalItems": 843,
  "totalItemsText": "843 titles",
  "links": {
    "self": { "page": 1 },
    "last": { "page": 9 }
  },
  "facets": {
    "subjects": [ { "id": 24, "name": "Fantasy", "count": 150 } ],
    "availability": "..."
  }
}
```

**Important:** The `facets.subjects` array is library-scoped — subject IDs at LCPL may differ from NYPL. Always include facets in the search response so callers can discover valid subject IDs for the queried library.

### Response Item Fields (confirmed from `libby_triage.py normalize()`)

```json
{
  "id": "12345",
  "title": "Dungeon Crawler Carl",
  "subtitle": "...",
  "firstCreatorName": "Matt Dinniman",
  "reserveId": "GUID-string",
  "isAvailable": true,
  "availableCopies": 2,
  "ownedCopies": 3,
  "holdsCount": 0,
  "estimatedWaitDays": 0,
  "publisher": { "name": "Podium Audio" },
  "bisacCodes": ["FIC129000", "FIC009000"],
  "formats": [
    { "id": "audiobook-overdrive", "duration": "12:34:56" }
  ]
}
```

**Note on `id` type:** The Thunder API OpenAPI spec defines title IDs as `string` (see `/v2/media/bulk` and `/v2/libraries/{libraryKey}/media/availability` parameter schemas). While IDs may appear numeric, the MCP tools accept both string and integer inputs for `title_id`/`title_ids` and coerce to string internally. This avoids rejecting valid requests from LLM callers that send numeric IDs as integers.

**Validated availability rule:** `available_now = isAvailable AND availableCopies >= 1`

**Ignore:** `estimatedWaitDays` (nonzero even when copies are free — noise), `isOwned`, `isRecommendableToLibrary` (both always `true`).

### Parameter Mapping (MCP Tool → Thunder API)

| MCP Parameter | Thunder API Parameter | Transform |
|---------------|----------------------|-----------|
| `library_slug` | URL path: `/v2/libraries/{library_slug}/media` | Direct insertion |
| `subjects` | `subject` (repeated) | `[24, 80]` → `subject=24&subject=80` |
| `format` | `format` | `audiobook` → `audiobook-overdrive`, `ebook` → `ebook-overdrive` |
| `available` (true) | `showOnlyAvailable=true` | Direct |
| `available` (false/omitted) | `availableFirst=true` | Sort available to top |
| `creator` | `creator` | Direct |
| `bisac` | `bisacCode` **(server-side) + client-side filter** | Send `bisacCode` to Thunder AND filter response `bisacCodes[]` array (belt and suspenders) |
| `series_id` | `seriesId` | Direct |
| `under_hours` | **(client-side filter)** | Scan all `formats[]` entries for first non-empty `duration` (HH:MM:SS) → hours, filter |
| `per_page` | `perPage` | Direct (clamp to max 100) |
| `page` | `page` | Direct |
| `maturity_level` | `maturityLevel` | `general` → `generalcontent`, others direct |
| `sort_by` | `sortBy` | Direct |
| `query` | `query` | Direct |

### Format Mapping

| MCP `format` value | Thunder API `format` value |
|--------------------|---------------------------|
| `audiobook` | `audiobook-overdrive` |
| `ebook` | `ebook-overdrive` |

### Subject IDs (discovered from response `facets.subjects`)

**Important:** Subject IDs are library-scoped. The IDs below were discovered from LCPL and may not be valid at other libraries (e.g., NYPL). Always use the `facets.subjects` array in search responses to discover valid subject IDs for a specific library.

```
Fiction:     Fantasy=24, Fiction=26, Science Fiction=80, Literature=49,
             Thriller=100, Historical Fiction=115, Romance=77,
             Mythology=58, Young Adult Fiction=127, Suspense=86
Nonfiction:  Nonfiction=111, Business=8, Self-Improvement=81,
             Biography & Autobiography=7, Economics=144, Finance=27,
             Politics=67, Sociology=83
```

### BISAC Codes (from `libby_triage.py`)

```
FIC129000 = LitRPG                    FIC009000 = Fantasy/General
FIC009020 = Fantasy/Epic              FIC009030 = Fantasy/Historical
FIC009120 = Fantasy/Dragons           FIC010000 = Myth/Legend
FIC028000 = SciFi/General             FIC028010 = SciFi/Action
FIC031000 = Thriller                  FIC055000 = Dystopian
FIC061000 = Magical Realism           FIC019000 = Literary
FIC043000 = Coming of Age             FIC027030 = Romance/Fantasy
```

## Data Models

### Title Object (search_titles response item)

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
  "holds_ratio": 0.0,
  "hours": 12.6,
  "bisac_codes": ["FIC129000", "FIC009000"],
  "publisher": "Podium Audio",
  "link": "https://lcpl.overdrive.com/media/12345"
}
```

### Search Response (search_titles full response)

```json
{
  "titles": [ /* Title Objects */ ],
  "total_items": 843,
  "page": 1,
  "total_pages": 9,
  "filtered_count": 47,
  "facets": {
    "subjects": [ { "id": 24, "name": "Fantasy", "count": 150 } ]
  }
}
```

**Pagination note:** `total_items` and `total_pages` reflect the Thunder API server response BEFORE any client-side filtering (bisac, under_hours). `filtered_count` indicates how many titles remain after client-side filters are applied. This is because the server doesn't support these filters reliably, so true pagination with client-side filters would require fetching all pages.

### Availability Object (get_availability response item)

```json
{
  "title_id": "12345",
  "copies_owned": 3,
  "copies_available": 2,
  "holds_count": 0,
  "estimated_wait_days": 0
}
```

### Availability Response (get_availability full response)

```json
{
  "library_slug": "lcpl",
  "titles": [
    {
      "title_id": "12345",
      "copies_owned": 3,
      "copies_available": 2,
      "holds_count": 0,
      "estimated_wait_days": 0
    },
    {
      "title_id": "67890",
      "copies_owned": 5,
      "copies_available": 0,
      "holds_count": 3,
      "estimated_wait_days": 14
    }
  ]
}
```

### Deep Link Object (get_deep_link response)

```json
{
  "url": "https://lcpl.overdrive.com/media/12345",
  "title_id": "12345",
  "library_slug": "lcpl"
}
```

## MCP Tool Definitions

### Tool Result Format (H4)

All MCP tool results are returned using the standard MCP content block format:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"titles\": [...], \"total_items\": 843, ...}"
      }
    ]
  }
}
```

The `text` field contains the JSON-serialized tool output. Error responses use the `isError: true` flag:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"error\": \"Library 'xyz' not found or unavailable\"}"
      }
    ],
    "isError": true
  }
}
```

### search_titles

```json
{
  "name": "search_titles",
  "description": "Search a library's OverDrive catalog via the Thunder API. Returns titles with availability info, deep links, and copy/hold counts. Also returns facets (including valid subject IDs for the library). Use subjects or creator for precision; query for full-text (noisier). BISAC codes are sent server-side AND filtered client-side for reliability (belt and suspenders). Duration is filtered client-side only. Note: pagination metadata (total_items/total_pages) reflects server results before client-side filtering.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "library_slug": {
        "type": "string",
        "description": "Library identifier (e.g., lcpl, fairfaxcounty, nypl)"
      },
      "subjects": {
        "type": "array",
        "items": { "oneOf": [{"type": "string"}, {"type": "integer"}] },
        "description": "Subject IDs to filter by (e.g., [24, 80] or [\"24\", \"80\"] for Fantasy + Sci-Fi; coerced to strings internally). See response facets for available IDs."
      },
      "format": {
        "type": "string",
        "enum": ["audiobook", "ebook"],
        "description": "Media format filter"
      },
      "available": {
        "type": "boolean",
        "description": "Only show currently borrowable titles (reliable server-side filter)"
      },
      "creator": {
        "type": "string",
        "description": "Author or narrator name search (e.g., 'Will Wight')"
      },
      "bisac": {
        "type": "string",
        "description": "BISAC category code to filter by (e.g., FIC129000 for LitRPG). Sent server-side AND filtered client-side for reliability (belt and suspenders)."
      },
      "series_id": {
        "type": "integer",
        "description": "Filter to a specific series by ID"
      },
      "under_hours": {
        "type": "number",
        "description": "Maximum audiobook length in hours (client-side filter on duration). Items without a duration (e.g., ebooks) are INCLUDED — only items with a parseable duration exceeding the threshold are excluded."
      },
      "per_page": {
        "type": "integer",
        "default": 24,
        "maximum": 100,
        "description": "Results per page (max 100)"
      },
      "page": {
        "type": "integer",
        "default": 1,
        "description": "Page number for pagination"
      },
      "maturity_level": {
        "type": "string",
        "enum": ["general", "juvenile", "youngadult", "adultonly"],
        "description": "Content maturity filter (general strips kids/YA noise)"
      },
      "sort_by": {
        "type": "string",
        "description": "Sort order (e.g., mostpopular-site, newlyadded)"
      },
      "query": {
        "type": "string",
        "description": "Full-text search on title+subtitle+description. Noisier than subjects/creator — use sparingly."
      }
    },
    "required": ["library_slug"]
  }
}
```

### get_availability

```json
{
  "name": "get_availability",
  "description": "Get raw availability data for specific titles at a library via /v2/libraries/{slug}/media/availability. Returns copies owned, copies available, holds count, and estimated wait days for each title. No recommendations or rankings — just the facts for the LLM caller to reason about.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "library_slug": {
        "type": "string",
        "description": "Library identifier (e.g., lcpl, fairfaxcounty, nypl). Used in the availability endpoint path."
      },
      "title_ids": {
        "type": "array",
        "items": { "oneOf": [{"type": "string"}, {"type": "integer"}] },
        "description": "Array of OverDrive title IDs (strings or integers — coerced to string internally) to check availability for"
      }
    },
    "required": ["library_slug", "title_ids"]
  }
}
```

### get_deep_link

```json
{
  "name": "get_deep_link",
  "description": "Generate a direct OverDrive link for a specific title that opens the exact edition (collision-proof via numeric ID). Format: https://{slug}.overdrive.com/media/{id}",
  "inputSchema": {
    "type": "object",
    "properties": {
      "title_id": {
        "type": ["string", "integer"],
        "description": "OverDrive numeric title ID (string or integer — coerced to string internally)"
      },
      "library_slug": {
        "type": "string",
        "description": "Library slug for the link context"
      }
    },
    "required": ["title_id", "library_slug"]
  }
}
```

## Client-Side Filtering Logic

Ported from `libby_triage.py`:

### Duration Parsing (`hms_to_hours`)

```python
def hms_to_hours(duration_str: str) -> float | None:
    """Convert HH:MM:SS to decimal hours. Returns None if unparseable."""
    if not duration_str or not duration_str.strip():
        return None
    try:
        parts = [int(p) for p in duration_str.strip().split(":")]
        if not parts or len(parts) > 3:
            return None
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts[-3:]
        return round(h + m / 60 + s / 3600, 1)
    except (ValueError, TypeError):
        return None
```

### BISAC Filtering

```python
def passes_bisac(item_codes: list[str], keep: set[str]) -> bool:
    """True if item has ANY of the keep codes (or keep is empty)."""
    if not keep:
        return True
    return bool(set(item_codes) & keep)
```

### Duration Filtering (under_hours)

```python
def passes_duration(hours: float | None, max_hours: float | None) -> bool:
    """
    True if item should be INCLUDED in results.
    
    Items without a duration (hours is None, e.g. ebooks) are INCLUDED.
    Only items with a parseable duration that EXCEEDS max_hours are excluded.
    This matches libby.py behavior: `r["hours"] is None or r["hours"] <= a.under`
    """
    if max_hours is None:
        return True  # no filter active
    if hours is None:
        return True  # no duration = include (ebooks, etc.)
    return hours <= max_hours
```

### Availability Check

```python
def is_available(item: dict) -> bool:
    """Validated rule: isAvailable AND availableCopies >= 1."""
    return bool(item.get("isAvailable")) and (item.get("availableCopies", 0) or 0) >= 1
```

## Error Handling Strategy

Two distinct error paths:

1. **Protocol-level faults** — JSON-RPC `error` object (no `result` field). Used only for MCP protocol violations the client cannot recover from by changing tool arguments.
2. **Tool-execution failures** — JSON-RPC `result` with `isError: true`. Used for all tool-level failures (bad input, upstream errors) where the client might retry or adjust parameters.

| Error Scenario | HTTP Status | Response Type | Detail |
|---------------|-------------|---------------|--------|
| Missing/invalid MCP API key | 401 | N/A (HTTP level) | "Invalid or missing API key" |
| Unknown JSON-RPC method | 200 | JSON-RPC `error` (-32601) | "Method not found" |
| Malformed JSON-RPC request | 200 | JSON-RPC `error` (-32600) | "Invalid request" |
| Invalid library_slug (Thunder returns 200 with empty/mismatched response) | 200 | `result` with `isError: true` | "Library '{slug}' not found or unavailable" (best-effort heuristic — see AC-4.3; only triggers when no narrowing filters are applied) |
| Thunder API timeout (10s) | 200 | `result` with `isError: true` | "Library catalog temporarily unavailable — try again" |
| Thunder API 5xx | 200 | `result` with `isError: true` | "OverDrive service error, please retry" |
| Invalid/missing tool parameters | 200 | `result` with `isError: true` | Specific validation message (e.g., "{param} is required") |

**Rationale:** MCP clients (Claude, Kiro) handle `isError: true` results gracefully — they display the error message and can reason about retrying. JSON-RPC error codes (-32600, -32601) are reserved for protocol faults that indicate a broken client implementation, not a recoverable tool invocation problem.

## Project Structure

```
libby-mcp/
├── AGENTS.md                   # Table of contents for agent navigation
├── docs/
│   ├── design-docs/            # Spec files (requirements, design, tasks)
│   └── references/             # Thunder API docs, MCP spec excerpts
├── .github/
│   └── workflows/
│       └── ci.yml              # CI pipeline: lint, typecheck, test, integration
├── scripts/
│   ├── validate_spec.py        # Spec drift validator
│   ├── check_compliance.py     # Implementation compliance checker
│   └── test_mcp.sh             # Manual MCP endpoint testing script
├── src/
│   ├── __init__.py
│   ├── handler.py              # Lambda entry point
│   ├── mcp_server.py           # MCP protocol implementation
│   ├── auth.py                 # MCP API key validation
│   ├── thunder_client.py       # Thunder API HTTP client (public, no auth)
│   └── tools/
│       ├── __init__.py
│       ├── search_titles.py    # search_titles tool (server + client-side filtering)
│       ├── get_availability.py # get_availability tool (raw availability data retrieval)
│       └── deep_link.py        # get_deep_link tool (URL construction)
├── infra/
│   └── libby-mcp-stack.ts           # CDK stack
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # Shared fixtures, mock Thunder responses
│   ├── test_smoke.py           # Trivial smoke test (prevents pytest exit 5)
│   ├── fixtures/               # JSON response fixtures
│   │   ├── search_response.json
│   │   ├── availability_response.json
│   │   └── error_responses.json
│   ├── test_search_titles.py
│   ├── test_get_availability.py
│   ├── test_deep_link.py
│   ├── test_thunder_client.py
│   ├── test_auth.py
│   ├── test_handler.py
│   ├── test_mcp_server.py
│   └── integration/
│       └── test_live.py        # Real Thunder API (rate-limited, scheduled CI)
├── pyproject.toml              # Project metadata + ruff/mypy config
├── requirements.txt            # Pinned runtime dependencies
├── requirements-dev.txt        # Pinned dev dependencies (pytest, ruff, mypy, pre-commit)
├── .pre-commit-config.yaml     # Pre-commit hooks: ruff lint/format + mypy
├── cdk.json              # CDK configuration
├── Makefile                    # make test, make lint, make ci, make preflight
├── TEMPLATE_INVENTORY.md       # Delta between agent-router-template and implementation
└── README.md
```

## Technology Choices

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Runtime | Python 3.12 | Matches existing code (`libby.py`, `libby_triage.py`), fast Lambda cold start |
| HTTP client | `httpx` | Async-capable, lightweight, good timeout handling |
| MCP framework | Custom minimal | Streamable HTTP is new; avoid heavy framework dependencies for Lambda size |
| IaC | AWS CDK (TypeScript) | Type-safe constructs, IDE support, consistent with other projects |
| Testing | pytest | Standard Python testing, good Lambda testing patterns |
| Packaging | pip + requirements.txt | Simple dependency management for Lambda deployment |

## Harness Engineering

The project follows harness engineering principles: agents validate their own work, every task produces testable output, CI is the feedback loop, and the repo is the system of record.

**Red-Commit / Green-PR Policy:** Tests-first and implementation land in the SAME PR. Red (failing tests committed before implementation) is allowed at commit granularity, never at merge. Every PR must be green before merge.

**Extend or Create:** Tasks 1a and 1b say "extend or create" — the `agent-router-template` may already provide base CI/harness files. Check what exists before creating from scratch.

### Test-Driven Foundation

Tests are written BEFORE implementation code (TDD). Each module in `src/` has a corresponding test file that defines its acceptance criteria mechanically. The agent runs `pytest` locally to validate its own work before committing.

```
tests/
├── __init__.py
├── conftest.py                 # Shared fixtures: mock Thunder responses, test client
├── fixtures/                   # JSON response fixtures from Thunder API
│   ├── search_response.json
│   ├── availability_response.json
│   └── error_responses.json
├── test_thunder_client.py
├── test_auth.py
├── test_search_titles.py
├── test_get_availability.py
├── test_deep_link.py
├── test_handler.py
├── test_mcp_server.py
└── integration/
    └── test_live.py            # Real Thunder API calls (rate-limited, scheduled CI only)
```

### CI/CD Pipeline

GitHub Actions workflow at `.github/workflows/ci.yml`:

```yaml
# Triggered on every PR and push to main
jobs:
  lint:        # ruff check + ruff format --check
  typecheck:   # mypy src/
  test:        # pytest tests/ --ignore=tests/integration --cov=src --cov-fail-under=80
  integration: # pytest tests/integration -m integration (schedule-only with RUN_INTEGRATION=1)
  comment:     # PR comment with results (if: always(), separate job)
```

Key properties:
- **Every PR:** lint → typecheck → unit tests (fast, no external calls)
- **Coverage threshold:** 80% minimum enforced via `--cov-fail-under=80`
- **Scheduled (daily):** integration tests against real Thunder API (gated with `@pytest.mark.integration` + `RUN_INTEGRATION=1`)
- **Status checks required:** PR cannot merge until lint + typecheck + test pass
- **PR comment:** Separate job with `if: always()` and explicit `permissions: {contents: read, pull-requests: write}` — runs even when upstream jobs fail

### Agent Feedback Harness

Tools and scripts that enable the agent to validate spec compliance and iterate on failures:

| Tool | Purpose | Exit Codes |
|------|---------|------------|
| `scripts/validate_spec.py` | Cross-references spec files against context; detects drift | 0=PASS/WARN, 2=ERROR (never 1) |
| `scripts/check_compliance.py` | Validates implementation against acceptance criteria (scoped to existing modules) | 0=pass, 1=failures |
| Pre-commit hooks | Run ruff lint + format on staged files | — |
| `Makefile` | `make test`, `make lint`, `make ci`, `make preflight` — mirrors CI locally | — |

**Exit code contract:** `validate_spec.py` never exits 1 (reserved for `check_compliance.py`). This avoids collision when both run in sequence via `make preflight`.

Error messages are written for agent consumption: they include remediation hints (what to fix, where to look) rather than just failure descriptions.

### Repository Knowledge Structure

```
libby-mcp/
├── AGENTS.md                   # Table of contents: what's where, how to build/test/deploy
├── docs/
│   ├── design-docs/            # This spec lives here (requirements, design, tasks)
│   └── references/             # Thunder API docs, MCP spec excerpts
├── .github/
│   └── workflows/
│       └── ci.yml              # CI pipeline
├── scripts/
│   ├── validate_spec.py        # Spec drift validator
│   ├── check_compliance.py     # Implementation compliance checker
│   └── test_mcp.sh             # Manual MCP endpoint testing script
├── src/                        # Implementation
├── tests/                      # Test harness
└── ...
```

`AGENTS.md` is a concise routing document — it tells the agent where to find what it needs (not a wall of prose). The repo is self-contained: an agent can reason about the full domain from the repo alone.

### Linter-Enforced Architecture Constraints

Ruff configuration in `pyproject.toml`:
- Import sorting (isort-compatible)
- No wildcard imports
- Type annotations required on public functions (ANN201 — return type on public functions)
- Max line length 100
- No unused imports/variables

These constraints are enforced mechanically — violations fail CI, giving the agent immediate feedback.

## Security Considerations

- MCP API key stored in Secrets Manager (auto-generated at stack creation via `GenerateSecretString`), loaded at cold start via environment variable injection
- Thunder API requires NO authentication for catalog reads — it's a public API
- No secrets in source code or logs
- Lambda runs in VPC-less mode (public internet access for Thunder API)
- API Gateway throttling configured as the sole rate limit (10 req/s burst, 5 req/s sustained — sufficient for single-user MVP)
- Input validation on all parameters before Thunder API calls
- No user data persistence (stateless)
- User-Agent header identifies the client respectfully (`libby-mcp/1.0`)
