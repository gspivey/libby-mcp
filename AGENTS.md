# Agent Guide -- libby-mcp

An MCP server exposing OverDrive/Libby library catalog search to Claude/Kiro.
Three fact-finding tools (search_titles, get_availability, get_deep_link) that
return raw data from the Thunder API. No recommendations or advisory logic --
the LLM caller does reasoning.

## Quick Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run tests (unit only, with coverage)
make test

# Lint (ruff check + format check)
make lint

# Type check (mypy strict on src/)
make typecheck

# Full local CI mirror (lint + typecheck + test)
make ci

# Validate spec alignment
make validate

# Check implementation compliance
make compliance

# Full pre-push check (validate + compliance + ci)
make preflight

# Integration tests (requires RUN_INTEGRATION=1, hits real Thunder API)
make integration

# Auto-format
make format
```

## Directory Layout

```
src/                    # Python source (Lambda function code)
  __init__.py
  tools/                # MCP tool handlers (one module per tool)
    __init__.py
    search_titles.py    # Task 6
    availability.py     # Task 7
    deep_link.py        # Task 8
  thunder_client.py     # Task 3: HTTP client for Thunder API
  auth.py               # Task 4: Bearer token validation
  mcp_server.py         # Task 5: JSON-RPC protocol layer
  handler.py            # Task 9: Lambda entry point
tests/                  # Unit and fixture tests
  __init__.py
  conftest.py           # Shared pytest fixtures
  fixtures/             # Mock Thunder API responses
    search_response.json
    availability_response.json
    error_responses.json
  integration/          # Live API tests (gated by RUN_INTEGRATION=1)
    test_live.py        # Thunder API response shape validation
scripts/                # Validation, compliance, and deployment tooling
  validate_spec.py      # Spec alignment checker
  check_compliance.py   # Implementation compliance checker
  deploy.sh             # Deploy CDK stack
  destroy.sh            # Destroy CDK stack
  get-api-key.sh        # Retrieve API key from Secrets Manager
docs/
  design-docs/          # Spec copies (requirements.md, design.md, tasks.md)
  references/           # Domain reference material (Thunder API, MCP spec)
infra/                  # AWS CDK stack (Task 10, TypeScript)
.github/workflows/      # CI pipeline
```

## Spec and Design Documents

- `docs/design-docs/requirements.md` -- acceptance criteria for all tools
- `docs/design-docs/design.md` -- architecture, data models, API mappings
- `docs/design-docs/tasks.md` -- implementation sub-tasks with TDD workflow
- `.kiro/specs/libby-mcp/` -- Kiro spec source of truth (do not reference from scripts)

## Domain Reference

- `docs/references/thunder_api_reference.md` -- endpoints, params, response shapes
- `docs/references/mcp_spec_excerpt.md` -- MCP 2025-06-18 protocol details
- `docs/references/libby.py` -- working Thunder API client (reference implementation)
- `docs/references/libby_triage.py` -- hold analysis logic (normalization patterns)
- `docs/references/LIBBY_RECIPE.md` -- endpoint documentation and workflow
- `docs/references/thunder_openapi_v1.json` -- partial OpenAPI spec (121 paths)

## Validation Commands

### make validate

Checks spec alignment: directory structure matches design.md, tool test files
exist, spec files are accessible. Outputs structured results per check.

Exit codes: 0=PASS (or WARN), 2=ERROR (blocking).

### make compliance

Checks implementation compliance: modules importable, expected classes/functions
exist, tool definitions match schema. Skips modules not yet implemented (TDD
workflow support).

Exit codes: 0=PASS, 1=FAIL.

### make preflight

Runs validate + compliance + ci. This is the full pre-push check. Run before
every push.

## Workflow

1. Write tests (red phase -- tests may fail or skip on import)
2. Run `make test` -- confirm tests are collected and fail as expected
3. Implement the module
4. Run `make test` -- confirm tests pass
5. Run `make preflight` -- full validation
6. Commit and push

## Session Contract

See `prompts/agent-router.md` for the full RFC-2119 session mechanics:
branch from `development`, one ROADMAP item per PR, tick checkboxes before
merge, squash-merge to `development`.

## Infrastructure (CDK)

The AWS infrastructure is defined in `infra/` as a CDK TypeScript stack:

```bash
# Verify TypeScript compiles
cd infra && npx tsc --noEmit

# Synthesize CloudFormation (requires Docker for PythonFunction bundling)
cd infra && npx cdk synth

# Deploy (requires AWS credentials + Docker)
./scripts/deploy.sh

# Destroy all resources
./scripts/destroy.sh

# Retrieve MCP API key post-deploy
./scripts/get-api-key.sh
```

Stack resources: Lambda (Python 3.12, ARM64, 256MB, 30s), API Gateway HTTP API
(POST /mcp, 10 req/s burst / 5 req/s sustained), Secrets Manager (auto-generated
API key), CloudWatch Logs (14-day retention).

## Branch Protection (development)

Required status checks before merge:
- `lint` (ruff check + format)
- `typecheck` (mypy strict)
- `test` (pytest with 80% coverage threshold)

No direct pushes to `main`. PRs target `development`.
