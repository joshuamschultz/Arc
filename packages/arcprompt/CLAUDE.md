# arcprompt

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Editable, signed, inspectable system prompts: stock markdown in owning packages plus signed overlays, snapshotted once per run.

## Layer

**Prompt plane (leaf).** Depends only on `arctrust`. Enforced by `tests/architecture/test_no_arcprompt_imports_upward.py`. Consumed by `arcrun`, `arcagent`, `arcmemory`. Injected into packages that must stay leaf-clean (e.g. `arcskill` never imports arcprompt).

## Layout

```
src/arcprompt/
  catalog.py     # PromptCatalog
  document.py    # parse/render prompt documents
  resolver.py    # PromptResolver — overlay-over-stock, first-match-wins
  snapshot.py    # PromptSnapshot / snapshot() — freeze once per run
  verifier.py    # SignatureVerifier, TrustPosture
  errors.py
```

Stock prompts live in the **owning** package: `packages/<pkg>/src/<pkg>/context/<name>.md` (shipped in wheels — see `tests/architecture/test_prompt_markdown_ships_in_wheels.py`).

## Entry points

`PromptCatalog`, `PromptResolver`, `PromptSnapshot`/`snapshot`, `load_stock`/`load_stock_document`, `parse_prompt`/`render_prompt`, `SignatureVerifier`.

## Package rules

- Overlay-over-stock; first-match-wins; freeze with `snapshot()` at run start.
- Broken overlay **fails loud** — never silent fallback to stock.
- Version = `sha256` of file bytes. Overlays need `.arcsig`.
- Do not import consumers (`arcrun`, `arcagent`, …).

## Tests

`packages/arcprompt/tests/unit/` — catalog, document, resolver, snapshot, verifier.

## Working here

Add stock content in the package that owns the prompt (`arcrun/context/`, etc.). Wire resolvers by injection at the consumer; keep this package a pure leaf.
