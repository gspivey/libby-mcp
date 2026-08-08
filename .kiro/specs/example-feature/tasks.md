<!--
EXAMPLE SPEC — REPLACE ME.

These are the implementation tasks for the bundled `greet` example feature. The two task
groups map one-to-one onto the two example items in ROADMAP.md:

  - Group 1 (tasks 1.1, 1.2) -> ROADMAP item 1 "Greeting function and default output"
  - Group 2 (tasks 2.1, 2.2) -> ROADMAP item 2 "`--name` option"

All boxes are unchecked: the agent ships one ROADMAP item per session, ticking the task
boxes and the ROADMAP checkbox (with the PR number) when CI is green. Delete the whole
`.kiro/specs/example-feature/` directory once you have written your own spec.
-->

# Tasks: Example Feature — `greet` CLI

## Group 1 — Greeting function and default output

_Realizes Requirement 1. ROADMAP item 1._

- [ ] **1.1** Create `src/greet.py` with `greet(name: str | None = None) -> str`
  returning `Hello, world!` when `name` is `None` or empty and `Hello, <name>!`
  otherwise. Add the `argparse` CLI scaffolding and `main(argv=None) -> int` that prints
  the result and returns `0`, runnable as `python -m greet` and `python src/greet.py`.
  Add the minimal config needed for `pytest` to import the module from `src/`.
  _Requirements: 1.1, 1.2, 1.3._
- [ ] **1.2** Create `tests/test_greet.py` with pytest tests for the default greeting:
  `greet(None) == "Hello, world!"` and a CLI test that runs `main([])` and asserts
  `Hello, world!\n` on stdout with exit code `0` (use `capsys`).
  _Requirements: 1.1, 1.2, 1.3._

## Group 2 — `--name` option

_Realizes Requirement 2. ROADMAP item 2._

- [ ] **2.1** Extend `src/greet.py` so `greet("Ada")` returns `Hello, Ada!`, the
  `argparse` CLI accepts `--name NAME` and passes it through, and an empty `--name`
  value falls back to the default greeting. Confirm `-h`/`--help` documents `--name` and
  exits `0`.
  _Requirements: 2.1, 2.2, 2.3, 2.4._
- [ ] **2.2** Extend `tests/test_greet.py` with pytest tests for the named greeting:
  `greet("Ada") == "Hello, Ada!"`, a CLI test that runs `main(["--name", "Ada"])` and
  asserts `Hello, Ada!\n` on stdout, and an empty-name fallback test asserting
  `Hello, world!\n`.
  _Requirements: 2.1, 2.2, 2.3._
