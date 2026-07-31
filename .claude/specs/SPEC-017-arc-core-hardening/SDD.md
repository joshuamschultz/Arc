# SDD: Arc Core Hardening

**Spec:** SPEC-017
**Type:** integration
**Status:** pending-approval
**Coding Identity:** principled-coder (Simplicity → Modularity → Security → Scalability)

Every design choice is filtered through the four pillars in order. Module boundaries are hard. Where a choice conflicts with a pillar, the rationale is stated explicitly.

---

## 1. Module Boundaries (Non-Negotiable)

Per `CLAUDE.md`:

```
arcllm   → LLM provider calls ONLY. No agent state, no loop.
arcrun   → Loop execution, strategy, tool dispatch. No LLM knowledge, no agent composition.
arcagent → Agent composition: tools, skills, extensions, memory, policy. No provider calls, no loop internals.
arccli   → User surface only. No business logic.
```

This spec touches all four; each change stays within its owning module. The dependency direction is:

```
arccli → arcagent → arcrun → arcllm
```

No reverse dependencies. No skipping layers. `arcagent` wires the other three together via explicit constructor injection.

## 2. Change Map (Files)

### 2.1 New Files

| File | Package | Pillar | Purpose |
|------|---------|--------|---------|
| `core/tool_policy.py` | arcagent | Security | Policy pipeline types, layer protocol, pipeline class |
| `modules/proactive/engine.py` | arcagent | Modularity | Unified ProactiveEngine (replaces pulse + scheduler) |
| `modules/proactive/MODULE.yaml` | arcagent | Modularity | Module metadata for convention loader |
| `modules/proactive/__init__.py` | arcagent | Modularity | Module entry point |
| `modules/proactive/circuit_breaker.py` | arcagent | Security | Resilience4j-style state machine |
| `modules/proactive/leader.py` | arcagent | Scalability | Leader election (K8s Lease / Redis lock) |
| `tools/skill_tools.py` | arcagent | Modularity | `create_skill`, `improve_skill` |
| `tools/tool_tools.py` | arcagent | Modularity | `create_tool`, `list_artifacts`, `reload_artifacts` |
| `tools/extension_tools.py` | arcagent | Modularity | `create_extension` |
| `tools/schedule_tools.py` | arcagent | Modularity | `create/list/pause/resume/delete_schedule` |
| `tools/completion.py` | arcagent | Simplicity | `task_complete` tool shim (wraps arcrun builtin) |
| `tools/_decorator.py` | arcagent | Simplicity | `@tool` decorator (Pydantic schema inference) |
| `tools/_dynamic_loader.py` | arcagent | Security | Dynamic loader: AST validation + restricted builtins + egress proxy |
| `tools/_egress.py` | arcagent | Security | `ToolContext.http` proxy with per-tool allowlist |
| `builtins/task_complete.py` | arcrun | Simplicity | Runtime-side `task_complete` builtin |

### 2.2 Modified Files

| File | Package | Change | Pillar |
|------|---------|--------|--------|
| `core/agent.py` | arcagent | Wire LLM bridge, register new tools, `model.close()` in shutdown | Simplicity |
| `core/tool_registry.py` | arcagent | Replace `_check_policy()` with pipeline; move to execution-time; add classification field | Security |
| `utils/__init__.py` | arcagent | Add `on_event` parameter to `load_eval_model()` | Modularity |
| `modules/ui_reporter/MODULE.yaml` | arcagent | NEW FILE (enables convention loading) | Modularity |
| `modules/messaging/tools.py` | arcagent | Fix `byte_pos` in `svc.ack()` | Simplicity |
| `modules/pulse/**` | arcagent | DELETE — replaced by `proactive/` | Modularity |
| `modules/scheduler/**` | arcagent | DELETE — replaced by `proactive/` | Modularity |
| `arccli/agent.py` | arccli | Fix `/sandbox` and `/strategy` REPL; add schedule/tool/skill/policy CLI | Simplicity |
| `arcrun/strategies/react.py` | arcrun | Parallel dispatch with semaphore + read-write classification | Scalability |
| `arcrun/loop.py` | arcrun | `task_complete` handling; `max_turns` / `max_cost` enforcement | Security |
| Various configs | All | Move hardcoded constants to package TOML | Modularity |

## 3. Architecture

### 3.1 Tool Policy Pipeline

**Location:** `packages/arcagent/src/arcagent/core/tool_policy.py`

**Design intent:** Simplicity first — one class, one responsibility per layer. Layers are independent, composed at startup. No hidden state between calls.

```
┌─────────────────────────────────────────────────────────┐
│                   ToolPolicyPipeline                     │
│                                                          │
│   evaluate(call: ToolCall, ctx: PolicyContext) -> Decision
│                                                          │
│   for layer in self.layers:               (ordered)     │
│     try:                                                 │
│       decision = await layer.evaluate(call, ctx)         │
│     except Exception as e:                               │
│       return Decision.deny("layer_error", layer, e)     │
│     if decision.is_deny():                               │
│       return decision          # ← FIRST-DENY-WINS       │
│   return Decision.allow()                                │
└─────────────────────────────────────────────────────────┘
             │
             ├─ GlobalLayer       (always)    - tenant rules, classification
             ├─ ProviderLayer     (always)    - LLM provider budget, rate limits
             ├─ AgentLayer        (always)    - per-agent allowlists
             ├─ TeamLayer         (fed + ent) - team-scoped delegation rules
             └─ SandboxLayer      (fed)       - dynamic tool runtime constraints
```

**Types (all frozen Pydantic models):**

```python
class Decision(BaseModel):
    outcome: Literal["allow", "deny", "error"]
    layer: str | None               # None for allow
    rule_id: str | None
    reason: str | None              # human-readable, answers: layer + rule + inputs
    input_hash: str                 # for cache validation
    evaluated_at_us: int            # monotonic microseconds
    
class ToolCall(BaseModel):
    tool_name: str
    arguments: dict[str, Any]
    agent_did: str
    session_id: str
    classification: str             # unclassified | FOUO | SECRET | ...
    parent_call_id: str | None

class PolicyContext(BaseModel):
    tier: Literal["federal", "enterprise", "personal"]
    policy_version: str
    bundle_age_seconds: float
```

**Layer protocol:**

```python
class PolicyLayer(Protocol):
    name: str
    async def evaluate(self, call: ToolCall, ctx: PolicyContext) -> Decision: ...
```

**Performance design (R-013):**
- Layers hold pre-indexed rule tables keyed by `tool_name` (O(1) lookup)
- Decision cache: `functools.lru_cache` wrapper per layer, keyed on `(agent_did, tool_name, classification, input_hash)`, TTL 30s via monotonic clock check. Cache miss = full evaluation; hit = return cached decision (audit log skipped — cache hit is not an evaluation, record separately)
- All rules loaded at startup; no external calls during `evaluate()` (R-013 mandate)

**Tier selection:** Pipeline receives the layer list at construction. Factory `build_pipeline(tier)` returns the tier-correct stack. Pipeline itself has zero tier logic — tier lives in the factory.

**Dry-run (R-017):** Pipeline takes `shadow: bool` at construction. When `shadow=True`, it evaluates and logs but returns `Decision.allow()`. Used for safe policy hot-reload.

### 3.2 Parallel Tool Execution

**Location:** `packages/arcrun/src/arcrun/strategies/react.py`

**Design intent:** Modular boundary preserved — `arcrun` does not know about policy. Policy is enforced inside `tool_registry.call()` (in arcagent). arcrun's job is dispatch + classification inspection only.

**Flow:**

```
batch = [ToolCall, ToolCall, ...]
↓
classify_batch(batch) → (read_only_subset, state_modifying_subset)
↓
if state_modifying_subset is empty and no implicit deps:
    results = await gather_with_semaphore(batch, max=config.max_parallel_tools)
else:
    results = await dispatch_sequential(batch)
↓
emit audit events in dispatch order (monotonic seq)
```

**Classification source:** `Tool.classification: Literal["read_only", "state_modifying"]` declared at registration. `tool_registry` exposes `get_classification(tool_name)` — no duplicate classification store.

**Implicit dep detection (R-022):** Parameter-based heuristic — if tool call B has an argument whose value matches the value of any argument in tool call A in the same batch AND that argument for A is a file-path-like type, flag as dependent. O(n²) but n ≤ 10 in practice. If flagged, the entire batch goes sequential. No DAG planner (LLMCompiler) — too complex for the return.

**Semaphore (R-021):** `asyncio.Semaphore(max_parallel_tools)` default 10, FIPS default 4 (R-025). Value comes from `ArcRunConfig.tool_dispatch.max_parallel_tools`.

**Partial failure (R-023):** `asyncio.gather(*coros, return_exceptions=True)`. Results list contains either result or `Exception`. Per-tool errors logged as structured events. Loop continues.

**Audit ordering (R-024):** Before `gather()`, assign `seq = next(self._seq)` to each call. Emit `dispatch` event per call immediately (`seq, tool_id, dispatch_ts`). After gather, emit `complete` event per call with `(seq, tool_id, complete_ts, status, duration_us)`. Consumer reconstructs order via `seq`.

### 3.3 `task_complete` and Loop Limits

**Location:** `packages/arcrun/src/arcrun/builtins/task_complete.py`, `packages/arcrun/src/arcrun/loop.py`

**Design intent:** Simplicity. One explicit tool, one explicit check. No implicit "loop knows when done" heuristics.

```python
# builtins/task_complete.py
class TaskCompleteArgs(BaseModel):
    status: Literal["success", "partial", "failed"]
    summary: str
    artifacts: list[str] | None = None
    next_steps: list[str] | None = None
    error: str | None = None

@builtin(name="task_complete", classification="state_modifying")
async def task_complete(args: TaskCompleteArgs, ctx: LoopContext) -> None:
    ctx.request_termination(args)
```

**Loop integration (R-031):** When the loop processes a tool result batch, if any result came from `task_complete`, the loop terminates after processing the current turn. Emits `loop.completed` bus event with the `TaskCompleteArgs` payload.

**Limits (R-032):** `loop.py` holds turn counter and cost accumulator. Before each turn:

```python
if turns >= config.max_turns:
    await self.tool_registry.call("task_complete", {"status":"failed", "error":"max_turns"})
    return
if cost_usd >= config.max_cost_usd:
    await self.tool_registry.call("task_complete", {"status":"failed", "error":"max_cost"})
    return
```

Tier variation (R-032): Federal = hard cap. Enterprise = on breach, emit approval event; if approved within 30s, raise to 2x. Personal = auto-approve.

### 3.4 ProactiveEngine

**Location:** `packages/arcagent/src/arcagent/modules/proactive/`

**Design intent:** Modularity — one module replaces two. Simplicity — one timer, one priority queue.

```
┌────────────────────────────────────────────────────┐
│                 ProactiveEngine                    │
│                                                    │
│    min-heap: heapq of (next_run_mono, schedule)   │
│    single asyncio task: _tick_loop()               │
│                                                    │
│    tick():                                         │
│      now = time.monotonic()                        │
│      while heap[0].next_run <= now:                │
│        schedule = heappop(heap)                    │
│        if schedule.circuit_breaker.is_open():      │
│          emit("skipped_circuit_open"); reschedule  │
│          continue                                  │
│        if schedule.in_flight:                      │
│          emit("missed_concurrency"); reschedule    │
│          continue                                  │
│        dispatch(schedule)                          │
│        schedule.next_run = (                        │
│          schedule.last_actual_run + interval        │
│          - 0.010                                    │
│          + random_jitter(0, jitter_max)             │
│        )                                            │
│        heappush(heap, schedule)                     │
│      await asyncio.sleep(1.0)  # poll granularity  │
└────────────────────────────────────────────────────┘
```

**Heartbeat isolation (R-044):** Heartbeat uses a separate `HeartbeatContext` with only:
- System time
- Last agent activity timestamp
- Open circuit breakers count

NO session memory, NO conversation history, NO tool results. This context is passed to a minimal LLM call (cheap model, single-token decision boundary `{IDLE, NOT_IDLE}`). Result never touches `ArcAgent.context_manager`.

**Circuit breaker (R-045):** Per-schedule `CircuitBreaker` in `circuit_breaker.py`. States CLOSED / OPEN / HALF_OPEN. Sliding window tracks last 10 executions. Opens on 5 consecutive failures. `waitDuration = min(60 * 2^open_count, 1800)`. HALF_OPEN allows 1 probe. Success → CLOSED. Failure → OPEN (increment `open_count`).

**Leader election (R-048):** `leader.py` abstracts behind `LeaderElection(Protocol)` with two impls:
1. `KubernetesLeaseElection` — uses K8s coordination.k8s.io/v1/leases
2. `RedisLockElection` — fallback using Redis `SET NX PX` + heartbeat renewer

`ProactiveEngine.start()`:
```python
async def start(self):
    await self.leader.acquire_or_wait()
    try:
        await self._tick_loop()
    finally:
        await self.leader.release()
```

Personal tier: `LeaderElection` defaults to a no-op impl that always "wins" (single instance).

**Timezone (R-049):** Store `next_run_utc` + `interval_seconds`. Active-hours check converts to IANA TZ at evaluation time (cheap). Overnight windows handled by `end < start → now >= start OR now < end`. DST: `zoneinfo.ZoneInfo` handles automatically.

**Migration from pulse + scheduler (R-040):** Clean break. A one-time migration script (`arc agent schedule migrate`) converts any existing persisted state. Old modules deleted in same PR. Per CLAUDE.md "No backwards-compatibility hacks."

### 3.5 Dynamic Tool Loader

**Location:** `packages/arcagent/src/arcagent/tools/_dynamic_loader.py`

**Design intent:** Security is the dominant pillar here. Defense in depth.

```
┌────────────────────────────────────────────────────┐
│          DynamicToolLoader.load(source, name)      │
│                                                    │
│   1. _validate_source_encoding(source)             │
│      └─ reject non-UTF-8 coding declarations       │
│   2. _ast_validate(source)                         │
│      └─ walk AST, reject blocked patterns          │
│      └─ (R-053: 9 categories)                      │
│   3. module = _compile_in_sandbox(source)          │
│      └─ importlib.util.spec_from_file_location     │
│      └─ module_name = f"_agent_tools.{name}_{h}"   │
│      └─ __builtins__ = RESTRICTED_BUILTINS (R-054) │
│      └─ ToolContext injected with http proxy       │
│   4. tool = module.get_tool()                      │
│   5. pydantic_validate(tool.signature)             │
│   6. register(tool, classification, namespace)     │
│   7. emit audit event (M-4, M-7)                   │
└────────────────────────────────────────────────────┘
```

**AST validation rules (R-053):** Implemented as `AstValidator(ast.NodeVisitor)`. Reject patterns:

```
1. Import of: ctypes, subprocess, socket, os, sys, pickle, marshal, shelve
2. Attribute access ending in: gi_frame, f_back, f_builtins, f_globals, f_locals, __class__, __bases__, __subclasses__, __reduce__, __reduce_ex__
3. Calls to: compile, eval, exec, __import__, getattr (dynamic), setattr, delattr (against __builtins__)
4. Subscript of sys.modules
5. Source begins with #! shebang or coding declaration != utf-8
6. Use of string.Formatter.vformat with untrusted format string
7. Assignment to __builtins__, __loader__, __spec__
8. Class with __init_subclass__ that mutates base class
9. Starred unpacking of __builtins__
```

Every block emits a structured validation-failure event.

**Restricted builtins (R-054):** Literal dict, no wildcards. Anything not in the dict is `NameError` at execution time. No `builtins` module reachable via any path.

**Egress proxy (R-055):** `ToolContext.http: EgressProxy`. `EgressProxy.request(url, method, ...)`:
1. Parse URL, extract origin
2. Check against tool-specific allowlist (registered at tool-create time as part of `@tool` metadata)
3. If not in allowlist → `EgressDenied` exception
4. Forward through shared `httpx.AsyncClient` with per-tool rate limits
5. Emit `egress_request` audit event with `(tool, endpoint, method, status, bytes)`

In air-gapped federal deployments, the allowlist is empty and registration of endpoint-requiring tools fails at creation time.

**Module isolation (R-052):** `spec_from_file_location` with unique module name. Module is NOT inserted into `sys.modules`. Held in a `dict[str, ModuleType]` inside the loader. Reloading a tool creates a fresh module object (never `importlib.reload()`).

**Collision policy (R-057):** Config `tool_registry.on_collision`. Default `"warn"`. Enum-validated at config parse time.

### 3.6 Policy + Tool Registry Integration

**Location:** `packages/arcagent/src/arcagent/core/tool_registry.py`

Currently `tool_registry._check_policy()` runs at registration. This moves to execution.

```python
class ToolRegistry:
    def __init__(self, pipeline: ToolPolicyPipeline, ...):
        self._pipeline = pipeline
        self._tools: dict[str, RegisteredTool] = {}
        self._classifications: dict[str, Classification] = {}

    async def call(self, name: str, args: dict, agent_ctx: AgentContext) -> Any:
        call = ToolCall(
            tool_name=name,
            arguments=args,
            agent_did=agent_ctx.did,
            session_id=agent_ctx.session_id,
            classification=agent_ctx.classification,
            parent_call_id=agent_ctx.current_call_id,
        )
        decision = await self._pipeline.evaluate(call, agent_ctx.policy_context())
        if decision.outcome != "allow":
            raise PolicyDenied(decision)
        # dispatch the actual tool
        return await self._tools[name].invoke(args, agent_ctx)
```

This is the ONLY path. There is no second `call_without_policy()` method. No `skip_policy=True` flag. No sudo mode. All tool calls flow through `evaluate` (R-011).

Registration is cheap and unchanged — no evaluation at registration time.

### 3.7 LLM Bridge Wiring (R-001)

**Location:** `packages/arcagent/src/arcagent/utils/__init__.py` and `core/agent.py`

Current state: `create_arcllm_bridge()` produces an `on_event` callback but `load_model()` never receives it.

Change:

```python
# utils/__init__.py
async def load_eval_model(
    ...,
    on_event: Callable[[Event], Awaitable[None]] | None = None,
) -> EvalModel:
    model = await load_model(..., on_event=on_event)
    return EvalModel(model)
```

```python
# core/agent.py
class ArcAgent:
    async def __ainit_async__(self):
        bridge = create_arcllm_bridge(self.module_bus)
        self.model = await load_eval_model(
            config=self.llm_config,
            on_event=bridge.on_event,       # ← wired
        )
        ...
    async def shutdown(self):
        await self.model.close()           # ← added (R-004)
        ...
```

This is a boundary-respecting change: `arcllm` exposes `on_event` in its public interface; `arcagent` provides the callback. No leaking internal types across the boundary.

### 3.8 REPL Fixes (R-003)

**Location:** `packages/arccli/src/arccli/agent.py`

`/sandbox` currently prints help. Make it set `agent.policy_context.sandbox_mode = True/False` and acknowledge.
`/strategy` currently prints help. Make it swap the active strategy via `agent.set_strategy(name)`.

Both updates are audit-logged (admin action via REPL).

### 3.9 Config Migration (R-006)

Move 4 hardcoded constants to TOML:

| Constant | Current location | New location |
|----------|-----------------|--------------|
| `_CHECK_CIRCUIT_BREAKER_THRESHOLD` | `modules/scheduler/` | `proactive.toml:[circuit_breaker].check_threshold` |
| `_MAX_STEERING_MESSAGE_LEN` | `modules/steering/` | `steering.toml:[limits].max_message_len` |
| WebSocket URL | `modules/ui_reporter/` | `ui_reporter.toml:[ui].ws_url` |
| OTEL endpoint | `core/telemetry.py` | `core.toml:[telemetry].otel_endpoint` |

Per-package TOML (Decision 20): each package owns its config. No cross-cutting config file. Tier is an independent key per-package.

## 4. Data Flow Diagrams

### 4.1 Tool Call End-to-End

```
Agent (React strategy)
   │ batch of ToolCall
   ▼
arcrun.strategies.react
   │ classify_batch → (read_only, state_modifying)
   │ if all read_only: gather_with_semaphore
   ▼
arcrun.executor  → emit dispatch audit events (seq, ts)
   │ 
   ▼  (for each tool)
arcagent.core.tool_registry.call()
   │ build ToolCall
   ▼
arcagent.core.tool_policy.evaluate()
   │ Global → Provider → Agent → (Team) → (Sandbox)
   │ first DENY → raise PolicyDenied
   ▼
tool.invoke() — actual tool work
   │ if dynamic: ToolContext.http (egress proxy)
   ▼
Result or Exception  → emit complete audit event
   │
   ▼
arcrun returns to strategy  → next turn
```

### 4.2 ProactiveEngine Tick

```
ProactiveEngine._tick_loop (leader only)
   │ now = time.monotonic()
   │ peek heap top
   │
   ├─ top.next_run > now? → sleep(diff or poll_interval)
   │
   └─ top.next_run ≤ now? → 
         pop schedule
         │ circuit_breaker.is_open()? → emit skipped, reschedule
         │ in_flight? → emit missed, reschedule
         │
         └─ dispatch(schedule)
               │
               ├─ heartbeat type? → HeartbeatContext side-channel
               │                     (NOT main agent context)
               │
               └─ cron type? → invoke agent.handle_scheduled(schedule)
                                
         last_actual_run = time.monotonic()
         next_run = last_actual_run + interval - 0.010 + jitter
         heappush
```

### 4.3 Dynamic Tool Creation

```
Agent calls create_tool(name, source, allowlist=[endpoints])
   │
   ▼
DynamicToolLoader.load(source, name)
   │
   ├─ _validate_source_encoding → UTF-8 only
   ├─ _ast_validate             → 9-category rejection
   ├─ _compile_in_sandbox       → restricted builtins
   ├─ pydantic_validate         → signature contract
   ├─ classify                  → read_only vs state_modifying
   ├─ register                  → namespace: agent.{session}.{name}
   └─ audit event (M-4)
   ▼
Tool now callable via tool_registry.call()
   │ Policy pipeline evaluates every call (R-010)
   │ Egress proxy on every network call (R-055)
   ▼
All subsequent uses audit-logged
```

## 5. Security Model

### 5.1 Threat Surfaces Addressed (CLAUDE.md)

| OWASP Code | Threat | This Spec's Response |
|------------|--------|---------------------|
| LLM01 Prompt Injection | — | (existing in SPEC-013; no change here) |
| LLM02 Info Disclosure | Classification in `ToolCall`; egress proxy blocks unlisted endpoints |
| LLM06 Excessive Agency | Policy pipeline (5 layers); `task_complete` + turn/cost caps |
| LLM07 Prompt Leakage | Heartbeat isolation (R-044); no secrets pass through policy context |
| LLM10 Unbounded Consumption | `max_turns`, `max_cost_usd`, tool `Semaphore` |
| ASI01 Goal Hijack | Tool allowlists; self-mod audit-logged; federal denies dynamic code |
| ASI02 Tool Misuse | Parameter validation at dispatch; classification enforced |
| ASI03 Identity Abuse | `agent_did` on every ToolCall; no privilege inheritance without explicit grant |
| ASI04 Supply Chain (modules) | MODULE.yaml signatures (existing); convention loader verifies |
| ASI05 RCE via Generated Code | AST + restricted builtins + blocked attrs + egress proxy + 5-layer policy |
| ASI06 Memory Poisoning | Heartbeat side-channel; no cross-context write |
| ASI07 Inter-Agent Comms | NATS mTLS (existing); unchanged |
| ASI08 Cascading Failures | Circuit breakers per schedule; `Semaphore` caps concurrency |
| ASI10 Rogue Agents | Per-evaluation audit with `agent_did` + `rule_id`; denial rate metrics |

### 5.2 Non-Compositional Safety

arXiv:2603.15973 — two individually-safe tools can compose into a forbidden capability (read_file + http_request = exfiltration).

**Mitigation:**
1. Every tool declares `capability_tags: list[str]` (`file_read`, `file_write`, `network_egress`, `subprocess`, `state_mutation`, ...)
2. `GlobalLayer` holds a `forbidden_compositions` set at startup (e.g., `{file_read, network_egress}` if the agent lacks exfil authorization)
3. At dispatch time, the union of capabilities for the batch is computed and checked against the forbidden set
4. This is a deployment-time declaration, not a runtime heuristic — compliance-verifiable

### 5.3 Audit Trail Requirements

Per CLAUDE.md "the audit trail must be sufficient to rebuild state":

Every new event includes:
- `event_id` (ULID, monotonic)
- `actor_did`
- `action` (e.g., `policy.evaluate`, `tool.call`, `task_complete`, `schedule.tick`, `dynamic_tool.create`)
- `target` (tool name, schedule id, etc.)
- `timestamp_wall` + `timestamp_monotonic`
- `outcome` (`allow`/`deny`/`success`/`failed`/`skipped`)
- `content_hash` (for self-mod events) or `input_hash` (for policy events)
- `session_id`
- `trace_id` (OTel correlation)

Events are hash-chained (SHA-256 of previous event id + current payload) — existing telemetry pattern extended. Tamper-evident.

## 6. Observability

### 6.1 OTel Spans

- `tool.dispatch` (span, per tool call) — children: `policy.evaluate`, `tool.invoke`, optional `egress.request`
- `schedule.tick` (span, per proactive tick) — children: `schedule.dispatch`, `circuit_breaker.state_change`
- `loop.turn` (span, per agent turn) — children: `llm.call`, `tool.dispatch[]`, `task_complete?`

### 6.2 Metrics (Prometheus-compatible)

Per R-061:
- `arc_policy_decisions_total{layer,outcome}` — counter
- `arc_policy_evaluation_duration_us{layer}` — histogram
- `arc_policy_cache_hits_total{layer}` / `arc_policy_cache_misses_total{layer}`
- `arc_policy_exceptions_total{layer}` — MUST be zero in healthy state
- `arc_schedule_circuit_breaker_state{schedule_id,state}` — gauge
- `arc_heartbeat_ticks_total{outcome}` — counter
- `arc_heartbeat_silent_suppressions_total` — counter
- `arc_tool_dispatch_parallelism` — histogram of batch sizes run in parallel
- `arc_dynamic_tool_creations_total{tier,outcome}` — counter

### 6.3 Structured Logs

Every audit event is a JSON log line. Log schema version: `arc.audit.v1`. Fields: see §5.3.

## 7. Module Budget (ADR-004)

CLAUDE.md: core < 3,500 LOC. Current core: **4,229 LOC**. Adding `tool_policy.py` (~400 LOC est.) pushes well beyond.

**Decision (ADR-004):** Raise core ceiling to **5,000 LOC** with the following justification:
- Policy pipeline is genuinely core (every tool call touches it); extracting to a module creates a tighter cycle
- Budget is an alarm, not a hard boundary — the purpose is "hard to break, robust, no confusion," and a 500-LOC policy file with tight single-responsibility classes serves that purpose
- Trade: if post-implementation core exceeds 5,000 LOC, we extract `context_manager.py` (269 LOC) and `session_manager.py` (310 LOC) to a new `session/` module. Design preserves boundary.

Full ADR drafted during `/review SPEC-017`.

## 8. Rollout / Migration

### 8.1 Order of Phases (see PLAN.md)

1. Bug fixes (R-001 … R-006) — independent, low risk, validates toolchain
2. Tool policy pipeline (R-010 … R-018) — foundation for everything else
3. Read-write classification + parallel exec (R-020 … R-025)
4. `task_complete` + limits (R-030 … R-032)
5. ProactiveEngine (R-040 … R-049)
6. Dynamic tool loader + self-mod tools (R-050 … R-058)
7. Observability (R-060, R-061)
8. Adversarial test suite + CLI mirror (R-070 … R-080)

Each phase is independently shippable. Phases 2–6 each gate on phase 1 being clean.

### 8.2 Pulse/Scheduler Removal

Per CLAUDE.md "No backwards-compatibility hacks": pulse + scheduler DELETED in same PR that introduces `proactive/`. A one-time migration tool exists (`arc agent schedule migrate`) for persisted schedules. Changelog has a single-line migration note. No shims.

## 9. Alternatives Considered & Rejected

| Alternative | Rejected Because |
|-------------|------------------|
| TaskGroup for parallel tools | Fail-fast wrong for tool batches; Python 3.11 deadlock edge cases |
| Full DAG dependency planner (LLMCompiler) | Complexity not justified for n ≤ 10 tools/batch; heuristic is 95% correct |
| First-ALLOW-wins policy | Security anti-pattern (catastrophically wrong); rejected by every production system |
| RestrictedPython for sandbox | 3 CVEs in 2 years; bypass-prone; chose AST + restricted builtins + egress proxy layered |
| `importlib.reload()` for hot-reload | Unsafe for concurrent calls; chose fresh module object pattern |
| `tempfile.TemporaryDirectory` for dynamic tool isolation | Subprocess overhead; not needed for Python code, only if we supported binary extensions |
| Separate heartbeat process | Operational complexity; chose side-channel context in same process |
| Distributed lock per schedule (multi-instance) | 10x the failure surface of leader election; chose leader election |
| Keep pulse + scheduler alongside proactive | Violates modularity pillar + "no backwards-compat hacks"; chose delete |

## 10. Traceability

| Requirement | Design Section | File(s) |
|-------------|----------------|---------|
| R-001 LLM bridge | §3.7 | `utils/__init__.py`, `core/agent.py` |
| R-002 ui_reporter MODULE.yaml | §2.1 | `modules/ui_reporter/MODULE.yaml` |
| R-003 REPL fixes | §3.8 | `arccli/agent.py` |
| R-004 httpx shutdown | §3.7 | `core/agent.py` |
| R-005 byte_pos | §2.2 | `modules/messaging/tools.py` |
| R-006 config constants | §3.9 | package TOMLs |
| R-010–R-018 policy pipeline | §3.1, §3.6 | `core/tool_policy.py`, `core/tool_registry.py` |
| R-020–R-025 parallel exec | §3.2 | `arcrun/strategies/react.py`, `arcrun/executor.py` |
| R-030–R-032 task_complete/limits | §3.3 | `arcrun/builtins/task_complete.py`, `arcrun/loop.py` |
| R-040–R-049 ProactiveEngine | §3.4 | `modules/proactive/**` |
| R-050–R-058 self-mod | §3.5 | `tools/_dynamic_loader.py`, `tools/{skill,tool,extension}_tools.py` |
| R-060–R-061 observability | §6 | `core/telemetry.py`, new metric emitters |
| R-070–R-072 testing | See PLAN.md Phase 8 | `tests/security/adversarial/` |
| R-080 CLI mirror | §2.2 | `arccli/agent.py` |
| M-1 … M-10 federal mandates | §5.3, §3.1, §3.5, §6 | audit trail fields + tier variations |

## 11. Open Questions

1. **Policy bundle signing algorithm** — Ed25519 (matches identity/DID) vs Sigstore (supply chain ecosystem). Recommend Ed25519 for federal (FIPS) + extend in future spec. **→ Resolve in PLAN Phase 2**
2. **Circuit breaker persistence** — ephemeral (in-memory only) or persisted to disk? In-memory is simpler; persist only if multi-instance failover requires it. **→ Resolve in PLAN Phase 5**
3. **Heartbeat LLM provider** — which model? Cheap = Haiku. Air-gapped: local small model. Config-driven. **→ Resolve in PLAN Phase 5**

## 12. Review Checklist (for fast-track gate)

- [ ] Each requirement has a design section
- [ ] Each design section maps back to a pillar
- [ ] Module boundaries preserved (no logic bleed across arcllm/arcrun/arcagent/arccli)
- [ ] Every new operation emits an audit event
- [ ] Every new external path has deny-by-default posture
- [ ] No backwards-compat shims; clean deletes where declared
- [ ] Tier variation is config, not code forks
- [ ] Test strategy covers adversarial cases (Phase 8)
- [ ] LOC budget addressed (ADR-004)
