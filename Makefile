.PHONY: test lint typecheck format ci validate compliance preflight integration integration-test install

## Install project in editable mode with dev dependencies
install:
	pip install -e ".[dev]"

## Run unit tests with coverage (ignores integration tests)
test:
	pytest tests/ --ignore=tests/integration --cov=src --cov-report=term-missing --cov-fail-under=80

## Run ruff linter and format check
lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

## Run mypy type checker on src/
typecheck:
	mypy src/

## Auto-format code with ruff
format:
	ruff format src/ tests/
	ruff check --fix src/ tests/

## Run lint + typecheck + test (mirrors CI pipeline locally)
ci: lint typecheck test

## Validate spec alignment
validate:
	python scripts/validate_spec.py

## Run compliance checker
compliance:
	python scripts/check_compliance.py

## Full pre-push check: validate + compliance + ci
preflight: validate compliance ci

## Run integration tests against real Thunder API
integration:
	RUN_INTEGRATION=1 pytest tests/integration/ -v -m integration

## Build + run the containerized MCP server and run the protocol suite against it
integration-test:
	docker compose -f docker-compose.integration.yml up -d --build
	@for i in $$(seq 1 30); do \
		code=$$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/mcp || true); \
		if [ "$$code" = "405" ]; then echo "server up"; break; fi; \
		sleep 2; \
	done
	RUN_LOCAL_INTEGRATION=1 MCP_ENDPOINT=http://localhost:8000/mcp MCP_API_KEY=ci-test-key \
		pytest tests/integration/test_mcp_protocol.py -v -m local_integration; \
		status=$$?; \
		docker compose -f docker-compose.integration.yml down -v; \
		exit $$status
