# arccli

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Unified `arc` CLI — single front door to the Arc stack (one-shot commands + interactive REPL). Terminal / entry layer only.

## Layer

**Surface.** Depends on `arcllm`, `arcrun`, `arc-agent`, `arcbundle`, `arcteam` (plus `click` for the two delegated agent-module CLIs). Soft-imports `arcui` / `arcgateway` / `arctui` / `arcmemory` for optional subcommands. Nothing in the nucleus should depend on this package.

PyPI name: **`arccmd`**. Import package: **`arccli`**. Console scripts: `arc` → `arccli.main:main`, `arc-agent-worker` → `arccli.agent_worker:main`.

## Layout

```
src/arccli/
  main.py / __main__.py / agent_worker.py    # entry + REPL + federal subprocess worker
  blueprints.py / blueprints_materialize.py / formatting.py
  commands/
    registry.py            # COMMAND_REGISTRY / CommandDef — longest-prefix matching
    render.py / _shared.py / _serve.py / _capability_registry.py / _arcllm_surface.py
    agent/                 # create/build/chat/run/serve/status/tools/skills/config/memory/…
    init.py / llm.py / run.py / keys.py / identity.py / prompt.py     # setup + LLM + prompts
    skill.py / skill_evals.py / ext.py / connector.py / module.py     # tools & extensibility
    blueprint.py / workflow.py                                        # signed presets + ArcFlow
    team.py / task.py / ui.py / store.py / memory.py                  # team, tasks, observability
    approve.py / trust.py / stop.py / user.py / operator.py           # operator controls
    install.py / up.py / runtime.py                                   # install + bring-up + versions
    gateway_connect.py                                                # arc gateway connect-telegram
```

## Entry points

`arc` CLI via `main.py` (`_dispatch_oneshot` for `arc <cmd>`, `_run_repl` for bare `arc`). Command surface = `commands/registry.py` (`COMMAND_REGISTRY`, `resolve_command_and_args`). Multi-word names (`gateway pair approve`) resolve by longest-prefix. Optional `import arctui.entry` at the bottom of the registry appends `arc tui` without a hard dep cycle.

## Package rules

- Add commands via `CommandDef` in `COMMAND_REGISTRY` — don't invent a second dispatcher. Multi-word names are fine (longest-prefix match).
- Keep optional surfaces (**soft-import**): a missing `arcui`/`arctui`/`arcgateway`/`arcmemory` must not break the core CLI.
- Prefer `--json` on data-oriented commands for scripting.
- Do not invert deps: CLI calls into packages; packages must not import CLI for core logic.
- Secrets (bot tokens, API keys) go through hidden prompts to the env file (`0600`) — never config, logs, or an agent chat/LLM.

## Tests

`packages/arccli/tests/` — agent/blueprint/cli smoke, ui, workflow, trust, store, …

## Working here

New user-facing verb → register in `COMMAND_REGISTRY`, thin wrapper over package APIs (e.g. workflow → `arcteam` control plane). Keep business logic out of the CLI layer.