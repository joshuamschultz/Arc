# arcagent

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Agent nucleus: DID-required identity, capability loading, sessions, module bus, and orchestration. Wires `arcrun` for accountable autonomy — does **not** own LLM HTTP or the execution loop.

## Layer

**Agent nucleus.** Depends on `arcrun`, `arctrust`, `arcprompt` (+ optional `arcmemory` extra). Imported by `arccli`, `arcgateway`, and other surfaces that construct agents. It is headless and works without `arcgateway` or `arcui`.

Must **not** import `arcgateway` (`tests/architecture/test_no_arcagent_imports_arcgateway.py`). Core LOC budget **< 3,500** (ADR-004).

## Layout

```
src/arcagent/
  core/                 # Nucleus — keep lean
    agent.py            # ArcAgent orchestrator
    agent_lifecycle.py
    agent_dispatch.py
    module_bus.py
    tool_registry.py
    session_internal/   # ContextManager, SessionManager
    config.py / telemetry.py / …
  modules/              # Independent modules (tasks, workflows, memory, skills, scheduler, …)
  capabilities/
  builtins/             # Builtin tools + bundled skills
  brain/                # Brain Protocol, NullBrain, select_brain
  orchestration/
  tools/
  extension/
  skilladapt/
```

PyPI name: `arc-agent`. Import: `arcagent`.

## Entry points

Top-level `__init__` exports errors mainly. Real entry: `arcagent.core.agent.ArcAgent`, config types, `arcagent.brain` (`Brain` / `NullBrain` / `select_brain`).

## Package rules

- **Concern split:** no LLM-call logic, no loop reimplementation — use `import arcrun` and invoke its public facade. ArcAgent never imports `arcllm` directly.
- Cross-package consumers use `import arcagent`; public seams belong on the root facade rather than in deep imports.
- **ADR-029:** agent state (memory, sessions, `context.md`, identity, audit chain) → **direct workspace I/O**. Never via LLM tools `write`/`bash`/`edit`. `working_dir` moves the tool root; the fence stays `workspace + allowed_paths`.
- Memory via structural `Brain` Protocol — no hard dependency on `arcmemory` (optional extra; default `NullBrain`).
- Modules stay independent; no global agent state on the module bus.
- Workflows module: runtime configure / runner inject ordering matters (gateway may publish runner before agent `configure`).

## Tests

`packages/arcagent/tests/` — `unit/`, `integration/`, `security/`, `architecture/`, `performance/` (largest suite in the repo).

## Working here

Prefer new behavior in `modules/` or capabilities, not `core/`. Capability loader / scan roots / precedence are load-bearing (SPEC-021). Read before expanding the nucleus.
