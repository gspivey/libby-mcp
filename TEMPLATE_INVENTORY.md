# Template Inventory

Documents what `agent-router-template` provides versus what was added or
modified for libby-mcp. Generated during ROADMAP item 1 (scaffolding).

## Template-Provided (kept or extended)

| File/Dir | Role | Status |
|----------|------|--------|
| `.github/PULL_REQUEST_TEMPLATE.md` | PR template | Kept as-is |
| `.gitignore` | Ignore patterns | Kept (already covers Python, Node, Rust, OS) |
| `sample-workflows/ci.yml` | Auto-detect CI (reference) | Kept as reference; custom CI at `.github/workflows/ci.yml` |
| `sample-workflows/integration-tests.yml` | Integration test template | Kept as reference |
| `sample-workflows/perf-tests.yml` | Perf test template | Kept as reference |
| `sample-workflows/README.md` | Workflow docs | Kept as reference |
| `docs/agent-router-setup.md` | Daemon setup guide | Kept as-is |
| `docs/roadmap-from-kiro-specs.md` | ROADMAP generation method | Kept as-is |
| `prompts/agent-router.md` | RFC-2119 session contract | Kept as-is |
| `config.example.json` | Agent-router config example | Kept as-is |
| `CLAUDE.md` | Short pointer for Claude | Kept as-is |
| `ROADMAP.md` | Work queue | Kept (items added by maintainer) |
| `README.md` | Project README | Kept as-is (will be rewritten in a future item) |
| `LICENSE` | MIT license | Kept as-is |

## Added for libby-mcp

| File/Dir | Role | Task |
|----------|------|------|
| `pyproject.toml` | Python project config (deps, ruff, mypy, pytest) | 1 |
| `requirements.txt` | Pinned runtime deps for Lambda | 1 |
| `requirements-dev.txt` | Pinned dev deps | 1 |
| `Makefile` | Standard build/test/lint targets | 1 |
| `src/__init__.py` | Package root | 1 |
| `src/tools/__init__.py` | Tools sub-package | 1 |
| `tests/__init__.py` | Test package | 1 |
| `tests/conftest.py` | Shared pytest fixtures | 1 |
| `tests/test_smoke.py` | Smoke test (prevents exit code 5) | 1 |
| `tests/fixtures/*.json` | Mock Thunder API responses | 1 |
| `tests/integration/__init__.py` | Integration test package | 1 |
| `tests/integration/test_placeholder.py` | Placeholder (skips until Task 11) | 1 |
| `scripts/validate_spec.py` | Spec alignment validator | 1b |
| `scripts/check_compliance.py` | Implementation compliance checker | 1b |
| `docs/design-docs/` | Spec copies (requirements, design, tasks) | 1 |
| `docs/references/thunder_api_reference.md` | Thunder API quick reference | 1b |
| `docs/references/mcp_spec_excerpt.md` | MCP protocol reference | 1b |
| `docs/references/libby.py` | Reference Thunder client | 1 |
| `docs/references/libby_triage.py` | Reference triage logic | 1 |
| `docs/references/LIBBY_RECIPE.md` | Endpoint docs | 1 |
| `docs/references/NONFICTION_TASTE_PROFILE.md` | Taste profile ref | 1 |
| `docs/references/thunder_openapi_v1.json` | Partial OpenAPI spec | 1 |
| `.github/workflows/ci.yml` | Python-specific CI pipeline | 1a |
| `.pre-commit-config.yaml` | Pre-commit hooks (ruff, mypy) | 1b |
| `AGENTS.md` | Rewritten with libby-mcp specifics | 1 |
| `TEMPLATE_INVENTORY.md` | This file | 0 |

## Harness Engineering Gaps (Template vs NFR-7)

| Component | Template State | NFR-7 Requirement | Resolution |
|-----------|---------------|-------------------|------------|
| CI workflow | Generic auto-detect (`sample-workflows/ci.yml`) | Python-specific lint/typecheck/test jobs (NFR-7.2) | Custom `.github/workflows/ci.yml` with ruff, mypy, pytest jobs |
| Test harness | None (template is language-agnostic) | pytest fixtures for Thunder API mocking (NFR-7.1) | `tests/conftest.py` + `tests/fixtures/` |
| AGENTS.md | Generic template guide | Libby-specific navigation (NFR-7.4) | Rewritten with project-specific commands and layout |
| Linter config | None | ruff rules for architecture constraints (NFR-7.3) | `pyproject.toml [tool.ruff]` section |
| Pre-commit | None | Hooks for lint + typecheck (NFR-7.3) | `.pre-commit-config.yaml` |
| Compliance checker | None | Mechanical AC validation (NFR-7.1) | `scripts/check_compliance.py` |
| Spec validator | None | Structure/drift detection (NFR-7.1) | `scripts/validate_spec.py` |
