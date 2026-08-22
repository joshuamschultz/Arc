# arcmas

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Meta-package install vehicle: `pip install arcmas` pulls the full-stack story. **No product runtime API.**

## Layer

**Install meta.** Two direct deps — `arccmd` (the `arc` CLI) and `arcmemory`. The core stack arrives transitively via the CLI graph: `arcllm`, `arcrun`, `arc-agent`, `arcbundle`, `arcteam`, plus their `arctrust`/`arcstore`/`arcprompt`. The `arcgateway`, `arcui`, and `arcskill` surfaces are **not** pulled in — they are separate installs. Not imported by other packages for logic.

## Layout

```
src/arcmas/
  __init__.py     # __version__ + docs only
  py.typed
```

## Entry points

None beyond `__version__`.

## Package rules

- **Do not put product logic here.** Implement features in the real packages.
- Touch this package only when the "one install" dependency contract changes.
- Keep the README honest about what a single `pip install` actually resolves.

## Tests

None under this package (by design).

## Working here

Prefer changing `arcagent` / `arccli` / etc. Update `pyproject.toml` pins only for install-surface changes.