# Libby MCP Server

An MCP server exposing OverDrive/Libby library catalog search to Claude and Kiro.
Three fact-finding tools that return raw data from the Thunder API — no
recommendations or advisory logic. The LLM caller does the reasoning.

## Tools

| Tool | Description |
|------|-------------|
| `search_titles` | Search a library's OverDrive catalog with filters (subjects, format, availability, creator, BISAC codes, duration, etc.) |
| `get_availability` | Get raw copy/hold counts for specific titles at a library |
| `get_deep_link` | Generate a direct OverDrive link for a specific title |

All tools accept a `library_slug` parameter (e.g., `lcpl`, `fairfaxcounty`, `nypl`) — any
OverDrive library works.

## Setup

### Prerequisites

- Python 3.10+
- Node.js 20+ (for CDK deployment)
- AWS CLI configured (for deployment)
- Docker (for CDK Lambda bundling)

### Local development

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
make test

# Full pre-push check (lint + typecheck + test + spec validation)
make preflight
```

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
```

Integration tests hit the real Thunder API and are gated by `RUN_INTEGRATION=1`.
They run on a daily schedule in CI, not on every PR.

## Project structure

```
src/                    Python source (Lambda function code)
  handler.py            Lambda entry point
  mcp_server.py         MCP JSON-RPC 2.0 protocol layer
  auth.py               Bearer token validation
  thunder_client.py     HTTP client for Thunder API
  tools/                MCP tool handlers
    search_titles.py    Catalog search with filtering
    availability.py     Batch availability lookup
    deep_link.py        URL generation

infra/                  AWS CDK stack (TypeScript)
  bin/libby-mcp.ts      CDK app entry point
  lib/libby-mcp-stack.ts Stack definition

tests/                  Test suite
  integration/          Live API tests (gated)
scripts/                Deploy, destroy, validation
```

## Architecture

```
MCP Client → API Gateway (POST /mcp) → Lambda (Python 3.12) → Thunder API (public)
                                              ↓
                                       Secrets Manager (API key at deploy time)
```

The Thunder API requires no authentication — it is a public endpoint. The MCP API key
protects access to this server, not to Thunder.

## License

MIT
