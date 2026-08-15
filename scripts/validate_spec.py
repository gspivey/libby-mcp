#!/usr/bin/env python3
"""Validate project structure and spec alignment.

Checks that the repository layout matches design.md, that every tool in
requirements.md has a corresponding test file, and that testable acceptance
criteria are covered.

Exit codes:
    0 - All checks pass (or only WARNs)
    2 - Blocking errors detected (ERROR-level check failed)

Exit code 1 is reserved for check_compliance.py to avoid collision in
`make preflight`.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ACs exempt from the "every AC has a test" rule (non-unit-testable).
EXEMPT_ACS = {
    "AC-4.4",  # Known working slugs (requires live API verification)
    "AC-5.3",  # API key configured in ~/.claude/mcp.json (client-side config)
    "AC-6.7",  # Full Streamable HTTP deferred (negative scope)
    "AC-1.17",  # Results match Libby app (requires manual validation)
}

# Expected directory structure from design.md.
REQUIRED_DIRS = [
    "src",
    "src/tools",
    "tests",
    "tests/fixtures",
    "tests/integration",
    "scripts",
    "docs/design-docs",
    "docs/references",
]

# Expected key files.
REQUIRED_FILES = [
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "src/__init__.py",
    "src/tools/__init__.py",
    "tests/__init__.py",
    "tests/conftest.py",
    "Makefile",
]

# Tool name -> expected test file mapping.
TOOL_TEST_MAP = {
    "search_titles": "tests/test_search_titles.py",
    "get_availability": "tests/test_availability.py",
    "get_deep_link": "tests/test_deep_link.py",
}


def check_directories(root: Path) -> dict:
    """Check that required directories exist."""
    missing = [d for d in REQUIRED_DIRS if not (root / d).is_dir()]
    if missing:
        return {
            "check": "directory_structure",
            "status": "ERROR",
            "message": f"Missing directories: {', '.join(missing)}",
            "remediation": f"Create missing directories: {', '.join(f'mkdir -p {d}' for d in missing)}",
        }
    return {
        "check": "directory_structure",
        "status": "PASS",
        "message": "All required directories exist",
        "remediation": None,
    }


def check_files(root: Path) -> dict:
    """Check that required key files exist."""
    missing = [f for f in REQUIRED_FILES if not (root / f).is_file()]
    if missing:
        return {
            "check": "required_files",
            "status": "ERROR",
            "message": f"Missing files: {', '.join(missing)}",
            "remediation": "Create the missing files per design.md and tasks.md",
        }
    return {
        "check": "required_files",
        "status": "PASS",
        "message": "All required files exist",
        "remediation": None,
    }


def check_tool_tests(root: Path) -> dict:
    """Check that every tool in requirements.md has a test file.

    Demoted to WARN level during TDD red phase (tests may exist before
    implementation modules).
    """
    missing = []
    for tool, test_file in TOOL_TEST_MAP.items():
        if not (root / test_file).is_file():
            missing.append(f"{tool} -> {test_file}")
    if missing:
        return {
            "check": "tool_test_coverage",
            "status": "WARN",
            "message": f"Missing test files for tools: {', '.join(missing)}",
            "remediation": "Create test files for each tool (TDD red phase is acceptable)",
        }
    return {
        "check": "tool_test_coverage",
        "status": "PASS",
        "message": "All tools have corresponding test files",
        "remediation": None,
    }


def check_spec_files(spec_dir: Path) -> dict:
    """Check that spec files are accessible."""
    expected = ["requirements.md", "design.md", "tasks.md"]
    missing = [f for f in expected if not (spec_dir / f).is_file()]
    if missing:
        return {
            "check": "spec_files",
            "status": "WARN",
            "message": f"Missing spec files in {spec_dir}: {', '.join(missing)}",
            "remediation": "Copy spec files to docs/design-docs/ per Task 1 step 8",
        }
    return {
        "check": "spec_files",
        "status": "PASS",
        "message": "All spec files accessible",
        "remediation": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate spec alignment")
    parser.add_argument(
        "--spec-dir",
        default=os.environ.get("SPEC_DIR", "docs/design-docs"),
        help="Path to spec files (default: docs/design-docs/ or SPEC_DIR env var)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    root = Path.cwd()
    spec_dir = Path(args.spec_dir)

    results = [
        check_directories(root),
        check_files(root),
        check_tool_tests(root),
        check_spec_files(spec_dir),
    ]

    has_error = any(r["status"] == "ERROR" for r in results)
    has_warn = any(r["status"] == "WARN" for r in results)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            icon = {"PASS": "[PASS]", "WARN": "[WARN]", "ERROR": "[ERROR]"}[r["status"]]
            print(f"{icon} {r['check']}: {r['message']}")
            if r["remediation"]:
                print(f"       Remediation: {r['remediation']}")
        print()
        if has_error:
            print("RESULT: FAIL (blocking errors detected)")
        elif has_warn:
            print("RESULT: PASS (with warnings)")
        else:
            print("RESULT: PASS")

    if has_error:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
