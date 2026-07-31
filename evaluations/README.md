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

## Layout convention

Every harness owns its whole tree, so adding a second one never disturbs the
first:

```
evaluations/
  <harness>/
    README.md        how to run this harness
    cli.py           the entry point: python -m evaluations.<harness>.cli
    ingest/          how a source corpus becomes agent input
    config/          *.toml.example templates for the generated agent
    tests/           the harness's own test suite
    data/            gitignored — the corpus (manual download)
    runs/            gitignored — throwaway per-question workspaces
    results/         gitignored — ledgers and manifests
```

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

## Testing a harness

```bash
uv run pytest evaluations/<harness>/tests -q -rs
```

The `-rs` matters. Tests that need a real corpus or a live judge key skip with
a reason naming exactly what is missing, so a skip is never mistaken for a pass.

## Adding a harness

1. Create `evaluations/<name>/` with the layout above.
2. Give it a `cli.py` exposing `python -m evaluations.<name>.cli`.
3. Add its row to the table above and a `README.md` in its directory.
4. The gitignore patterns are already generic (`/evaluations/*/data/`,
   `/evaluations/*/runs/`, `/evaluations/*/results/`) — nothing to add.
5. Keep the one-way rule: the harness imports Arc, Arc never imports the harness.
