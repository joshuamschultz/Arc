# SPEC-028 — ArcRun Tool / Code / Spawn Observability: Solution Design

> Extends the SPEC-026 data plane. Same two durable files, same pull-only read
> path, same producer import boundary. This spec adds **two spool kinds** and
> **one identity fix**, then teaches arcui to read them. Every choice is
> pillar-traced (S/M/Sec/Sc).

## 1. Architecture Overview

```
WRITE (producers append to the SPEC-026 spool — unchanged file, new kinds)
  arcllm           → llm_call    (existing; child calls now carry child identity)
  arcrun  EventBus → run_event   (existing lifecycle)
                   → tool_event  (NEW: tool.start/end/error, incl. code-exec)
  arcagent spawn   → spawn_event (NEW: parent→child lineage edge)

INGEST   arcstore StoreIngest  → SQLite (new tables tool_events, spawn_events)

READ     arcui Observe (pull)  → tool/code timeline, spawn tree, per-identity cost
```

No new transport, no new sink in `emit()`, no reversal of the import DAG.

## 2. Module Boundaries (unchanged contracts, extended)

| Module | Change | Still never |
|---|---|---|
| `arcstore.records` | add `tool_event`, `spawn_event` to `SpoolKind`; add their flat fields | depend on a producer |
| `arcstore.backends` | add `tool_events`, `spawn_events` tables + `table_for_kind` mapping | leak SQLite types into the Protocol |
| `arcrun.events` | spool `tool.*` events (new bridge from EventBus → spool) | learn about spawn/agents |
| `arcagent.orchestration.spawn` | pass `actor_did`+`agent_label` to child run/telemetry; emit `spawn_event` | move spawn into arcrun |
| `arcui.observe` | new read queries; UI surfaces | become a sink/subscriber |

**Import DAG (enforced):** `arctrust ← arcstore ← {arcllm, arcrun, arcagent} ←
arcui`. Unchanged.

## 3. arcstore — new spool kinds (FR-1, FR-3)

### 3.1 Record schema additions (`records.py`)

`SpoolKind = Literal["llm_call", "run_event", "agent_event", "tool_event", "spawn_event"]`

New flat fields on `SpoolRecord` (all optional, like the existing llm_call set):

```python
# tool_event
tool_name: str | None = None
phase: str | None = None            # start | end | error
args_digest: str | None = None      # sha256(canonical args)
args_size: int | None = None
result_digest: str | None = None
result_size: int | None = None
# spawn_event
parent_did: str | None = None
child_did: str | None = None
role: str | None = None
depth: int | None = None
```

- Reuse existing `outcome`, `latency_ms`, `request_id`, `actor_did`, `name`.
- **Bodies** (code, tool args, tool results, child prompt/output) ride `extra`
  and are populated **only** when `store_raw_bodies=true` — keeps the default
  metadata-only (NFR-2). The producer decides; the record just carries it.
- `record_id` derivation is unchanged (content-derived, idempotent ingest).
- **S, Sec.** Flat model, no nested envelope; secure default unchanged.

### 3.2 Backend tables (`backends/base.py`, `backends/sqlite.py`)

- `OPERATIONAL_TABLES += ("tool_events", "spawn_events")`; extend `_KIND_TABLE`.
- `SqliteBackend` schema gains the two tables with the same flat-column +
  `INSERT OR IGNORE` content-keyed idempotency the existing tables use.
- `FakeBackend` needs no change (schema-agnostic dict store) — it keeps proving
  no SQLite leak (SPEC-026 AC-3.4). **M.**

## 4. arcrun — spool tool events (FR-1, FR-2)

### 4.1 Where (`arcrun/events.py`)

arcrun already emits `tool.start`/`tool.end`/`tool.error` to its `EventBus`
(`executor.py`). The `EventBus` already has the spool side-channel
(`_record_run_event`, gated by `spool_actor_did`). **Extend that same method** to
also map `tool.*` events to a `tool_event` `SpoolRecord`:

```python
_TOOL_EVENT_TYPES = {"tool.start", "tool.end", "tool.error"}

# in _record_run_event(event):
if event.type in _RUN_EVENT_TYPES:   # existing
    ... run_event
elif event.type in _TOOL_EVENT_TYPES:
    _spool_record(_SpoolRecord(
        kind="tool_event",
        actor_did=self._spool_actor_did,
        request_id=self._run_id,
        tool_name=event.data.get("name"),
        phase=event.type.split(".", 1)[1],
        outcome="error" if event.type == "tool.error" else "ok",
        args_digest=_digest(event.data.get("arguments")),
        result_digest=_digest(event.data.get("result")),
        # bodies only under store_raw_bodies (carried via a bus-level flag)
    ))
```

- Same gate (`spool_actor_did is not None`), same fail-open `record()`. **Sec, Sc.**
- The `store_raw_bodies` decision is passed to the `EventBus` at construction
  (one bool), mirroring how arcllm's `TelemetryModule` already holds it. arcrun
  does not read config — the **caller** (arcagent / arccli) passes the flag, same
  as it already passes `actor_did`. **M.**

### 4.2 Code execution (FR-2)

`make_execute_tool`'s tool name is a known constant. The UI recognizes it; the
record is just a `tool_event` whose `arguments` body is the code and whose
`result` body is stdout/result. No special arcrun path — code-exec is a tool,
recorded like any tool, with digests always and bodies under the flag. **S.**

### 4.3 Sampling

`[arcstore].sample_rate` (SPEC-026) applies to `tool_event` (high-frequency).
`run_event` and `spawn_event` are never sampled out (low-volume, structural). The
sample decision is made by the caller passing an effective rate; the spool stays
dumb. **Sc.**

## 5. arcagent — spawn identity + lineage (FR-3)

### 5.1 The fix (`orchestration/spawn.py`)

Today `spawn()` calls:

```python
result = await run(model=model, tools=tools, system_prompt=..., task=prompt, max_turns=10)
```

`run()` already accepts `actor_did` (→ `EventBus(spool_actor_did=...)`). The fix:

```python
child_label = _child_label(parent_agent, child_did, role)   # e.g. "<parent>/child:<role>:<depth>"
child_model = _child_model_with_identity(model, child_did, child_label)  # telemetry under child id
result = await run(
    model=child_model, tools=tools, system_prompt=..., task=prompt,
    max_turns=10, actor_did=child_did,            # ← spools child run_events (F5)
)
```

- `_child_model_with_identity` configures the child's `TelemetryModule`
  `agent_did`/`agent_label` so the child's `llm_call`s carry the child identity
  (closes F4). This is an arcllm concern invoked from arcagent — it wraps/clones
  the telemetry config, it does not reach into arcrun. **M.**
- If the parent model can't be re-identified safely, fall back to the existing
  behavior **but** still tag the child run via `actor_did` (run_events separate
  even if llm_calls can't) — degrade, never break. **Sec.**

### 5.2 Lineage record

After deriving `child_did`, emit a `spawn_event` spool record
(`parent_did`, `child_did`, `role`, `depth`, `outcome`) alongside the existing
`_emit_spawn_audit` (arctrust). The audit chain keeps the compliance edge; the
spool carries the operational edge the UI reads. **Sec, M.**

## 6. arcui — read + render (FR-4)

### 6.1 Observe queries (`observe.py`)

- `tool_events(run_id=…, limit=…)` → ordered tool/code timeline for a run.
- `spawn_tree(root_did=… | run_id=…)` → parent→child edges from `spawn_events`,
  assembled into a tree (plain recursion over rows; no graph DB). **S.**
- `llm_by_identity(window=…)` → `llm_calls` grouped by `agent_label` (parent vs
  children) — extends the existing stats path.

### 6.2 Routes + frontend

- New read routes mirror the existing trace routes (pull, short-lived query,
  `agent_label` filter). No push, no polling machinery beyond the existing
  read-on-demand React Query (SPEC-026 4.10/4.15). **S, Sc.**
- UI: per-run timeline component (tool/code rows interleaved with llm rows by
  `ts`), a lineage tree component, and an identity breakdown in the cost view.

## 7. Data Flow — UC-2 "see a spawned agent's work"

```
parent run: model calls spawn_task("summarize X", role="researcher")
  arcagent.spawn():
     child_did  = derive(parent_did, depth)
     spool spawn_event{parent_did, child_did, role, depth}
     run(model=child-identified, actor_did=child_did):
        EventBus(spool_actor_did=child_did)
          → run_event turn.start   (child identity)
          → tool_event web.fetch    (child identity)
          → llm_call                (child identity, via child telemetry)
          → run_event loop.completed
  arcui:
     spawn_tree(parent run)  → parent → [researcher child]
     click child → tool_events(child run) + llm_by_identity row for child
```

The child's every action is durable and attributed. **S, Sec, Sc.**

## 8. Failure Modes & Mitigations

| Failure | Behavior | Pillar |
|---|---|---|
| Spool write fails on a tool event | logged, swallowed; tool call proceeds (AU-5) | Sec |
| `store_raw_bodies=false` | digests/sizes only; no code/args/output bodies | Sec |
| High-frequency tool loop | `sample_rate` thins `tool_event`; lifecycle/spawn kept | Sc |
| Child model can't be re-identified | run_events still tagged via `actor_did`; llm_calls degrade to parent id (logged) | Sec |
| Old store without new tables | `start()` creates them idempotently; backfill re-reads spool | S |

## 9. Test Strategy

- **Unit (70%)**: `tool_event`/`spawn_event` record validation; arcrun EventBus
  → spool mapping (start/end/error, metadata-only vs body); spawn passes
  child `actor_did`/label; digest helper.
- **Integration (20%)**: run-with-tool → spool → Observe query (UC-4); code-exec
  → record with code gated by flag (UC-1/5); spawn → child llm_calls separated +
  lineage edge (UC-2/3); arcui server restart loses no tool/spawn history.
- **Architecture**: import-graph (arcrun imports only `arcstore.spool`; spawn
  stays arcagent; arcui not a sink); secure-default test (no bodies when flag off).

## 10. Package Layout (touch list)

```
packages/arcstore/src/arcstore/
    records.py            # + tool_event/spawn_event kinds & fields
    backends/base.py      # + tables, _KIND_TABLE entries
    backends/sqlite.py    # + table schemas
packages/arcrun/src/arcrun/
    events.py             # EventBus._record_run_event: map tool.* → tool_event
packages/arcagent/src/arcagent/orchestration/
    spawn.py              # child actor_did/label + spawn_event
packages/arcui/src/arcui/
    observe.py            # tool_events / spawn_tree / llm_by_identity
    routes/…              # read routes
    web/…                 # timeline + lineage tree + identity cost view
```
