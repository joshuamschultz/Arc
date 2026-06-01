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
    executor.py           # compute args/result digest+size where the data lives (§11.1 C1)
    events.py             # EventBus._record_run_event: map tool.* → tool_event
packages/arcagent/src/arcagent/orchestration/
    spawn.py              # child actor_did/label + spawn_event
packages/arcllm/src/arcllm/modules/
    telemetry.py          # read agent_did/agent_label from contextvars (§11.2)
packages/arcui/src/arcui/
    observe.py            # tool_events / spawn_tree / llm_by_identity
    routes/…              # read routes
    web/…                 # timeline + lineage tree + identity cost view
```

---

## 11. Research Insights (from /deepen, 2026-05-31)

Five parallel research agents (3 codebase, 2 web) enriched this design. Solutions
archive: 4 security learnings, none directly applicable (they cover sandbox/AST
and scheduler hardening, not telemetry). Findings filtered Simplicity > Modularity
> Security > Scalability, each with the three mandatory callouts. **Two findings
(C1, C2) change the build; the rest confirm or refine it.**

### 11.0 CRITICAL — corrections to the Phase-1/2 design as drafted

| # | Issue (verified in code) | Fix | Pillar |
|---|---|---|---|
| C1 | **The result body is NOT available where SDD §4.1 assumed.** `arcrun/executor.py:34` emits `tool.start{name, arguments}` (args present) but `tool.end` (`executor.py:75-82`) emits only `result_length` + `duration_ms` — **no result body**. Computing `result_digest` in `events.py` would digest a length, not content. | Compute `args_digest`/`args_size` **and** `result_digest`/`result_size` in `executor.py` where `tc.arguments` and `result` both exist; emit them on the `tool.start`/`tool.end`/`tool.error` events. `events.py` only maps the already-computed fields to a `SpoolRecord`. Bodies ride the event (and into `extra`) **only** when `store_raw_bodies=true`. | S, Sec |
| C2 | **Child-LLM identity via `set_global_defaults` is concurrency-unsafe.** `telemetry.py:71-95` `_global_defaults` is a process-global dict; `spawn_many` (`spawn.py`) runs children via `asyncio.gather`, so concurrent children would overwrite each other's `agent_did` → cross-attributed `llm_call`s. | Use **`contextvars`** (task-local, asyncio-native, zero shared state). `telemetry.py` reads `agent_did`/`agent_label` from a `ContextVar` (falling back to config/global). `spawn.py` sets the contextvar around the child `run()` with capture-and-`try/finally`-reset. Industry-confirmed: LangSmith uses a `_PARENT_RUN_TREE` ContextVar for exactly this. | M, Sec, Sc |

### 11.1 arcrun tool spooling (FR-1/FR-2)

- **Schema: extend the shared `_OPERATIONAL_COLUMNS`, don't fork.** `backends/base.py:18` + `sqlite.py:27-66` already drive *all* operational tables from one column tuple via a template DDL. Add `tool_name`/`phase`/`args_digest`/`result_digest`/`args_size`/`result_size` once; `tool_events` and the new `spawn_events` reuse the same column set. One DDL, one ingest path, five kinds. **S.**
- **`store_raw_bodies` flows like `actor_did`:** add a `store_raw_bodies: bool` param to `EventBus.__init__` and `run()`/`run_async()`; the *caller* (arcagent/arccli) passes it from `ArcStoreConfig`. arcrun never reads config — boundary held (`events.py:17-18` imports only `arcstore.spool`/`records`). **M.**
- **Align field names to OTel GenAI semconv** (`gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.operation.name=execute_tool`, `error.type`) so the flat record is convention-compatible even though it is **not** OTLP-file format. OTel marks `gen_ai.tool.call.arguments`/`.result` as **Opt-In** ("may contain sensitive information") — independent validation of our metadata-only default. [OTel GenAI spans]. **Sec.**
- **Callouts:** *(ceiling)* tool events are the volume driver — 10–1000+/run vs ~10–50 `llm_call`s; a heavy code-gen loop is the worst case. *(air-gapped)* spool is local `os.write`, no network; OTel's own air-gap answer is the file exporter — we already are one. *(module)* arcrun stays `arcstore.spool`-only; digest helper is stdlib `hashlib`.

### 11.2 Child identity + lineage (FR-3)

- **Child `run_event`s: the fix is one argument.** `arcrun.run()` already accepts `actor_did` → `EventBus(spool_actor_did=…)` → `_record_run_event`. Both spawn call sites omit it today (`spawn.py:156` in `make_spawn_tool`, `spawn.py:495` in `spawn()`). Passing `actor_did=child_did` (already derived as `ChildIdentity.did`) completes child run-event separation. **M.**
- **Child `llm_call`s: contextvars (C2).** Ranked options — (D) `set_global_defaults` *rejected* (global race); (C) thread through `run()` *rejected* (arcrun→arcllm boundary breach); (A) wrapper TelemetryModule *viable* but adds a proxy layer; **(B) contextvars *chosen*** — task-local, zero overhead, asyncio-native, minimal coupling (telemetry identity stays an arcllm concern; arcagent only *sets* the context). **The asyncio trap:** `create_task` snapshots context at task-creation time — set the contextvar *before* spawning the child task, or capture+attach inside the child coroutine. **M, Sec, Sc.**
- **Lineage = flat edge + parent pointer, reconstruct on read.** Every production system examined (Langfuse `parent_id`, LangSmith `parent_run`/`dotted_order`, Phoenix `parent_span_id`, MLflow DAG) stores flat rows and rebuilds the tree at query time — never nested storage. Our `spawn_event{parent_did, child_did, role, depth}` is exactly that. This is OTel "Pattern B" (new trace per child + a link back) materialized as a durable edge table — the right fit for async children that must stay independently queryable. **S, M.**
- **`spawn.py` may import `arcstore.spool`** to emit the operational `spawn_event` (precedent: `arcrun/events.py:17`). `_emit_spawn_audit` (arctrust WORM) stays — compliance edge in the chain, operational edge in the spool. `parent_state.depth` is available at spawn time. **M.**
- **Callouts:** *(ceiling)* `max_depth=3` (`arcrun/state.py`) bounds trees to ~125 nodes (≤5 concurrent × 3 levels) — trivial to render; deep/wide fan-out is structurally capped. *(air-gapped)* all identity flow is in-process, no network. *(module)* spawn stays arcagent, arcrun source unchanged, telemetry identity stays arcllm.

### 11.3 Cost attribution + sampling

- **Store cost at the `llm_call` leaf, aggregate up on read — never write parent totals.** Universal industry standard (ClickHouse names pre-aggregated parent totals an anti-pattern). Double-counting is impossible by construction: orchestrator/parent records carry no token fields. `observe_stats.compute_stats()` already groups `llm_calls` by `agent_label`, so once children carry a distinct label, parent-vs-child separation is **free** — no new aggregation code. Parent-subtree total = sum over the lineage subtree at read. **S, M.**
- **`[arcstore].sample_rate` is defined but UNWIRED today** (`config.py:60`; zero refs in `spool.py`/`ingest.py`). Wire it at the **EventBus level** (caller passes effective rate, like `store_raw_bodies`) so only `tool_event` is thinned. **Never sample out errors, `run_event`, or `spawn_event`** — OTel tail-sampling's core rule (errors + lifecycle/parent spans are always kept; only routine spans are probabilistic). **Sc, Sec.**
- **Callout (ceiling, quantified):** at single-operator scale a full-window timeline read (merge `llm_calls`+`run_events`+`tool_events` by `ts` in Python) is cheap to ~10k rows/run; beyond that a SQL `UNION ALL … ORDER BY ts` view is the escape hatch (deferred — YAGNI until measured).

### 11.4 arcui surfaces (FR-4)

- **Timeline = query-per-table + merge by `ts` in Python** (no union today; matches the existing `Observe` one-table-per-call shape). Correlation key is `request_id == run_id` — already set on `run_event`; **set it on `tool_event` too** so a run's three streams join. **S.**
- **Read-on-demand React Query only.** Confirmed `server.py` keeps only `/ws/chat`; pages use `useQuery`/`useQueries` (`web/src/pages/arcllm.tsx`, `arcrun.tsx`). New routes must be synchronous read-response. `test_no_push_pipeline.py` forbids re-introducing any of `arcui.bridge/aggregator/event_buffer/subscription/transport*/routes.ws/dashboard_ws/agent_ws` or symbols `UIBridgeSink/RollingAggregator/EventBuffer/...` — new code must not import or name them. **M.**
- **Minimal component set:** a timeline list (reuse trace-table row logic + a `ToolEventRow` variant), a recursive `SpawnTree` (`<details>/<summary>`, bounded by depth≤3), and an identity breakdown reusing the existing agent-stats table. **S.**

### Source-of-truth precedents to clone (codebase)

| Build target | Clone from |
|---|---|
| `tool_event` spool record + EventBus mapping | `arcllm/modules/telemetry.py` `llm_call` recorder; `arcrun/events.py:165-181` `_record_run_event` |
| digest-at-source on tool I/O | `arcrun/executor.py:34,75-82` (where args/result exist) |
| child `actor_did` threading | `arcrun/loop.py` `run(actor_did=…)` → `EventBus(spool_actor_did=…)` |
| contextvar identity | LangSmith `_PARENT_RUN_TREE` pattern (external) |
| timeline merge / cost-by-label | `arcui/observe.py`, `arcui/observe_stats.py compute_stats()` |
