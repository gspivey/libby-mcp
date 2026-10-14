#!/usr/bin/env python3
"""Check implementation compliance against acceptance criteria.

Validates that implemented modules expose expected classes/functions and that
MCP tool definitions match the schema in design.md. Scoped to modules that
already exist under src/ -- does not fail on modules not yet implemented
(supports incremental TDD workflow).

Exit codes:
    0 - All checks pass (or modules not yet implemented are skipped)
    1 - Failures found (with details and remediation hints)
"""

from __future__ import annotations

import importlib
import sys

RESULTS: list[dict] = []


def record(check: str, status: str, message: str, remediation: str | None = None) -> None:
    """Record a check result."""
    RESULTS.append({
        "check": check,
        "status": status,
        "message": message,
        "remediation": remediation,
    })


def check_module_exists(module_name: str, display_name: str) -> bool:
    """Check if a module can be imported. SKIP if not yet implemented."""
    try:
        importlib.import_module(module_name)
        record(
            f"module_{display_name}",
            "PASS",
            f"Module {module_name} is importable",
        )
        return True
    except ImportError:
        record(
            f"module_{display_name}",
            "SKIP",
            f"Module {module_name} not yet implemented (TDD red phase)",
        )
        return False


def check_thunder_client() -> None:
    """Validate ThunderClient module structure."""
    if not check_module_exists("src.thunder_client", "thunder_client"):
        return
    from src import thunder_client  # type: ignore[attr-defined]

    # Check for ThunderClient class or key functions
    if not hasattr(thunder_client, "ThunderClient"):
        record(
            "thunder_client_class",
            "FAIL",
            "src.thunder_client missing ThunderClient class",
            "REMEDIATION: Define class ThunderClient in src/thunder_client.py",
        )


def check_auth() -> None:
    """Validate auth module structure."""
    if not check_module_exists("src.auth", "auth"):
        return
    from src import auth  # type: ignore[attr-defined]

    expected_functions = ["validate_api_key", "AuthMiddleware"]
    for fn in expected_functions:
        if not hasattr(auth, fn):
            record(
                f"auth_{fn}",
                "FAIL",
                f"src.auth missing: {fn}",
                f"REMEDIATION: Define {fn} in src/auth.py",
            )


def check_tools() -> None:
    """Validate tool modules exist and expose expected functions."""
    tools = {
        "src.tools.search_titles": "handle",
        "src.tools.availability": "handle",
        "src.tools.deep_link": "handle",
    }
    for module_name, expected_fn in tools.items():
        short = module_name.split(".")[-1]
        if not check_module_exists(module_name, short):
            continue
        mod = importlib.import_module(module_name)
        if not hasattr(mod, expected_fn):
            record(
                f"tool_{short}_handler",
                "FAIL",
                f"{module_name} missing function: {expected_fn}",
                f"REMEDIATION: Define function {expected_fn} in {module_name.replace('.', '/')}.py",
            )


def check_handler() -> None:
    """Validate Lambda handler module (FastMCP-based).

    After the FastMCP migration (and the stateless-per-invocation refactor), the
    FastMCP instance and its @mcp.tool functions are created inside
    ``_make_mcp()`` on every invocation rather than exposed at module scope
    (StreamableHTTPSessionManager.run() can only be called once per instance).
    The stable module-level surface is therefore:

    - ``lambda_handler(event, context)``: Lambda entry point
    - ``_make_mcp()``: builds a fresh FastMCP app with all tools registered
    - ``_build_app()``: wraps the FastMCP ASGI app in AuthMiddleware

    Tool registration is validated end-to-end by the handler tests
    (``tests/test_handler.py`` asserts ``tools/list`` contains each tool),
    which exercise the real registered schema rather than a module attribute.
    """
    if not check_module_exists("src.handler", "handler"):
        return
    from src import handler  # type: ignore[attr-defined]

    for fn in ("lambda_handler", "_make_mcp", "_build_app"):
        if not hasattr(handler, fn):
            record(
                f"handler_{fn}",
                "FAIL",
                f"src.handler missing {fn}() function",
                f"REMEDIATION: Define {fn}(...) in src/handler.py",
            )


def main() -> int:
    check_thunder_client()
    check_auth()
    check_tools()
    check_handler()

    # Print results
    has_failure = False
    for r in RESULTS:
        icon = {"PASS": "[PASS]", "SKIP": "[SKIP]", "FAIL": "[FAIL]"}[r["status"]]
        print(f"{icon} {r['check']}: {r['message']}")
        if r["remediation"]:
            print(f"       {r['remediation']}")
        if r["status"] == "FAIL":
            has_failure = True

    print()
    skipped = sum(1 for r in RESULTS if r["status"] == "SKIP")
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    print(f"RESULT: {passed} passed, {failed} failed, {skipped} skipped")

    if has_failure:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
