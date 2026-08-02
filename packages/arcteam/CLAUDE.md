# arcteam

> **Build standards:** repo root [`CLAUDE.md`](../../CLAUDE.md). This file is package-local — goal, layout, seams, and rules for fast correct work here.

## Goal

Multi-agent coordination: entity registry, channels/DMs, operator-signed audit, pluggable storage — plus the **ArcFlow** workflow engine (SPEC-061). Orchestrates agents; does **not** replace agent internals, LLM calls, or the ReAct loop.

## Layer

**Coordination / workflows.** Depends on `arcstore`, `arctrust` (NATS, pydantic). **Not** `arcagent`. Legal edge: `arcteam → arcstore` (tasks/runs substrate). No upward imports of `arcagent` / `arcui` / `arccli` / `arcrun` / `arcgateway`.

Imported by `arcgateway`, `arccli`, `arcui`; arcagent bootstraps via hooks without owning the engine.

## Layout

```
src/arcteam/
  team.py / messenger.py / registry.py / audit.py / storage.py / files.py / crypto.py
  backends/           # NATS, …
  memory/             # Team memory service
  workflow/           # ArcFlow (SPEC-061)
    models.py         # WorkflowDefinition, node kinds
    validator.py      # GraphValidator
    predicates.py     # Whitelisted predicate AST — no eval
    resolver.py       # Typed value binding (not string interpolation)
    runner.py         # Deterministic frontier runner (+ budget/state helpers)
    control_plane.py  # Shared create/edit/archive/run/cancel for all surfaces
    store.py / stores.py / serialize.py / identity.py / narrator.py / …
```

## Owns vs does not own

| Concern | ArcTeam | Not ArcTeam |
|---------|---------|-------------|
| Team formation / roster / channels | Yes | Individual agent lifecycle |
| Inter-agent messaging (wake + narration) | Yes | Carrying work in message bodies (handoffs = task rows) |
| Workflow definition + deterministic runner | Yes | LLM sequencing / orchestrator agent |
| Task/run substrate | Uses `arcstore` | Reimplementing a third DAG engine |
| Signing workflows | Definition store + operator pin | Agent self-signing as “verified” |

## Package rules

- Definition vs execution separated; **parsing / validation never confers signed status**.
- Workflow runner is deterministic code — models never sequence the graph.
- Handoff = task-row write naming the next owner; messaging is wake-signal + narration only (REQ-251).
- RunnerHost lifecycle lives in **`arcgateway`**, not arcui — execution must not require the dashboard (REQ-230).

## Tests

`packages/arcteam/tests/` — `unit/` (+ `unit/workflow/`), `integration/` (live runner, NATS, store conformance), `e2e/`.

## Working here

Extend `workflow/control_plane.py` for shared operations so CLI / agent tools / dashboard stay one code path. Keep predicates fail-closed (no calls, no attribute access, no env reads).
