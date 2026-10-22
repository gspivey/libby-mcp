# Libby MCP Server

An MCP server exposing OverDrive/Libby library catalog search to Claude and Kiro.
Three fact-finding tools that return raw data from the Thunder API — no
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

## Authentication

The server has three authentication modes for the `/mcp` endpoint, selected by
environment variables. The same `AuthMiddleware` enforces them identically on
Docker and Lambda (no deployment-specific fork).

| `LIBBY_MCP_API_KEY` | `LIBBY_MCP_OAUTH_ENABLED` | Mode | `/mcp` behavior |
|---|---|---|---|
| unset | not `true` | **No-auth** | **Docker:** served unauthenticated (local-dev / firewalled). **Lambda:** fails closed — HTTP 500 `authentication_not_configured` (a public Lambda must have an auth mechanism). |
| set | not `true` | **Static Bearer** | `Authorization: Bearer <key>` required; validated in constant time. |
| unset | `true` | **OAuth 2.1** | a valid JWT access token is required; the OAuth endpoints (below) are activated. |
| set | `true` | **Static Bearer + OAuth** | a Bearer token is accepted if it equals the static key **or** validates as a JWT access token. |

No-auth is Docker-only; on Lambda the server must have at least one auth mechanism
configured.

### OAuth 2.1 (connect from Claude.ai, ChatGPT, Cursor)

Set `LIBBY_MCP_OAUTH_ENABLED=true` to turn on an embedded, **stateless** OAuth 2.1
authorization server (no third-party account, no server-side token store). Every
artifact — client IDs, authorization codes, access and refresh tokens — is a
self-contained HS256 JWT signed with `LIBBY_MCP_OAUTH_SIGNING_KEY`, so any Lambda
instance validates artifacts minted by any other (JWT uses only the Python
standard library — no new dependency).

| Variable | Required when OAuth on | Purpose |
|---|---|---|
| `LIBBY_MCP_OAUTH_ENABLED` | — | `true` turns OAuth on. |
| `LIBBY_MCP_OAUTH_SIGNING_KEY` | **yes** | Secret string of **≥ 32 bytes** that signs every artifact. The server **refuses to start** without it (no random per-instance fallback). |
| `LIBBY_MCP_OAUTH_OWNER_SECRET` | yes (or `LIBBY_MCP_API_KEY`) | The owner-consent secret entered on the `/authorize` form. Defaults to `LIBBY_MCP_API_KEY` when unset. The server **refuses to start** in OAuth mode if neither is set. |
| `LIBBY_MCP_PUBLIC_BASE_URL` | yes (non-loopback) | The public origin (e.g. `https://mcp.example.com`), used verbatim for every generated OAuth URL and the canonical resource `<base>/mcp`. Required for any non-loopback deployment; the Host header is never trusted to derive it. |
| `LIBBY_MCP_OAUTH_DEV_AUTO_APPROVE` | — | **Dev only.** Skips the interactive consent form. Honored **only** off-Lambda **and** when `LIBBY_MCP_PUBLIC_BASE_URL` is an explicit loopback origin (`http://127.0.0.1[:port]`, `http://localhost[:port]`, `http://[::1][:port]`). Never waives the owner-secret startup requirement, and must never be used behind a published (non-loopback) port. |
| `LIBBY_MCP_OAUTH_ACCESS_TTL_SECONDS` | — | Access-token lifetime (default 3600). |
| `LIBBY_MCP_OAUTH_REFRESH_TTL_SECONDS` | — | Refresh-token lifetime (default 2592000 = 30 days). |
| `LIBBY_MCP_OAUTH_CODE_TTL_SECONDS` | — | Authorization-code lifetime (default 60). |

**HTTPS / localhost rule.** All OAuth endpoints and generated URLs must use
`https` in any non-loopback deployment; `localhost` / `127.0.0.1` / `::1` are
exempt (allowed over `http`) for local development. Behind a TLS proxy (API
Gateway, nginx), run uvicorn with `--proxy-headers --forwarded-allow-ips='*'` and
set `LIBBY_MCP_PUBLIC_BASE_URL` to the public HTTPS URL — the shipped `Dockerfile`
already passes the proxy flags.

Endpoints activated in OAuth mode (all on the same host as `/mcp`):

| Method & path | Purpose |
|---|---|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 Protected Resource Metadata |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 Authorization Server Metadata |
| `POST /register` | RFC 7591 Dynamic Client Registration (public clients, PKCE) |
| `GET /authorize` | consent page (owner-secret gated) |
| `POST /authorize` | consent submit → authorization code |
| `POST /token` | code → access + refresh token; refresh rotation |

Run OAuth mode with Docker (loopback dev example):

```bash
export LIBBY_MCP_OAUTH_ENABLED=true
export LIBBY_MCP_OAUTH_SIGNING_KEY="$(head -c 48 /dev/urandom | base64)"
export LIBBY_MCP_OAUTH_OWNER_SECRET="choose-an-owner-secret"
export LIBBY_MCP_PUBLIC_BASE_URL="http://127.0.0.1:8000"
docker compose up --build
```

#### Per-client connector configuration

All three web clients connect via RFC 9728 discovery + RFC 7591 Dynamic Client
Registration with Authorization Code + PKCE (S256) — **no manual client
registration**. Point the client at your server's `/mcp` URL with OAuth enabled;
the client discovers and registers itself, then drives you through the consent
page where you enter the owner secret.

- **Claude.ai (web).** Add a custom connector with the URL
  `https://<your-host>/mcp`. Redirect URI
  `https://claude.ai/api/mcp/auth_callback` is accepted automatically.
- **ChatGPT.** Add an MCP connector with the URL `https://<your-host>/mcp`.
  Redirect URI `https://chatgpt.com/connector_platform_oauth_redirect` is
  accepted; the optional `resource` parameter ChatGPT sends is honored (it must
  equal the canonical `<base>/mcp` or is rejected with `invalid_target`; omitting
  it defaults to canonical).
- **Cursor.** Add the server URL to `mcp.json`; Cursor registers via DCR with the
  redirect URI `cursor://anysphere.cursor-mcp/oauth/callback`. Cursor users who
  prefer a static header can instead configure an `Authorization` header and skip
  OAuth entirely (static-Bearer mode).
- **Claude Desktop (local stdio).** Unaffected — it uses env-var credentials and
  no OAuth; no change is required.

`client_credentials` is never offered or accepted — only `authorization_code` and
`refresh_token` are advertised.

### Deploy to AWS

The infrastructure is defined as an AWS CDK stack (TypeScript) in `infra/`. It deploys into
whichever account and region your AWS credentials point at (set `AWS_PROFILE` to choose a
profile). You need Node.js, the AWS CLI with working credentials, and Docker running (CDK
uses it to package the Lambda).

```bash
# Once per account/region: create the CDK bootstrap resources
cd infra && npm install && npx cdk bootstrap && cd ..

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

The throttle is one limit for the whole API, shared by every caller, not a per-user limit.

### Estimated cost

At personal or small-group usage this is **about $0.40–$0.50 a month**, almost all of it the
Secrets Manager secret. Estimates use us-east-1 list prices as of October 2026; check the AWS
pricing pages for your region.

| Item | Price | 1,000 calls/month | 100,000 calls/month |
| --- | --- | --- | --- |
| Secrets Manager (1 secret) | $0.40/secret/month | $0.40 | $0.40 |
| API Gateway HTTP API | $1.00 per million requests | <$0.01 | $0.10 |
| Lambda (ARM, 256 MB, ~1 s/call) | always-free tier: 1M requests + 400,000 GB-s/month | $0 | $0 |
| CloudWatch Logs | $0.50/GB ingested | <$0.01 | ~$0.05 |
| **Total** | | **~$0.40** | **~$0.55** |

Turning on OAuth (`-c oauthEnabled=true`) adds a second secret (+$0.40/month). With no traffic
the cost is just the secret(s); `./scripts/destroy.sh` removes everything.

### Enabling OAuth on the Lambda deploy

OAuth is opt-in on the CDK stack. Deploy with the `oauthEnabled` context flag:

```bash
cd infra
npx cdk deploy -c oauthEnabled=true
# optional custom domain (otherwise the API Gateway endpoint origin is used):
#   npx cdk deploy -c oauthEnabled=true -c publicBaseUrl=https://mcp.example.com
```

When `oauthEnabled=true` the stack adds the six OAuth API Gateway routes (all
pointing at the same Lambda as `/mcp`), provisions an OAuth signing-key secret,
and sets `LIBBY_MCP_PUBLIC_BASE_URL` on the Lambda (defaulting to the API Gateway
endpoint origin, or the `publicBaseUrl` context override). With OAuth disabled the
stack is identical to v2.

**Operator verification (post-deploy).** After deploying the OAuth-enabled stack,
confirm the metadata route reaches the Lambda through API Gateway — it must return
**HTTP 200**, not a 404/403 from an unrouted path:

```bash
curl -i https://<your-api-id>.execute-api.<region>.amazonaws.com/.well-known/oauth-protected-resource
# → HTTP/1.1 200 OK, a JSON RFC 9728 Protected Resource Metadata document whose
#   "resource" is "<base>/mcp" and "authorization_servers" points at the AS.
```

This end-to-end check requires AWS credentials (SSO) and Docker (for Lambda
bundling); it is an **operator step**, not part of the unattended CI.

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

`make integration-test` tests the standalone image you actually run: it builds the root
`Dockerfile` through `docker-compose.yml` and drives the container end to end over HTTP,
with the live (public, read-only) Thunder API. It needs **no AWS, no Lambda, and no
secrets**. `scripts/container-test.sh` does the work:

| Step | Checks |
| --- | --- |
| Image shape | runs as non-root uid 10001; `/app` holds only `src/`; port bound to `127.0.0.1:8000` |
| Static-Bearer mode (`LIBBY_MCP_API_KEY=ci-test-key`) | healthy within 30s, then the full protocol suite, including 401 on a missing/wrong token |
| No-auth mode (`LIBBY_MCP_API_KEY` unset) | healthy within 30s, then the protocol suite with no token |

```bash
# One command: build, start, check, run the suite in both modes, tear down.
make integration-test

# Or point the suite at a server you started yourself:
LIBBY_MCP_API_KEY=ci-test-key docker compose up -d --build --wait
RUN_LOCAL_INTEGRATION=1 MCP_ENDPOINT=http://127.0.0.1:8000/mcp MCP_API_KEY=ci-test-key \
  pytest tests/integration/test_mcp_protocol.py -v -m local_integration
docker compose down -v
```

The suite (`tests/integration/test_mcp_protocol.py`) is marked `-m local_integration` and
additionally guarded by a module-level `skipif` on `RUN_LOCAL_INTEGRATION=1`, so it never
runs under the unit (`make test`) or live-Thunder (`make integration`) commands.
`MCP_ENDPOINT`, `MCP_API_KEY` and `MCP_AUTH_MODE` (`bearer` or `none`) let the same suite
target a bare `uvicorn` process or a remote server. In CI the `local-integration` job runs
`scripts/container-test.sh` on every push and PR.

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
  auth.py               Three-mode auth middleware (no-auth / static Bearer / OAuth JWT)
  thunder_client.py     HTTP client for Thunder API
  slug_directory.py     In-process TTL cache of the Thunder library directory
  fallback_slugs.json   Bundled offline library-slug snapshot (directory fallback)
  oauth/                Embedded stateless OAuth 2.1 authorization + resource server
    jwt.py              HS256 sign/verify (stdlib only; typed, alg-pinned artifacts)
    metadata.py         RFC 9728 PRM + RFC 8414 AS metadata; canonical resource
    registration.py     RFC 7591 public-client Dynamic Client Registration
    authorize.py        GET/POST /authorize — owner-consent gate, PKCE S256, signed code
    token.py            POST /token — code exchange, refresh rotation, RFC 6749 errors
    router.py           Path dispatch for the OAuth endpoints, mounted in front of /mcp
    config.py           OAuth env resolution + fail-fast startup checks
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
