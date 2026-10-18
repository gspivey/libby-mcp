# Libby MCP Server

An MCP server exposing OverDrive/Libby library catalog search to Claude and Kiro.
Two fact-finding tools that return raw data from the Thunder API — no
recommendations or advisory logic. The LLM caller does the reasoning.

## Tools

| Tool | Description |
|------|-------------|
| `search_titles` | Search one or more libraries' OverDrive catalogs with filters (subjects, format, availability, creator, BISAC codes, duration, etc.). Each result includes a direct OverDrive `link`, folded availability signals (`holds_per_copy`, `estimated_wait_days`), content enrichment (`series_name`, `series_position`, `narrator`, `publisher`, `edition`, `is_abridged`), and copy/hold counts. Pass `exact=true` with a `query` for exact-title matching (no fuzzy fallback). Supports multi-library fan-out via `library_slugs`. |
| `get_availability` | Get raw copy/hold counts for specific titles at one or more libraries. A title the library does not own (or a malformed title ID) is returned with `status: not_owned` rather than failing the request. Supports multi-library fan-out via `library_slugs` (results grouped per library). |
| `lookup_library_slug` | Look up OverDrive library slugs by library name or partial name (case-insensitive). Use when you know a library's name (e.g. `Fairfax`) but need its slug (e.g. `fairfax`) for the other tools. Returns raw directory rows (slug + name); the directory is cached in-process with a 7-day TTL. |

All tools accept a `library_slug` parameter (e.g., `lcpl`, `fairfax`, `nypl`) — any
OverDrive library works.

## Setup

### Prerequisites

- Python 3.11+ (runtime 3.12 — the Docker image and the deployed Lambda both run on 3.12)
- Docker (for the standalone container, or for CDK Lambda bundling)
- Node.js 20+ (for CDK deployment)
- AWS CLI configured (only for the AWS deployment path)

### Local development

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
make test

# Full pre-push check (lint + typecheck + test + spec validation)
make preflight
```

### Run with Docker (standalone, no AWS)

The server runs as a standalone container — `git clone && docker compose up`, with
no AWS account, credentials, or Secrets Manager access. The same `src.handler:_build_app`
ASGI factory the Lambda uses is served by uvicorn (the handler auto-detects the runtime;
no code fork), so a connector config is portable between Docker and Lambda.

```bash
# Build and start the server. Binds to 127.0.0.1:8000 by default (loopback only).
docker compose up --build

# The MCP endpoint is now at http://127.0.0.1:8000/mcp
```

By default (no `LIBBY_MCP_API_KEY` set) the container serves `/mcp` **unauthenticated** —
intended for local-dev / firewalled use only. To require a static Bearer token, export the
key before starting:

```bash
export LIBBY_MCP_API_KEY="your-secret-key"
docker compose up --build
```

Verify the server is up (a valid MCP `initialize`, when a key is set):

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $LIBBY_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

A bare `GET /mcp` returns HTTP 405 (the endpoint is POST-only); a `POST /mcp` with no
`Authorization` header, when a key is set, returns HTTP 401.

> The default compose binding is `127.0.0.1:8000:8000` (loopback only). If you publish the
> port to a non-loopback interface (`8000:8000`), you MUST configure an auth mechanism
> (`LIBBY_MCP_API_KEY` or OAuth) first — never expose the server unauthenticated.

#### Connector configuration (static header)

Claude Desktop (`~/.claude/mcp.json`) or Cursor (`mcp.json`), pointing at the local
container:

```json
{
  "mcpServers": {
    "libby": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer your-secret-key"
      }
    }
  }
}
```

If you run with no `LIBBY_MCP_API_KEY`, omit the `headers` block.

### Deploy to AWS

The infrastructure is defined as an AWS CDK stack (TypeScript) in `infra/`.

```bash
# Deploy
./scripts/deploy.sh

# Get the API key (after deploy)
./scripts/get-api-key.sh

# Destroy (removes all resources)
./scripts/destroy.sh
```

Or manually:

```bash
cd infra
npm install
npx cdk deploy --require-approval never --outputs-file ../cdk-outputs.json
```

### What gets deployed

- **Lambda** — Python 3.12, ARM64, 256MB memory, 30s timeout
- **API Gateway HTTP API** — `POST /mcp` endpoint with throttling (10 req/s burst, 5 req/s sustained)
- **Secrets Manager** — auto-generated API key for MCP client authentication
- **CloudWatch Logs** — 14-day retention

### MCP client configuration

After deployment, configure your MCP client (e.g., `~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "libby": {
      "url": "<ApiEndpoint from cdk-outputs.json>",
      "headers": {
        "Authorization": "Bearer <API key from get-api-key.sh>"
      }
    }
  }
}
```

## Testing

```bash
make test          # Unit tests with coverage (80% threshold)
make lint          # ruff check + format
make typecheck     # mypy strict
make ci            # lint + typecheck + test (mirrors CI)
make preflight     # validate + compliance + ci (full pre-push)
make integration   # Live Thunder API tests (requires RUN_INTEGRATION=1)
make integration-test  # Containerized MCP protocol suite (local harness)
```

Integration tests hit the real Thunder API and are gated by `RUN_INTEGRATION=1`.
They run on a daily schedule in CI, not on every PR.

### Local integration harness (MCP protocol over HTTP)

`make integration-test` exercises the full MCP protocol surface — the ASGI app,
`AuthMiddleware`, FastMCP tool registration, and JSON-RPC request/response handling —
against a running server, end to end with the live (public, read-only) Thunder API.
It needs **no AWS, no Lambda, and no secrets**: a Docker image
(`Dockerfile.integration`) serves the production ASGI app factory
(`src.handler:_build_app`) under uvicorn, and the suite
(`tests/integration/test_mcp_protocol.py`) drives it over HTTP with the non-secret
test token `ci-test-key`.

```bash
# One command: build the image, start the container, run the suite, tear down.
make integration-test

# Or run the pieces by hand:
docker compose -f docker-compose.integration.yml up -d --build
RUN_LOCAL_INTEGRATION=1 MCP_ENDPOINT=http://localhost:8000/mcp MCP_API_KEY=ci-test-key \
  pytest tests/integration/test_mcp_protocol.py -v -m local_integration
docker compose -f docker-compose.integration.yml down -v
```

The suite is marked `-m local_integration` and additionally guarded by a module-level
`skipif` on `RUN_LOCAL_INTEGRATION=1`, so it never runs under the unit (`make test`) or
live-Thunder (`make integration`) commands. The endpoint and token are overridable via
`MCP_ENDPOINT` / `MCP_API_KEY` so the same suite can target a bare `uvicorn` process or a
remote server. In CI the `local-integration` job runs this suite on every PR.

### Branch protection (operator step)

The `local-integration` job is a required status check on `development`. After this
feature merges, an operator must add the required status check named
**`Local Integration Tests`** (the `name:` of the `local-integration` job) to the
`development` branch protection rule, alongside the existing required checks
(`lint`, `typecheck`, `test`). Applying the GitHub setting is a manual operator step;
the workflow only defines the job.

## Project structure

```
src/                    Python source (shared by Lambda and Docker)
  handler.py            Single entry point: Mangum on Lambda, uvicorn ASGI on Docker (auto-detected)
  auth.py               Bearer token validation (static-Bearer, plus Docker no-auth)
  thunder_client.py     HTTP client for Thunder API
  slug_directory.py     In-process TTL cache of the Thunder library directory
  fallback_slugs.json   Bundled offline library-slug snapshot (directory fallback)
  tools/                MCP tool handlers
    search_titles.py    Catalog search with filtering, multi-library fan-out
    availability.py     Batch availability lookup
    lookup_library_slug.py  Library-name -> slug lookup over the cached directory

Dockerfile              Standalone image (multi-stage, non-root, python:3.12-slim)
docker-compose.yml      `docker compose up` — standalone server on 127.0.0.1:8000

infra/                  AWS CDK stack (TypeScript)
  bin/libby-mcp.ts      CDK app entry point
  lib/libby-mcp-stack.ts Stack definition

tests/                  Test suite
  integration/          Live API tests (gated)
scripts/                Deploy, destroy, validation
```

## Architecture

```
                         AWS_LAMBDA_RUNTIME_API set?
                 ┌─────────────────┴──────────────────┐
         yes (Lambda)                          no (Docker / local)
   API Gateway → Mangum(_build_app())    uvicorn src.handler:_build_app --factory
                 └───────────────┬──────────────────┘
                                 ▼
                    _build_app() → AuthMiddleware(/mcp)
                                 ▼
                         Thunder API (public, no auth)
```

One codebase serves both runtimes — `handler.py` auto-detects Lambda vs. Docker via the
`AWS_LAMBDA_RUNTIME_API` environment variable and serves the identical ASGI app at `/mcp`.

## License

MIT
