"""Shared pytest fixtures for Libby MCP tests.

Provides mock Thunder API responses and client fixtures for unit testing
without network access.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_search_response() -> dict:
    """Load a realistic Thunder API search response (3 items with all fields)."""
    return json.loads((FIXTURES_DIR / "search_response.json").read_text())


@pytest.fixture
def sample_availability_response() -> dict:
    """Load a realistic Thunder API availability response envelope."""
    return json.loads((FIXTURES_DIR / "availability_response.json").read_text())


@pytest.fixture
def sample_error_responses() -> dict:
    """Load a collection of error scenarios (timeout, 500, bad slug, rate limit)."""
    return json.loads((FIXTURES_DIR / "error_responses.json").read_text())


@pytest.fixture
def mock_thunder_client(sample_search_response: dict) -> MagicMock:
    """A mocked ThunderClient that returns fixture data by default.

    The mock's search() method returns the sample_search_response fixture.
    Override return_value on specific methods for custom test scenarios.
    """
    client = MagicMock()
    client.search.return_value = sample_search_response
    client.get_availability.return_value = {"items": []}
    return client
