# ROADMAP

Ordered work queue for agent-router sessions. This is the serialized form of the Kiro
specs under [`.kiro/specs/`](.kiro/specs/): one dependency-ordered list of PR-sized items
an agent ships one at a time.

**How an agent uses this file:** pick the **first item whose checkbox is unchecked**
(`- [ ] Complete`), implement exactly that one item, and read every spec file named on the
item's `Spec:` line *before* writing any code. When CI is green, tick the item's box with
the PR number (`- [x] Complete · PR: #<n>`), tick the matching checkboxes in the relevant
`tasks.md`, and squash-merge the branch into `development`. **One item per session — never
start a second.** The full session contract lives in
[`prompts/agent-router.md`](prompts/agent-router.md) and the conventions in
[`AGENTS.md`](AGENTS.md).

Each item is sized for a single reviewable PR (roughly 300–500 lines of new or modified
code, tests included) and bundles a handful of related `tasks.md` sub-tasks. Items are
topologically ordered: by the time an agent reaches an item, every prerequisite it builds
on has already merged to `development`. For the method used to turn a spec into this queue,
see [`docs/roadmap-from-kiro-specs.md`](docs/roadmap-from-kiro-specs.md). For a real,
large-scale example of this format in a production project, see the ROADMAP of
[`gspivey/dpdk-stdlib-rust`](https://github.com/gspivey/dpdk-stdlib-rust/blob/development/ROADMAP.md).

---

## Active Roadmap

> **These two items are the bundled EXAMPLE.** They serialize the throwaway
> [`greet` command-line tool](.kiro/specs/example-feature/) spec so the agent-router loop is
> demonstrable the moment you create a repo from this template. **Delete both items (and the
> `.kiro/specs/example-feature/` directory) and replace them with the serialized form of
> your own spec.** See [`docs/roadmap-from-kiro-specs.md`](docs/roadmap-from-kiro-specs.md)
> for the worked walkthrough that produced exactly these two items.

### 1. Greeting function and default output

Implement the core `greet(name: str | None) -> str` function at `src/greet.py` and wire up
the `argparse` CLI so that running the tool with no arguments prints `Hello, world!`. This
item establishes the module layout (`python -m greet` / `python src/greet.py`) and the
`pytest` harness; the default-greeting test asserts the no-argument behavior end to end.
This is the foundation item — item 2 extends the CLI it creates.

- Spec: `.kiro/specs/example-feature/` · tasks `1.1`, `1.2`
- [ ] Complete · PR: —

---

### 2. `--name` option

Add the `--name <NAME>` option to the CLI so that `greet(name)` returns `Hello, <NAME>!`
and `python -m greet --name Ada` prints `Hello, Ada!`. Builds directly on the `greet()`
function and CLI scaffold merged in item 1; the option-handling test covers a supplied name
alongside the preserved default. No new module or dependency is introduced.

- Spec: `.kiro/specs/example-feature/` · tasks `2.1`, `2.2`
- [ ] Complete · PR: —

---

## Completed

Items move here after they merge to `development`.

*(none yet)*
