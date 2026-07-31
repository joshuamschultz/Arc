# evaluations/ — benchmark harnesses

Each benchmark lives in its own self-contained directory under `evaluations/`.
A harness drives the **unmodified** Arc stack as a black box and measures what
it can do. Zero framework changes: every requirement is met by configuration,
by public API already in a package's `__all__`, or by harness-side code.

`evaluations/` is a peer of `packages/`, deliberately **not** a uv workspace
member. Nothing under `packages/` may import it —
`tests/architecture/test_no_evaluations_layering_violations.py` enforces that.

## Available harnesses

| Harness | Measures | Docs |
|---|---|---|
| [`longmemeval/`](longmemeval/README.md) | Long-term memory recall across many sessions | [README](longmemeval/README.md) |

## Layout

`ingest/` is the **shared, source-agnostic seam**: it turns any corpus into
agent input and knows nothing about which benchmark is consuming it. Each
harness then owns its whole tree, so adding a second one never disturbs the
first.

```
evaluations/
  ingest/            SHARED — how a source corpus becomes agent input
    types.py adapter.py chunker.py fidelity.py agent_factory.py
    driver.py consolidation.py lifecycle.py limits.py models.py
    tests/           the seam's own test suite

  <harness>/
    README.md        how to run this harness
    cli.py           the entry point: python -m evaluations.<harness>.cli
    config/          *.toml.example templates for the generated agent
    tests/           the harness's own test suite
    data/            gitignored — the corpus (manual download)
    runs/            gitignored — throwaway per-question workspaces
    results/         gitignored — ledgers and manifests
```

**The one-way rule inside `evaluations/`:** a harness may import `ingest/`;
`ingest/` may never import a harness. A seam that needs its consumer is not a
seam. `tests/architecture/test_no_evaluations_layering_violations.py` enforces
this by AST-scanning `ingest/` for forbidden imports.

Three directories per harness are generated and gitignored: `data/`, `runs/`
and `results/`. They hold **no source, ever** — git cannot re-include a file
beneath an ignored directory, so a `!` negation could never rescue it.

## Running a harness

Always from the repository root, always as a module:

```bash
uv run python -m evaluations.<harness>.cli --help
```

`python evaluations/<harness>/cli.py` does **not** work: Python puts the
script's own directory on `sys.path` instead of the repository root, so the
first import fails with `ModuleNotFoundError: No module named 'evaluations'`.
There is no console-script entry either — `evaluations/` is deliberately
outside the uv workspace.

## Testing

```bash
uv run pytest evaluations -q -rs                    # the seam and every harness
uv run pytest evaluations/ingest/tests -q           # the shared seam only
uv run pytest evaluations/<harness>/tests -q -rs    # one harness
```

The `-rs` matters. Tests that need a real corpus or a live judge key skip with
a reason naming exactly what is missing, so a skip is never mistaken for a pass.

## Adding a harness

1. Create `evaluations/<name>/` with the layout above.
2. Give it a `cli.py` exposing `python -m evaluations.<name>.cli`.
3. Add its row to the table above and a `README.md` in its directory.
4. Reuse `evaluations/ingest/` by writing a `SourceAdapter` for your corpus.
   If you find yourself wanting to edit `ingest/` for harness-specific reasons,
   the need belongs in your harness or in the `SourceAdapter` contract.
5. The gitignore patterns are already generic (`/evaluations/*/data/`,
   `/evaluations/*/runs/`, `/evaluations/*/results/`) — nothing to add.
6. Keep both one-way rules: the harness imports Arc and `ingest/`; neither Arc
   nor `ingest/` ever imports the harness.
