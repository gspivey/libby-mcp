.PHONY: test lint typecheck format ci validate compliance preflight integration install

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
