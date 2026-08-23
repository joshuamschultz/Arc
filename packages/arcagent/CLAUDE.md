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
    agent_lifecycle.py  agent_dispatch.py  agent_security.py
    module_bus.py  module_config.py  module_discovery.py
    config.py  config_loading.py       # 3-file split: arcagent/arcllm/arcrun sibling chains
    tool_registry.py  tool_policy.py  tool_policy_bridge.py
    session_internal/   # ContextManager, SessionManager
    model_manager.py  telemetry.py  vault/  …
  modules/              # Independent modules — tasks, workflows, connectors, memory, skills,
                        #   scheduler, proactive, planning, policy, messaging, browser, web,
                        #   voice, session, user_profile, workpad, progress, runcontrol, pulse
  capabilities/         # CapabilityLoader, registry, signing, inventory (SPEC-021)
  builtins/             # Builtin tools + bundled skills (the one TRUSTED scan root)
  brain/                # Brain Protocol, NullBrain, select_brain (pluggable memory seam)
  extension/            # ExtensionPoint + select_extension (four families)
  orchestration/        # spawn / spawn_many / RootTokenBudget (sub-runs)
  skilladapt/           # SkillAdapter seam (optional arcskill)
  tools/  utils/  context/
  connections.py  connection_catalog.py  keys.py  tiers.py  parts.py   # top-level facade
```

PyPI name: `arc-agent`. Import: `arcagent`.

## Entry points

Root `arcagent.__init__` is the facade (see its `__all__`): `ArcAgent`, `ArcAgentConfig` /
`SecurityConfig` / `load_config`, `KeyStore`, `CapabilityLoader` / `CapabilityRegistry`, the
`tool` decorator, `inspect_extensions`, `Connections` / `catalog`, and the error hierarchy.
`arcagent.brain` (`Brain` / `NullBrain` / `select_brain`) is the memory seam. `IdentityRequired`
lives in `core.errors` but is **not** on the root facade — import it from there if needed.

## Package rules

- **Concern split:** no LLM-call logic, no loop reimplementation — use `import arcrun` and invoke its public facade. ArcAgent never imports `arcllm` directly.
- Cross-package consumers use `import arcagent`; public seams belong on the root facade rather than in deep imports.
- **ADR-029:** agent state (memory, sessions, `context.md`, identity, audit chain) → **direct workspace I/O**. Never via LLM tools `write`/`bash`/`edit`. `working_dir` moves the tool root; the fence stays `workspace + allowed_paths`.
- Memory via structural `Brain` Protocol — no hard dependency on `arcmemory` (optional extra; default `NullBrain`).
- Modules stay independent; no global agent state on the module bus. Per-agent runtime state binds through a `ContextVar` (never a module `global` — an AST arch test fails CI on one).
- **ADR-033:** a module declares its dependencies through its `configure()` signature — no separate DI container.
- **ADR-034:** modules enable/disable/upgrade live (no restart); a module reaches a deployment as a signed bundle under `~/.arc/modules/`, never in the wheel.
- Workflows module: runtime configure / runner inject ordering matters (gateway may publish runner before agent `configure`).

## Tests

`packages/arcagent/tests/` — `unit/`, `integration/`, `security/`, `architecture/`, `performance/` (largest suite in the repo).

## Working here

Prefer new behavior in `modules/` or capabilities, not `core/`. Capability loader / scan roots / precedence are load-bearing (SPEC-021). Read before expanding the nucleus.
