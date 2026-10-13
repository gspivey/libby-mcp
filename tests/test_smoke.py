"""Smoke test to prevent pytest exit code 5 (no tests collected)."""


def test_smoke():
    """Project is importable and pytest collection succeeds."""
    import src  # noqa: F401

    assert True
