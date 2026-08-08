<!--
EXAMPLE SPEC — REPLACE ME.

This design document accompanies the bundled `greet` example feature. It exists only to
demonstrate the agent-router workflow and is meant to be deleted. Replace the whole
`.kiro/specs/example-feature/` directory with your own spec, then regenerate ROADMAP.md
(see docs/roadmap-from-kiro-specs.md).
-->

# Design: Example Feature — `greet` CLI

## Overview

A single-module Python tool. `greet(name)` returns the greeting string; an `argparse`
CLI wraps it and prints the result. There are no third-party runtime dependencies.

## Toolchain

- Language: **Python 3.12** (uses the `str | None` union syntax directly).
- Tests: **pytest**.
- Lint (optional): **ruff**, if configured. CI does not require it.

## Layout

```
src/greet.py            # implementation: greet() + argparse CLI
tests/test_greet.py     # pytest tests
```

`src/` is placed on the path so the module is importable as `greet` and runnable as
`python -m greet`. A `pyproject.toml` (or `pytest.ini`/`conftest.py` adding `src/` to
`sys.path`) is acceptable; keep it minimal. The agent adds whatever is needed for
`pytest` to discover the module and tests.

## Public interface

```python
def greet(name: str | None = None) -> str:
    """Return the greeting string.

    Returns "Hello, world!" when name is None or empty (after stripping is not
    required; an empty string falls back to the default per Requirement 2.3).
    Otherwise returns "Hello, <name>!".
    """
```

Behavior:

- `greet()` / `greet(None)` / `greet("")` -> `"Hello, world!"`
- `greet("Ada")` -> `"Hello, Ada!"`

The returned string carries no trailing newline; the CLI adds it via `print`.

## CLI

`argparse` parser with one optional argument:

- `--name NAME` — name to greet. Default: unset (`None`).

The CLI is exposed two ways, both calling the same `main()`:

- `python -m greet`
- `python src/greet.py`

`main(argv: list[str] | None = None) -> int` parses args, calls `greet(args.name)`,
`print`s the result, and returns an exit code (`0` on success). The `if __name__ ==
"__main__":` guard calls `raise SystemExit(main())`.

## Testing strategy

`tests/test_greet.py` covers the `greet()` return values directly (fast, no subprocess)
and exercises the CLI via `main()` with `capsys` to assert printed output. Tests map to
acceptance criteria:

- Requirement 1: `greet(None)` and the no-arg CLI print path.
- Requirement 2: `greet("Ada")`, the `--name` CLI path, and the empty-name fallback.

## Out of scope

Internationalization, configuration files, logging, packaging for distribution. This is
a demonstration feature; keep it small.
