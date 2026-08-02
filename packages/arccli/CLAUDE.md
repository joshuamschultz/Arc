# arccli

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Unified `arc` CLI — single front door to the Arc stack (one-shot commands + interactive REPL). Terminal / entry layer only.

## Layer

**Surface.** Depends on `arcllm`, `arcrun`, `arc-agent`, `arcteam`. Soft-imports `arcui` / `arcgateway` / `arctui` for optional subcommands. Nothing in the nucleus should depend on this package.

PyPI name: **`arccmd`**. Import package: **`arccli`**. Console scripts: `arc` → `arccli.main:main`, `arc-agent-worker` → `arccli.agent_worker:main`.

## Layout

```
src/arccli/
  main.py / __main__.py / agent_worker.py
  blueprints*.py / formatting.py
  commands/
    registry.py       # COMMAND_REGISTRY / CommandDef — longest-prefix matching
    agent/            # agent create/build/chat/run/serve/…
    init.py / llm.py / run.py / ui.py / team.py / workflow.py
    skill*.py / gateway_connect.py / approve.py / trust.py / store.py / task.py / …
```

## Entry points

`arc` CLI via `main.py`. Command surface = `commands/registry.py`. Optional `import arctui.entry` registers `arc tui` without a hard dep cycle.

## Package rules

- Add commands via `CommandDef` registry — don't invent a second dispatcher.
- Keep optional surfaces (**soft-import**): missing `arcui`/`arctui`/`arcgateway` must not break core CLI.
- Prefer `--json` on data-oriented commands for scripting.
- Do not invert deps: CLI calls into packages; packages must not import CLI for core logic.

## Tests

`packages/arccli/tests/` — agent/blueprint/cli smoke, ui, workflow, trust, store, …

## Working here

New user-facing verb → register in `COMMAND_REGISTRY`, thin wrapper over package APIs (e.g. workflow → `arcteam` control plane). Keep business logic out of the CLI layer.
