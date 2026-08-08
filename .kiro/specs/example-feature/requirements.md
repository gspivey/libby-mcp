<!--
EXAMPLE SPEC — REPLACE ME.

This is a bundled example feature that ships with the agent-router-template so the
ROADMAP -> agent -> CI loop is demonstrable immediately. It describes a tiny `greet`
command-line tool. The template ships this SPEC only, never the implementation code;
the agent writes the code when it implements the matching ROADMAP item.

Delete the entire `.kiro/specs/example-feature/` directory (and its two ROADMAP items)
once you have written your own spec. See docs/roadmap-from-kiro-specs.md for how to
serialize a Kiro spec into ROADMAP.md.
-->

# Requirements: Example Feature — `greet` CLI

## Introduction

`greet` is a minimal Python command-line tool used to exercise the full agent-router
workflow end to end. Run with no arguments, it prints a default greeting. Given a name,
it greets that name instead. The feature is intentionally small: it fits in two ROADMAP
items so a maintainer can watch a complete branch -> implement -> test -> PR -> CI ->
merge cycle without reading a large codebase.

The acceptance criteria below use EARS-style phrasing (WHEN / THE system SHALL). Each
requirement is realized by one ROADMAP item and the matching task group in `tasks.md`.

## Requirement 1: Default greeting

**User Story:** As a user, I want to run the tool with no arguments and see a friendly
default greeting, so that the tool is useful with zero configuration.

### Acceptance Criteria

1. WHEN the tool is invoked with no arguments, THE system SHALL print exactly
   `Hello, world!` followed by a single trailing newline to standard output.
2. WHEN the tool finishes a no-argument run successfully, THE system SHALL exit with
   status code `0`.
3. WHEN the greeting function is called with no name (or `None`), THE system SHALL
   return the string `Hello, world!` (without a trailing newline).

## Requirement 2: Named greeting

**User Story:** As a user, I want to pass a name on the command line, so that the tool
greets a specific person.

### Acceptance Criteria

1. WHEN the tool is invoked with `--name <NAME>`, THE system SHALL print exactly
   `Hello, <NAME>!` followed by a single trailing newline to standard output, where
   `<NAME>` is the supplied value.
2. WHEN the greeting function is called with a non-empty name, THE system SHALL return
   the string `Hello, <NAME>!` (without a trailing newline).
3. WHEN the tool is invoked with `--name` and an empty string, THE system SHALL fall
   back to the default and print `Hello, world!`.
4. WHEN the tool is invoked with `-h` or `--help`, THE system SHALL print usage text
   that documents the `--name` option and SHALL exit with status code `0`.
