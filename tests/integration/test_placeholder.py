"""Placeholder so pytest collection doesn't fail on empty integration dir."""

import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("RUN_INTEGRATION"),
        reason="requires RUN_INTEGRATION=1",
    ),
]


def test_integration_placeholder() -> None:
    """Replaced by real integration tests in Task 11."""
    pytest.skip("No live integration tests implemented yet")
