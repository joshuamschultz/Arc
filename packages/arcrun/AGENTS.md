# arcrun

> **Build standards:** repo root [`AGENTS.md`](../../AGENTS.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Async ReAct execution engine: think → act → observe with tool registry, sandbox, streaming, strategies, and hash-chained events. **No agent identity, skills, sessions, or memory.**

## Layer

**Loop.** Depends on `arcllm`, `arctrust`, `arcstore`, and `arcprompt`. It has no knowledge of concrete callers or higher layers.

Must not import any higher layer (`tests/test_layering.py`, architecture guards).

## Layout

```
src/arcrun/
  loop.py             # run / run_async / run_oneshot + RunHandle
  streams.py          # run_stream / collect / StreamEvent types
  model.py            # ArcLLM model facade (load_model, Model, Message, …) re-export
  _messages.py        # SystemPrompt alias, message helpers
  capabilities.py     # CapabilityProvider Protocol, StaticProvider (impls live in hosts)
  registry.py         # ToolRegistry
  executor.py         # Per-tool dispatch + JSON-schema param validation
  parallel_dispatch.py# BatchClassifier + dispatch_ready
  events.py           # EventBus, verify_chain, hash chain
  state.py            # RunState (per-run budgets, breakers, counters)
  types.py            # Tool, ToolContext, LoopResult, SandboxConfig
  sandbox.py
  checkpoint.py       # LoopCheckpoint (resume_from)
  prompts.py          # get_strategy_prompts
  strategies/         # react, code, dynamic, oneshot, plan_execute + selection
  dynamic/            # Dynamic strategy internals: host boundary, grammar, interpreter, journal, seal
  builtins/           # execute_python / run_shell / task_complete
  backends/           # local, docker, vm (firecracker), loader, policy
  context/            # Stock system-prompt / strategy markdown
```

## Entry points

`run` / `run_async` / `run_oneshot` / `run_stream`, `RunHandle`, `CapabilityProvider`/`StaticProvider`, `ToolRegistry`, `Tool`/`ToolContext`, `EventBus`, `Strategy` + `available_strategies` (react / code / dynamic / oneshot / plan_execute), sandbox helpers (`make_execute_tool`, `run_shell`), stream event types, `SystemPrompt`, the ArcLLM model facade (`load_model`, `Model`, …), `dynamic.ScriptHost`.

## Package rules

- Own the model-execution seam used by higher layers while keeping provider routing in ArcLLM.
- Consume ArcLLM with `import arcllm`; consumers likewise use only `import arcrun` and qualified root-facade names.
- `CapabilityProvider` is a Protocol; concrete providers belong in the host.
- Stock prompts under `arcrun/context/`; overlays via `arcprompt`.
- Sandbox / backend work stays defensive (path traversal, injection) — see `tests/security/` and integration docker/firecracker tests.

## Tests

`packages/arcrun/tests/` — unit (loop/strategy/sandbox), `integration/` (docker/firecracker/spawn), `security/`.

## Working here

Loop changes touch the hottest path in the stack — keep concerns pure. Prefer extending `Strategy` / registry / stream contracts over special-casing agent behavior.

`dynamic/grammar.py`'s whitelist is a security boundary, not a style choice — it is the only thing standing between a model-authored script and arbitrary code (LLM01/ASI05). Every effect a script can have is named on `dynamic/host.py`'s `ScriptHost` Protocol; widening the grammar (a new builtin, a new method, attribute access) means auditing what that name newly reaches, the same care as reviewing a sandbox escape.