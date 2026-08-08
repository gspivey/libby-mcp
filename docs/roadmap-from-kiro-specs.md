# Serializing a Kiro spec into a ROADMAP

This document describes the **method** for turning a [Kiro](https://kiro.dev) spec into the
[`ROADMAP.md`](../ROADMAP.md) work queue that agent-router consumes. The agent ships one
ROADMAP item per session; the quality of that loop depends almost entirely on how well the
spec is serialized. Read this before you write your own ROADMAP.

A Kiro spec is three documents in a `.kiro/specs/<feature>/` directory:

- **`requirements.md`** — what the feature must do, in EARS/Kiro acceptance-criteria form.
- **`design.md`** — how it will be built: components, types, file layout, interfaces.
- **`tasks.md`** — a flat checklist of implementation sub-tasks, usually grouped and
  numbered (`1.1`, `1.2`, `2.1`, …).

The ROADMAP is **not** a copy of `tasks.md`. It is a re-grouping of those sub-tasks into
**PR-sized, dependency-ordered items**, each of which an agent can implement, test, and
merge in a single session. The mapping is many-to-one: one ROADMAP item bundles several
`tasks.md` sub-tasks.

For a real, large example of the output, see the ROADMAP of
[`gspivey/dpdk-stdlib-rust`](https://github.com/gspivey/dpdk-stdlib-rust/blob/development/ROADMAP.md):
two multi-crate Kiro specs (`tcp-support`, `s2n-quic-provider`) serialized into 30-plus
ordered items, each citing the exact `tasks.md` numbers it covers.

---

## Why serialize at all?

The agent-router daemon wakes an agent, which picks the **first unchecked item** and ships
exactly one PR (see [`AGENTS.md`](../AGENTS.md) and
[`prompts/agent-router.md`](../prompts/agent-router.md)). That model only works if the queue
has three properties:

1. **One item is one PR.** If an item is too large, the PR is unreviewable and CI takes too
   long to close the loop. If it is too small, you spend more sessions on overhead than on
   code. Target roughly **300–500 lines** of new or modified code per item, tests included.
2. **Items are independently mergeable.** Each item must build, test, and merge to
   `development` on its own. It may depend on *earlier* items (already merged) but never on
   a *later* one.
3. **The order is a valid topological sort.** When the agent reaches item N, every
   prerequisite it imports, calls, or extends is already on `development`. No item may
   reference code that a later item introduces.

Serialization is the act of imposing those three properties on a spec that, on its own, is
just a pile of requirements and an unordered (or loosely ordered) task list.

---

## The method, step by step

### 1. Read all three spec documents fully

Start from `requirements.md` to understand *what* and *why*, then `design.md` for the
component and file layout, then `tasks.md` for the unit of work. Note every type, module,
and file path the design names — these become the nouns in your item titles.

### 2. Build the dependency graph

For each `tasks.md` sub-task, ask: *what must already exist for this to compile and pass its
tests?* A codec must exist before the engine that calls it; a type must exist before the
function returning it; a public API must exist before the integration test exercising it.
Draw (mentally or on paper) the edges. The result is a directed acyclic graph over
sub-tasks. If you find a cycle, the spec has a design problem — resolve it before
serializing (usually by splitting a type out into its own earlier item).

### 3. Group sub-tasks into PR-sized items

Walk the graph in dependency order and accrete sub-tasks into an item until it reaches the
~300–500-line budget or a natural seam (a complete type, a finished codec, one engine
state-machine path). Prefer grouping sub-tasks that share a file or a test fixture. Each
item should leave the tree **green**: it builds and all its tests pass with nothing stubbed
that a later item is responsible for. A good item is a sentence: "add X and its tests."

A few sizing heuristics:

- A foundational item (new crate/module skeleton, shared types) is often light on lines but
  unblocks many later items — keep it focused; do not pad it.
- A property-test or integration-test item can stand alone once the code it tests has
  merged.
- If a single `tasks.md` sub-task is itself ~500 lines, it becomes its own item; the
  many-to-one mapping is a guideline, not a rule.

### 4. Topologically order the items

Order the items so every item's prerequisites appear above it. Within the freedom the graph
allows, prefer ordering that lets an agent demonstrate end-to-end behavior early (a thin
walking skeleton before deep features). The first item should have no unmet prerequisites —
it is what the agent picks on the very first session.

### 5. Map each item back to `tasks.md`

Record, on each item, the exact `tasks.md` sub-task numbers it covers (the `Spec:` line
below). This is what lets the agent tick the matching `tasks.md` checkboxes when the item
merges, keeping `tasks.md` and `ROADMAP.md` in lockstep. Every sub-task in `tasks.md` must
appear in exactly one ROADMAP item — no orphans, no duplicates.

### 6. Write each item in the exact format

Use this shape for every Active Roadmap item (verbatim — the agent and the tooling rely on
it):

```markdown
### N. Title

A short paragraph: what the item delivers, the key types/files it touches, the tests it
adds, and which earlier item(s) it builds on. One PR's worth of work, described in prose.

- Spec: `.kiro/specs/<feature>/` · tasks `a.b`, `c.d`
- [ ] Complete · PR: —
```

Rules for the format:

- `### N. Title` — sequential number, then a terse noun-phrase title naming the thing built.
- The paragraph names concrete files/types and states the dependency on prior items so a
  reviewer understands the slice without opening the spec.
- The `Spec:` line points at the spec **directory** and lists the covered `tasks.md`
  numbers in backticks, separated by the middle dot `·`.
- The checkbox line is **literally** `- [ ] Complete · PR: —` until merge, then becomes
  `- [x] Complete · PR: #<number>`.
- Separate items with a `---` rule.

### 7. Keep the file's three sections

The ROADMAP has a header paragraph (how the file is used and sized), an **`## Active
Roadmap`** section (the ordered items), and an empty **`## Completed`** section that items
move into after they merge. Larger specs may add a "Future Specs (Not Yet Written)" note
for work that still needs Kiro spec files before an agent can pick it up — see the
dpdk-stdlib-rust ROADMAP for that pattern.

---

## Worked walkthrough: the bundled `example-feature`

The template ships one throwaway spec, [`.kiro/specs/example-feature/`](../.kiro/specs/example-feature/),
describing a `greet` command-line tool. Here is how it serializes into the two items in
[`ROADMAP.md`](../ROADMAP.md).

**The spec.** `requirements.md` states two requirements:

- **R1** — running the tool with no arguments prints `Hello, world!`.
- **R2** — a `--name <NAME>` option prints `Hello, <NAME>!`.

`design.md` fixes the implementation: Python 3.12, a `greet(name: str | None) -> str`
function at `src/greet.py` with an `argparse` CLI runnable as `python -m greet` /
`python src/greet.py`, and `pytest` tests at `tests/test_greet.py`.

`tasks.md` groups the work:

- **Task group 1** — `1.1` implement `greet()` and the default-output CLI; `1.2` test the
  default greeting.
- **Task group 2** — `2.1` add the `--name` option; `2.2` test the named greeting.

**Apply the method.**

1. *Read.* Two requirements, one module, one test file, four sub-tasks.
2. *Dependency graph.* `2.1` (the `--name` option) extends the CLI created in `1.1`; the
   test `2.2` depends on `2.1`; the test `1.2` depends on `1.1`. So group 2 depends on
   group 1, and nothing else.
3. *Group into items.* The natural seam is the requirement boundary. Group 1 (`1.1` + `1.2`)
   delivers a complete, green slice: the function, the CLI scaffold, and a passing
   default-output test. Group 2 (`2.1` + `2.2`) is a second complete slice on top of it.
   Each is comfortably under the line budget.
4. *Order.* Item 1 = group 1 (no prerequisites — the agent's first pick). Item 2 = group 2
   (depends on item 1 being merged).
5. *Map back.* Item 1 covers tasks `1.1`, `1.2`; item 2 covers tasks `2.1`, `2.2`. Every
   `tasks.md` sub-task is claimed exactly once.
6. *Write the items:*

```markdown
### 1. Greeting function and default output

Implement the core `greet(name: str | None) -> str` function at `src/greet.py` and wire up
the `argparse` CLI so that running the tool with no arguments prints `Hello, world!`. ...

- Spec: `.kiro/specs/example-feature/` · tasks `1.1`, `1.2`
- [ ] Complete · PR: —

### 2. `--name` option

Add the `--name <NAME>` option to the CLI so that `greet(name)` returns `Hello, <NAME>!` ...

- Spec: `.kiro/specs/example-feature/` · tasks `2.1`, `2.2`
- [ ] Complete · PR: —
```

That is the entire `## Active Roadmap` for the example. When you replace the example with
your own spec, delete these two items and `.kiro/specs/example-feature/`, then run the same
six steps over your `.kiro/specs/<your-feature>/`.

---

## Checklist before you commit a ROADMAP

- [ ] Every `tasks.md` sub-task appears in exactly one item.
- [ ] Each item is one PR's worth of work (~300–500 lines, tests included).
- [ ] Each item leaves the tree green on its own — nothing it ships is stubbed pending a
      later item.
- [ ] Items are in valid topological order: no item references code a later item introduces.
- [ ] Every item has a number, a title, a paragraph, a `Spec:` line with `tasks.md` numbers,
      and a `- [ ] Complete · PR: —` line.
- [ ] The file has a header paragraph, `## Active Roadmap`, and an (initially empty)
      `## Completed` section.
