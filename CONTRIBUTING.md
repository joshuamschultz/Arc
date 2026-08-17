# Contributing to Arc

This repository is currently private. External-contribution policy (who
can open a PR, what review is required) is not yet published — if you are
reading this without already having repository access, contact the
maintainer before starting work. Everything below describes the actual
mechanics of making a change once you have access.

## Prerequisites

- Python 3.11 or newer (`pyproject.toml` requires `>=3.11`; CI additionally
  verifies 3.12 and 3.13 on Linux, macOS, and Windows).
- [`uv`](https://docs.astral.sh/uv/) — Arc is a `uv` workspace: one
  lockfile (`uv.lock`) and one root `pyproject.toml` govern every package
  under `packages/*`.

## Getting Set Up

```bash
git clone git@github.com:joshuamschultz/Arc.git
cd Arc
uv sync --all-packages
```

`uv sync --all-packages` installs every workspace package in editable mode
against the single lockfile, so a change in one package (e.g. `arcllm`) is
immediately visible to a package that depends on it (e.g. `arcagent`),
with no reinstall step.

For local development you also want the `dev` and `test` dependency
groups (ruff, mypy, pytest, pip-audit, and friends):

```bash
uv sync --all-packages --all-groups
```

This is what CI runs, so matching it locally avoids "works on my machine"
gate failures.

Running the CLI:

```bash
arc agent create myagent --model anthropic/claude-sonnet-5   # scaffold a new agent
arc agent build myagent --check                              # validate, writes nothing
arc agent run myagent "some task"                             # run one task
arc agent chat myagent                                        # interactive chat
arc ui start --port 8420                                      # multi-agent dashboard
```

`arc agent build` without `--force` refuses to overwrite an existing
`arcagent.toml` and exits 1. `--force` regenerates the file, keeping only
`[agent].name` and `[identity].did` — every other hand-edited value reverts
to the template default.

Docker is the canonical path for *running/deploying* Arc, not for
contributing — the image is a frozen `uv sync --frozen --no-dev` build
with no mounted source tree to iterate on. Use the local `uv` workspace
above for development; only rebuild the Docker image when your change
touches `Dockerfile`, `deploy/entrypoint.sh`, or `docker-compose.yml`.

## Repository Layout and the Dependency Rule

Sixteen packages live under `packages/*`: `arcagent`, `arcbundle`,
`arccli`, `arcgateway`, `arcllm`, `arcmas`, `arcmemory`, `arcmodel`,
`arcprompt`, `arcrun`, `arcskill`, `arcstore`, `arcteam`, `arctrust`,
`arctui`, `arcui`. Each is independently installable.

**Dependencies point one way, never up, and no layer reaches past its
immediate neighbor:**

```
arcllm          standalone model adapter/router; knows nothing above it
   ^
arcrun          knows arcllm, owns the agentic loop, does not know arcagent
   ^
arcagent        uses the arcrun facade only — never `import arcllm` directly
   ^        ^
arcgateway   arcui
```

- `arctrust` is a leaf: it imports no other Arc package. Everything else
  depends on it for identity, signing, policy, and audit primitives.
- A cross-package consumer imports the root of the package it needs
  (`import arcllm`, `import arcrun`, `import arcagent`) and reaches public
  names off that root, rather than deep-importing internal submodules.
- This is not just a convention — it is enforced by tests. See
  "Architecture Tests" below.

Concerns stay separated along the same lines: `arcllm` owns LLM calls,
`arcrun` owns loop execution, `arcagent` owns the agent (tools, skills,
extensions, memory). `arcagent` must not own LLM-call or loop logic;
`arcrun` must not own agent or LLM-provider logic.

## Workflow

1. **Test first.** Write a failing test that describes the behavior you
   want before writing the implementation.
2. **Implement.** Write the minimal code that makes the test pass.
3. **Verify.** Run the quality gates below and confirm they pass with
   fresh output — not from memory of an earlier run.

If a fix feels like a workaround rather than addressing the actual cause,
it probably is — look for the root cause instead of patching a symptom.

**Leave it correct.** If `ruff`, `mypy`, or any quality gate surfaces an
error while you are working — even in code you did not touch — fix it in
the same PR. "Pre-existing, not my problem" is not an accepted answer
here. If a fix is genuinely out of scope, say so explicitly and ask
before deferring; the default is to fix it.

**No legacy or backward-compatibility code.** This is a local-only
repository with no external consumers to protect. When you change a
behavior, delete the old code in the same edit — no commented-out blocks,
no deprecation shims, no "kept for compatibility" stubs. One line beats
five: prefer the smallest correct change over a resolver/helper/warner
abstraction.

Other rules enforced in review: no `# type: ignore` without an inline
comment explaining why, no bare `except:`, no mutable default arguments,
no monkey-patching, no `print()` (use structured logging), no global
state outside config, no hardcoded secrets or plaintext credentials.
Comment the *why*, not the *what* — code reflects current reality; commit
messages hold the history.

## Quality Gates

Every command below is real — verified against `pyproject.toml`,
`Makefile`, and `.github/workflows/ci.yml`, not aspirational.

```bash
ruff check .                                     # lint
ruff format --check .                            # format (CI checks; does not rewrite)
mypy packages/*/src/ --strict                    # type check, all shipped packages
uv run pytest packages/<pkg>/tests/ -v -m "not slow"   # tests, per package
pip-audit --no-deps -r sbom/requirements-gate.txt       # dependency vulnerability scan
```

`mypy` runs per-package in CI (currently gated for `arcllm`, `arcrun`,
`arcagent`, and `arcmemory` in `ci.yml`; other packages are not yet in
that list). If your change touches a package not currently gated in CI,
run `mypy packages/<pkg>/src/<pkg>/ --strict` locally anyway and fix what
it finds — "leave it correct" does not wait for CI to catch up.

`pip-audit` is scoped to the runtime dependency closure only (dev/test/
tutorial groups excluded). Documented, accepted exceptions live in
`sbom/security-suppressions.txt`.

A `Makefile` wraps some of these with repo-specific plumbing, but only
covers `arcgateway`, `arcagent`, and the root `tests/` — for any other
package, run `ruff`/`mypy`/`pytest` directly scoped to that package.

| Target | What it runs |
|---|---|
| `make lint` | `ruff check` on `arcgateway/src`, `tests/`, `scripts/`, `arcagent/src` |
| `make typecheck` | `mypy packages/arcgateway/src/arcgateway/ --strict` |
| `make test` | `pytest packages/arcgateway/tests/ tests/ -m "not slow"` |
| `make architecture-tests` | `pytest tests/architecture/ -v` |
| `make loc-budgets` | `scripts/check_loc_budgets.py` |
| `make coverage` | `scripts/coverage_report.py` |

**Thresholds:**

| Gate | Threshold |
|---|---|
| Line coverage | ≥ 80% |
| Branch coverage | ≥ 75% |
| Core component coverage | ≥ 90% |
| Cyclomatic complexity | ≤ 10 per function |
| Ruff errors | 0 |
| mypy errors | 0 |
| Critical/high vulnerabilities | 0 |

LOC budgets are enforced by `scripts/check_loc_budgets.py` (non-blank,
non-comment line counts, per package). If a budget check fails, the fix
is almost never "raise the ceiling" — it is usually a signal the code
belongs in a different package. Move it before you widen the budget.

## Testing

Target pyramid: unit 70% / integration 20% / e2e 10%, plus dedicated
security and performance suites.

| Location | What lives there |
|---|---|
| `packages/<pkg>/tests/` | Per-package unit + integration tests — most tests live here |
| `packages/<pkg>/tests/unit/` | Unit tests (present in most packages) |
| `packages/<pkg>/tests/integration/` | In-package integration tests |
| `packages/arcagent/tests/security/` (and similar dirs in `arcbundle`, `arcrun`, `arcmemory`, `arcllm`, `arcgateway`) | Adversarial / security-specific suites |
| `tests/architecture/` | Repo-wide layering invariants (see below) |
| `tests/integration/` | Cross-package end-to-end specs |

Run one package's suite the way CI does:

```bash
uv run pytest packages/arcllm/tests/ -v -m "not slow"
```

The `slow` marker is reserved for tests that start a real `nats-server`
and is skipped by default.

Two testing lessons worth internalizing before you write tests here:

- **Concurrency tests must force real interleaving.** An instant mock
  makes `asyncio.gather` run tasks sequentially in practice, so a test
  can pass even with the race condition it's supposed to catch fully
  restored. Use `asyncio.Barrier` to force two coroutines to a contested
  point at the same time.
- **Demand end-to-end-through-the-real-path tests.** A correct predicate
  behind dead activating wiring is Arc's most recurring failure mode — a
  feature whose logic is right but sits behind a switch nobody actually
  flips at runtime. Before trusting a new feature is live, trace it
  through its real entry point (CLI command, config flag, module
  registration), not just the function that implements it.

## Architecture Tests

`tests/architecture/` (repo root, 18 files) enforces the layering rules
above mechanically, not just by convention:

| Test | Enforces |
|---|---|
| `test_arc_home_single_resolver.py` | One resolver for every path under `~/.arc` |
| `test_no_arcagent_imports_arcgateway.py` | `arcagent` never imports `arcgateway` |
| `test_no_arcrun_imports_arcagent.py` | `arcrun` never imports `arcagent` |
| `test_no_arcprompt_imports_upward.py` | `arcprompt` never imports a package above it |
| `test_no_arcstore_arcteam_upward_imports.py` | `arcstore`/`arcteam` never import upward |
| `test_no_click_in_arccli.py` | `arccli` does not depend on `click` |
| `test_no_global_tool_name_mutation.py` | Tool registry names are not globally mutated |
| `test_no_unsigned_backends_at_federal.py` | Federal tier cannot load unsigned sandbox backends |
| `test_no_evaluations_layering_violations.py` | Evaluation code respects package layering |
| `test_backend_protocol_duck_typing.py` | Sandbox backends conform to the shared protocol |
| `test_blueprint_capabilities_can_run.py` | Blueprint-declared capabilities actually load |
| `test_blueprint_skills_are_valid.py` | Blueprint-declared skills validate |
| `test_deployment_closure.py` | The deployed dependency closure is complete |
| `test_module_bus_priority_assignments.py` | Module bus priority tiers are respected |
| `test_arccli_command_registry_minimal_surface.py` | CLI command registry stays minimal |
| `test_prompt_markdown_ships_in_wheels.py` | Prompt markdown files are packaged into wheels |
| `test_test_tree_module_names.py` | Test module naming avoids collisions |
| `test_workspace_install.py` | The workspace install path works end to end |

`arctrust` additionally has its own layering test enforcing that it
imports no other Arc package.

**If you strengthen or change a layering rule, update the corresponding
test in the same PR.** A silently passing architecture test after a real
structural change is false confidence, not a green light.

## Commit Messages

Real pattern from `git log` on this repository (not a style guide
someone wrote and nobody follows): a Conventional-Commits-style prefix —
`fix(scope): ...`, `feat(scope): ...`, `refactor: ...`, `test: ...`,
`docs: ...`, `merge: ...` — followed by a plain-English sentence
describing the change, not a terse imperative fragment. Work-in-progress
commits (`wip: ...`) land directly rather than being squashed before
push. Example from this repo's history:

```
fix(run): a message with blocks must not break the loop that carries it
feat(arcmemory): consolidate a merged procedure's steps, and stop losing wrapped answers
```

## Branching and PRs

Branch naming: `<type>/<description>` (e.g. `feat/quick-deploy`,
`fix/login-redirect-bug`). Never commit directly to `main`.

CI (`.github/workflows/ci.yml`) triggers on push and PR against `main`
and runs three jobs: lint (`ruff` + `mypy --strict`), test (3 OS × 3
Python versions), and security (`pip-audit` + SBOM refresh, blocking).
There is currently no PR template and no issue template in this
repository — open a PR with a plain description of what changed and why
until one exists.

## ADRs

Write an Architecture Decision Record when a choice is non-obvious enough
that a future contributor would otherwise re-litigate it — a scope cut, a
layering rule, a storage split, a security invariant.

New ADRs go at `.claude/architecture/decisions/ADR-NNN-<slug>.md` (this
directory is tracked in git, not ignored).

Do not work out the next free number by looking at that directory. ADRs sit
in three places: one file per decision there, a handful at `.claude/adrs/`
(ADR-017A through 017D), and others recorded inline inside the spec that
produced them under `### ADR-NNN` headings in `.claude/specs/*/SDD.md`. A
number with no file is usually still taken.

`.claude/architecture/decisions/README.md` is the index and the authority:
it lists every ADR, says which of the three places it lives in, and states
the next free number. Take the number from there, then add your ADR's row
and bump that number in the same PR. Never renumber an existing ADR to close
a gap — the references run through docs, specs, and source.

## Docs

A documentation site is built with MkDocs Material from `docs/` and
published via `.github/workflows/docs.yml`. If your change alters
behavior, update the affected page under `docs/` in the same PR. If the
package has a runnable tutorial under `walkthroughs/<package>/`, refresh
that too — a walkthrough that no longer runs is worse than none, because
it still looks authoritative.

`docs/reference/security.md` is the canonical security-model deep dive;
`docs/building/contributing.md` is a longer-form companion to this file
inside the docs site.

## Where to Look

| Path | What lives there |
|---|---|
| `pyproject.toml` | Workspace definition, ruff/mypy config, dependency groups |
| `Makefile` | Gate targets for `arcgateway`/`arcagent`/root `tests/` |
| `.github/workflows/ci.yml` | The actual CI pipeline |
| `scripts/check_loc_budgets.py` | LOC budget definitions |
| `scripts/coverage_report.py` | Per-package coverage thresholds |
| `tests/architecture/` | Repo-wide layering invariants |
| `.claude/architecture/decisions/` | ADRs |
| `docs/` | The documentation site source |
| `sbom/security-suppressions.txt` | Documented, accepted vulnerability exceptions |
| `packages/<pkg>/CLAUDE.md` | Per-package build standards |
