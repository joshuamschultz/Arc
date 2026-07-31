# SDD: ArcUI Zero-Config Launch

**Spec**: SPEC-019 | **Date**: 2026-04-26

## Architecture

```
            ┌──────────────────────────────────────────────────────────┐
            │  ArcUI Server  ──  AgentRegistry  ──  RollingAggregator  │
            │  agent_ws  ──  SubscriptionManager  ──  EventBuffer       │
            │  UIReporterModule (in arcagent)                            │
            │  AuthConfig (3 tokens: viewer, operator, agent)            │
            └──────────────────────────────────────────────────────────┘
                                      ▲
                                      │
            ┌──────────────────────────────────────────────────────────┐
            │              SPEC-019 zero-config layer                   │
            │                                                            │
            │  arcteam.types.Entity.workspace_path  ←  arc team register │
            │                                          arc team backfill │
            │              │                                              │
            │              ▼                                              │
            │  arc ui start (no args)                                    │
            │     │                                                       │
            │     ├─ query registry → list[workspace_path]                │
            │     ├─ build list[JSONLTraceStore] → FederatedTraceStore    │
            │     ├─ aggregator.warm_start_multi(stores)                  │
            │     ├─ if loopback: webbrowser.open(url#auth=token)         │
            │     └─ audit.emit("ui.session_start", auth_method=...)      │
            │                                                             │
            │  ui_reporter (auto-enable probe)                           │
            │     │                                                       │
            │     ├─ if enabled = false: skip (opt-out)                   │
            │     └─ if enabled = true (default):                         │
            │         ├─ stat ~/.arcagent/ui-token (0600, owner)         │
            │         ├─ HEAD url                                         │
            │         └─ if both pass → connect; audit.emit(autoconnect) │
            └──────────────────────────────────────────────────────────┘
```

## Module Boundaries (Pillar 2)

Hard boundaries, no logic bleed:

| Module | Owns | Does NOT touch |
|--------|------|----------------|
| **arcteam** | Entity schema, registry persistence, backfill logic | Trace files, UI, agent config |
| **arcllm** | TraceStore protocol + JSONLTraceStore (writer) | Multi-store reading (that's UI's concern) |
| **arcui** | Multi-store aggregation, browser bootstrap auth, registry query | Writing to TraceStores, agent configuration |
| **arcagent** | `ui_reporter` module probe + opt-out logic | Discovering other agents, server-side state |
| **arccli** | Wiring `arc ui start` to registry; backfill CLI; passing `--workspace` to register | Business logic — pure orchestration |

Dependency direction:
```
arccli  →  arcui, arcteam, arcagent
arcui   →  arcteam (read registry), arcllm (TraceStore protocol)
arcagent →  (no new imports)
arcteam  →  (no new imports)
arcllm   →  (no changes)
```
No circular imports introduced.

## Component Designs

### 1. `arcteam.types.Entity.workspace_path` (FR-1, FR-2)

**File**: `packages/arcteam/src/arcteam/types.py` (modify)

```python
class Entity(BaseModel):
    id: str
    name: str
    type: EntityType
    roles: list[str] = []
    capabilities: list[str] = []
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: EntityStatus = EntityStatus.ACTIVE
    workspace_path: str | None = None  # absolute path; None for un-backfilled records
```

Pydantic deserializes existing records that lack the field (default `None`). Records written before backfill carry `None` until `arc team backfill-workspaces` repairs them.

### 2. Backfill subcommand (FR-3)

**File**: `packages/arccli/src/arccli/commands/team.py` (modify)

Idempotent; dry-run by default. Adds `EntityRegistry.update(entity)` — straightforward write-through with audit `entity.updated`.

### 3a. `FederatedTraceStore` — read-only federated query (FR-4, FR-5)

**File**: `packages/arcui/src/arcui/federated_store.py` (new)

Routes `/api/traces` and `/api/traces/{id}` read from `request.app.state.trace_store` — a single `TraceStore`. Zero-arg mode produces N stores. Without federation, the Traces tab would show only one workspace's history.

```python
class FederatedTraceStore:
    """Read-only TraceStore Protocol implementation that fans out across N stores.

    Distinct from JSONLTraceStore (writer-of-one). Separate class to make the
    one-reader-many-stores semantic explicit at the call site (Pillar 1).
    """
    def __init__(self, stores: list[TraceStore]) -> None:
        self._stores = stores

    async def query(self, *, limit: int, cursor: str | None,
                    agent: str | None = None, provider: str | None = None,
                    status: str | None = None, start: str | None = None,
                    end: str | None = None) -> tuple[list[TraceRecord], str | None]:
        """Fan out query, merge by timestamp desc, paginate via compound cursor.

        Cursor format: base64(json([{store_idx: i, sub_cursor: c}, ...])).
        Each call advances per-store sub-cursors; returns merged window.
        """
        ...

    async def get(self, trace_id: str) -> TraceRecord | None:
        """Try each store; UUIDs are globally unique so first hit wins."""
        for store in self._stores:
            rec = await store.get(trace_id)
            if rec is not None:
                return rec
        return None

    async def iter_records(self) -> AsyncIterator[dict[str, Any]]:
        """Heap-merge by timestamp (shared logic with warm_start_multi)."""
        ...

    async def close(self) -> None:
        for store in self._stores:
            await store.close()
```

**Wiring in `arc ui start`** (`packages/arccli/src/arccli/commands/ui.py`):
```python
stores = _resolve_trace_stores(args)  # always registry-driven
trace_store = FederatedTraceStore(stores) if stores else None
serve(trace_store=trace_store, ...)
```

The `/api/traces` route is unchanged — it sees a `TraceStore` Protocol.

**Per-agent filter** (`?agent=my_agent`): the existing single-store filter (`trace_store.py:341 if agent and rec.agent_label != agent`) runs inside each per-store query under federation; results combine post-filter.

### 3b. `RollingAggregator.warm_start_multi()` (FR-4, FR-5)

**File**: `packages/arcui/src/arcui/aggregator.py` (modify)

```python
async def warm_start_multi(self, stores: list[TraceStore]) -> None:
    """Warm-start aggregator from multiple TraceStores in chronological order.

    Reads all records from each store, merges by timestamp, and ingests
    into the global aggregator + per-agent sub-aggregators (which are
    created on first event per agent_id, per substrate design).

    Memory bound: streams record-by-record, never holds full file in memory.
    """
    import heapq

    async def _records_from(store: TraceStore) -> AsyncIterator[dict[str, Any]]:
        async for rec in store.iter_records():
            yield rec

    iterators = [aiter(_records_from(s)) for s in stores]
    async for record in _merge_by_timestamp(iterators):
        self.ingest(record)
```

**Decision D-4 rationale (Pillar 1):** A `MultiWorkspaceTraceStore` class would imply a single object that *is* multiple stores — confusing for writers. `warm_start_multi(stores)` makes the read-many-write-none semantic explicit at the call site.

**Adds to TraceStore Protocol** (`packages/arcllm/src/arcllm/trace_store.py`):
```python
class TraceStore(Protocol):
    ...existing methods...
    def iter_records(self) -> AsyncIterator[dict[str, Any]]: ...
```

`JSONLTraceStore` implements via async file reads over each `traces-{date}.jsonl` in chronological filename order.

### 4. Browser bootstrap auth (FR-6, FR-7, SR-2)

**File**: `packages/arcui/src/arcui/static/index.html` + `assets/arc-shell.js` (modify)

JS bootstrap (added at top of `arc-shell.js`):
```javascript
// Consume token from URL hash on first load
(function bootstrapAuth() {
  const hash = window.location.hash;
  const m = hash.match(/[#&]auth=([^&]+)/);
  if (m) {
    const token = decodeURIComponent(m[1]);
    localStorage.setItem('arcui_viewer_token', token);
    // SR-2: strip from URL before any other code runs
    history.replaceState(null, '', window.location.pathname + window.location.search);
  }
})();
```

**File**: `packages/arccli/src/arccli/commands/ui.py` (modify `_start`)

```python
def _maybe_open_browser(host: str, port: int, viewer_token: str) -> None:
    """Open browser only on loopback bind (FR-8)."""
    import webbrowser
    if host not in ("127.0.0.1", "localhost", "::1"):
        return  # SR-4: do not emit URL with token to logs
    url = f"http://{host}:{port}/#auth={viewer_token}"
    if not webbrowser.open(url):
        _write(f"  Open this URL: {url}")  # local terminal only, never logged
```

Called after `uvicorn` reports ready (use uvicorn lifespan event hook).

### 5. ui_reporter auto-enable probe (FR-9, FR-10)

**File**: `packages/arcagent/src/arcagent/modules/ui_reporter/__init__.py` (modify)

```python
class UIReporterConfig(BaseModel):
    enabled: bool = True   # default: probe-and-connect
    url: str = "ws://localhost:8420/api/agent/connect"
    ...

def _should_auto_enable(config_token_file: Path, url: str) -> tuple[bool, str]:
    """Returns (enable, reason). reason is for audit/log."""
    # SR-1: token file must be 0600, owned by current UID
    if not config_token_file.exists():
        return False, "token_file_absent"
    st = config_token_file.stat()
    if st.st_uid != os.getuid():
        return False, "token_file_wrong_owner"
    if st.st_mode & 0o077:
        return False, "token_file_loose_perms"
    health_url = url.replace("ws://", "http://").replace("wss://", "https://").rsplit("/api/", 1)[0] + "/api/health"
    try:
        with httpx.Client(timeout=0.05) as client:
            r = client.head(health_url)
            if r.status_code in (200, 405):
                return True, "probe_ok"
            return False, f"probe_status_{r.status_code}"
    except httpx.HTTPError as e:
        return False, f"probe_failed_{type(e).__name__}"

class UIReporterModule:
    async def startup(self, ctx: ModuleContext) -> None:
        if not self._config.enabled:
            return  # explicit opt-out
        enable, reason = _should_auto_enable(_TOKEN_FILE, self._config.url)
        if not enable:
            _logger.debug("ui_reporter: auto-disabled (%s)", reason)
            return
        await ctx.audit.emit("ui.agent_autoconnect", {
            "agent_id": ctx.agent_id,
            "uid": os.getuid(),
            "url": self._config.url,
            "reason": reason,
        })
        await self._connect()
```

**Decision D-5 rationale (Pillar 1):** Probe is one stat + one 50ms HEAD. If the user runs an agent without UI, it's invisible. If they later launch UI, restarting the agent picks it up. No flag plumbing across `run`/`chat`/`serve`.

### 6. Loopback session audit (SR-3, SR-4, SR-5)

**File**: `packages/arcui/src/arcui/auth.py` + `packages/arcui/src/arcui/audit.py` (modify)

Add audit emission on first valid token use per session (tracked by viewer_token hash → session_id mapping in memory):

```python
class UIAuditLogger:
    def session_start(self, session_id: str, *, uid: int, remote_addr: str, auth_method: str) -> None:
        self.audit_event("ui.session_start", {
            "session_id": session_id,
            "uid": uid,
            "remote_addr": remote_addr,
            "auth_method": auth_method,
        })
```

Triggered from token validation middleware on first match per session_id.

**Loopback does NOT bypass token validation.** SR-5 is explicit: only the *delivery* changes. The browser sends the token in `Authorization: Bearer <token>` from localStorage on every request — same as if pasted manually. The auth_method label distinguishes "this token reached the browser via URL bootstrap" vs "via manual paste"; both result in identical downstream auth checks.

### 7. Registry query in `arc ui start` (FR-4)

**File**: `packages/arccli/src/arccli/commands/ui.py` (modify `_start`)

```python
def _resolve_trace_stores(args) -> list[TraceStore]:
    """Build trace store list from the registry."""
    from arcteam.config import TeamConfig
    from arcteam.storage import FileBackend
    from arcteam.registry import EntityRegistry
    backend = FileBackend(TeamConfig().root)
    registry = EntityRegistry(backend, audit=None)
    entities = asyncio.run(registry.list_entities())
    stores = []
    for e in entities:
        if e.workspace_path is None:
            _write(f"  skip {e.id}: no workspace_path (run `arc team backfill-workspaces`)")
            continue
        wp = Path(e.workspace_path)
        if not wp.is_dir():
            _write(f"  skip {e.id}: workspace_path {wp} not found")
            continue
        from arcllm.trace_store import JSONLTraceStore
        stores.append(JSONLTraceStore(wp))
    return stores
```

## Data Flow

### Zero-arg `arc ui start`

```
User: arc ui start
  │
  ├─ resolve trace stores via registry  (1 query, 1 store per agent)
  ├─ wrap stores in FederatedTraceStore (or None if zero stores)
  ├─ start Starlette app with stores
  ├─ aggregator.warm_start_multi(stores)  (chronological merge)
  ├─ uvicorn ready event:
  │    ├─ if loopback: webbrowser.open("http://127.0.0.1:8420/#auth=<viewer>")
  │    └─ else: print tokens to stdout
  │
Browser opens
  │
  ├─ JS bootstrap reads #auth=, stores in localStorage, strips hash
  ├─ first XHR includes Authorization: Bearer <token>
  ├─ server validates → emits ui.session_start{auth_method: browser_bootstrap}
  └─ dashboard renders historical + live
```

### Agent autoconnect

```
arc agent chat team/my_agent
  │
  ├─ load config → ui_reporter module instantiated
  ├─ startup():
  │    ├─ self._config.enabled is True (default)
  │    ├─ _should_auto_enable(~/.arcagent/ui-token, ws://...):
  │    │    ├─ stat → 0600 owned by UID ✓
  │    │    └─ HEAD /api/health → 200 ✓
  │    ├─ audit.emit("ui.agent_autoconnect")
  │    └─ _connect() → WS path
  │
LLM call
  │
  └─ existing event flow → bridge → UIReporter → WS → server → browser
```

## File Inventory

### Modified Files

| File | Package | Changes | Approx LOC |
|------|---------|---------|------------|
| `arcteam/src/arcteam/types.py` | arcteam | Add `workspace_path: str \| None = None` to Entity | 5 |
| `arcteam/src/arcteam/registry.py` | arcteam | Add `update(entity)` method | 15 |
| `arcllm/src/arcllm/trace_store.py` | arcllm | Add `iter_records()` to Protocol + JSONLTraceStore impl | 40 |
| `arcui/src/arcui/aggregator.py` | arcui | Add `warm_start_multi(stores)` method | 60 |
| `arcui/src/arcui/auth.py` | arcui | Add `session_start` audit hook | 25 |
| `arcui/src/arcui/audit.py` | arcui | Add `session_start` and `agent_autoconnect` event types | 15 |
| `arcui/src/arcui/static/assets/arc-shell.js` | arcui | URL hash bootstrap + history.replaceState | 20 |
| `arcagent/src/arcagent/modules/ui_reporter/__init__.py` | arcagent | `_should_auto_enable` probe + startup branch | 50 |
| `arccli/src/arccli/commands/team.py` | arccli | `--workspace` flag on register; `backfill-workspaces` subcommand | 100 |
| `arccli/src/arccli/commands/ui.py` | arccli | `_resolve_trace_stores`; `_maybe_open_browser` | 80 |

### New Files

| File | Package | Purpose | Approx LOC |
|------|---------|---------|------------|
| `arcui/src/arcui/federated_store.py` | arcui | Read-only `FederatedTraceStore` for `/api/traces` over N stores | 70 |

### Tests (new)

| File | Type | Focus |
|------|------|-------|
| `arcteam/tests/unit/test_entity_workspace_path.py` | unit | Schema default (None), serialization |
| `arccli/tests/test_team_backfill.py` | integration | Idempotent backfill, dry-run/apply, missing TOML handling |
| `arcllm/tests/unit/test_trace_store_iter.py` | unit | `iter_records()` chronological order, multi-day file handling |
| `arcui/tests/unit/test_aggregator_multi.py` | unit | `warm_start_multi` heap merge correctness, empty stores, single store equivalence |
| `arcui/tests/unit/test_federated_store.py` | unit | `FederatedTraceStore` query fan-out, `?agent=` filter combines correctly, cursor pagination across stores, get() first-hit-wins |
| `arcui/tests/integration/test_traces_route_federated.py` | integration | `/api/traces?agent=my_agent` returns records from all matching stores |
| `arcui/tests/unit/test_browser_bootstrap.py` | unit | Hash parse, localStorage write, history.replaceState fired |
| `arcui/tests/unit/test_session_audit.py` | unit | First-request emits, second-request does not, auth_method label correctness |
| `arcagent/tests/unit/modules/ui_reporter/test_auto_enable.py` | unit | Probe matrix: file absent / wrong owner / loose perms / URL down / all pass |
| `arccli/tests/test_ui_zero_arg.py` | integration | Registry-driven discovery, missing-workspace warnings, loopback browser open mocked |

## Testing Strategy

| Type | Coverage Target | Focus |
|------|----------------|-------|
| Unit (70%) | All new helper fns; probe matrix; schema | Logic correctness, edge cases |
| Integration (20%) | Full `arc ui start` flow with mocked webbrowser; backfill round-trip | Module composition |
| E2E (10%) | Real subprocess: launch UI, register agent, run chat, verify trace appears | Full system |

## Security Posture (Pillar 3)

**Bind address is the security axis.** Single code path; configuration flips defaults.

| Surface | Loopback bind (default) | Non-loopback bind |
|---------|------------------------|-------------------|
| Browser auth | Token issued; delivered via URL hash; auto-loaded into localStorage | Token issued; printed to stdout with warning; manual paste required |
| REST/WS validation | Identical token check on every request | Identical token check on every request |
| Agent WS | Token via shared file; loopback OK without TLS | Token via shared file; mTLS required |
| Audit | session_start, agent_autoconnect, all standard events | Same + non-loopback bind warning |
| Token in logs | Never (URL with token only opened locally; never logged) | Never (printed to terminal stdout, not log file) |

**Federal override path (SR-7):** A site administrator who wants to *force* per-agent opt-in adds to `~/.arc/arcagent.toml`:
```toml
[modules.ui_reporter]
enabled = false  # default-off; each agent must explicitly enable
```
This sets the user-wide default; per-agent TOML can override. **Same code path** — only the value flow differs.

## Scalability Notes (Pillar 4)

- `warm_start_multi` is O(N records) total across all workspaces; heap merge is O(log K) per record where K = workspace count. 100 agents × 10K records each = 1M ops on a heap of 100 = ~7M comparisons. Sub-second on modern hardware.
- `iter_records` streams; memory is per-iterator buffer (~64KB), not file size.
- Probe is one HEAD per agent startup. 100-agent fleet boot = 100 × 50ms = 5s if serial. **Mitigation**: probe is per-agent, agents start in their own processes — naturally parallel.
- Registry query is one TOML/JSON read per entity; for 100 agents = ~100 file reads = <100ms.

## Open Questions

1. **Should `workspace_path` reject relative paths or normalize them?** Recommendation: reject — registry stores absolute paths only (SR-6). `--workspace` accepts relative, resolves to absolute before persist.
2. **Should `iter_records` belong on TraceStore Protocol or on a separate ReadableTraceStore Protocol?** Recommendation: same Protocol — JSONL is read+write by design; SQLite (future) likewise. Keep one Protocol.
3. **Health endpoint for probe** — does `/api/health` exist today? If not, add a 5-LOC unauthenticated route. (LOC accounted for in arcui audit.py row.)
4. **Browser hash auth and SPA history**: confirm `history.replaceState` fires before any tracking pixel or telemetry beacon. Recommendation: bootstrap script must be the first `<script>` tag in `<head>`, before anything else.

## Architecture Decision Records

Six ADRs were captured by the Wave 2 review. Each pins a non-obvious decision so future readers don't second-guess it from the code alone.

### ADR-019.1 — Watermark cursor over per-store sub-cursors for FederatedTraceStore

**Status**: Accepted (2026-04-27, Wave 2 review)
**Context**: A federated query fans out to N stores, takes the global top-`limit` by timestamp, and must page deterministically. The first cursor design recorded each store's own next-page sub-cursor in a base64 envelope. That design dropped records: when the merge truncated some records from store A but store A had no further sub-pages of its own, the truncated records were never re-fetched.
**Decision**: The cursor encodes a single timestamp watermark plus the trace_ids of records emitted at that watermark (`{"ts": "...", "skip": [...]}`). The next page asks every store for records `<= watermark` and skips any trace_id in `skip`.
**Consequences**: (+) Correct on truncated merges. (+) Each per-store query is idempotent. (−) Per page, every store does up to `limit` records of work even when it contributes zero — see ADR-019.7 for the parallelization fix.
**Alternatives**: Per-store sub-cursors (rejected — drops records); offset-only (rejected — requires global ordering at the storage layer that JSONLTraceStore doesn't provide).

### ADR-019.2 — SessionTracker LRU bounds and re-emission semantics

**Status**: Accepted (2026-04-27, Wave 2 review H-1 fix)
**Context**: `SessionTracker._sessions` and `_bootstrap_token_hashes` were unbounded dicts. NAT/CGNAT clients accrete `(token, addr)` entries indefinitely; long-running federal deployments OOM the UI process.
**Decision**: Both internal stores are `_BoundedLRU` with size caps from `ARCUI_MAX_SESSIONS` (default 10K) and `ARCUI_MAX_BOOTSTRAP_MARKERS` (default 1K). On overflow, the oldest entry is evicted. An evicted long-idle client returning later will re-emit `ui.session_start` — auditably correct: each session has a definite start, and an "idle for hours" gap is itself an auditable signal.
**Consequences**: (+) Bounded memory (~5MB total). (+) Operator can tune via env. (−) An auditor reading the log sees the same client emit two `ui.session_start` events for what they perceive as one continuous session. Documented above the class so this isn't a surprise.
**Alternatives**: Cachetools TTLCache (rejected — adds runtime dep for ~10 lines of saving). No bounds (rejected — H-1 OOM).

### ADR-019.3 — Starlette lifespan over uvicorn Server.startup monkey-patch

**Status**: Accepted (2026-04-27, Wave 2 review TD-04, supersedes Wave 1 fix S-1)
**Context**: The first browser-open implementation monkey-patched `uvicorn.Server.startup` to fire `webbrowser.open` after the server reported ready. CLAUDE.md explicitly bans monkey-patching. Wave 1 replaced it with `Starlette(on_startup=[...])`. Wave 2 noted that `on_startup=` is deprecated by Starlette in favor of the lifespan context manager.
**Decision**: `create_app` declares an async lifespan context manager. The CLI registers extra startup hooks via `app.state._extra_startup_hooks`; the lifespan invokes each before yielding.
**Consequences**: (+) No monkey-patching. (+) No deprecation warning in tests. (+) Hard-binds startup-vs-shutdown halves explicitly. (−) Starlette's lifespan API requires Starlette ≥ 0.15 (project pin already satisfies).
**Alternatives**: Continue monkey-patching (banned); keep `on_startup=` (deprecated).

### ADR-019.4 — Registry-driven store discovery as the only path

**Status**: Accepted (2026-04-26, FR-4)
**Context**: Earlier drafts of SPEC-019 carried a legacy `--traces-dir <path>` flag for explicit single-workspace launches. After the user explicitly required "no backwards compatibility," that flag was removed.
**Decision**: `arc ui start` *only* discovers TraceStores via the arcteam Entity registry. No per-launch path override exists.
**Consequences**: (+) Audit completeness — every dashboard launch sees only registered agents. (+) No "shadow workspace" path that bypasses governance. (−) Investigative scenarios where an operator wants to point the UI at a single old workspace require either temporary registry registration or a debug-only flag (out of scope).
**Alternatives**: Keep `--traces-dir` (rejected — user requirement); add a `--debug-traces-dir` opt-in (deferred).

### ADR-019.5 — `iter_records()` on TraceStore Protocol — chronological-storage-order contract

**Status**: Accepted (2026-04-26, T2.1)
**Context**: The federated/aggregator merge sort assumes each store yields records in non-decreasing `timestamp` order. JSONLTraceStore happens to do this because agents append in real-time order. A future SQLiteTraceStore could trivially violate the assumption.
**Decision**: `TraceStore.iter_records()` is documented as "yields records in non-decreasing storage-timestamp order." The merge enforces only this contract; out-of-order input is undefined behavior. A property-style test (`TestMergeByTimestampContract`) pins the merge's tie-breaking rule.
**Consequences**: (+) Federation/merge implementation stays simple (heap-merge, no resort). (−) Future TraceStore implementations must enforce the order at write time or sort at read time. Property test fails loud if a future store breaks the contract.
**Alternatives**: Sort inside `merge_by_timestamp` (rejected — pushes O(N log N) into the hot path on every warm-start).

### ADR-019.6 — Loopback-only autoconnect probe (review H-4)

**Status**: Accepted (2026-04-27, Wave 1 review H-4 fix)
**Context**: `ui_reporter._server_reachable` originally probed whatever URL the agent's TOML configured. A poisoned agent config (ASI06: memory/context poisoning) could redirect autoconnect to an attacker-controlled host; `_resolve_token` would then resolve a real agent token and `WebSocketTransport` would ship it to the attacker.
**Decision**: `_server_reachable` parses the URL host and rejects anything not in `LOOPBACK_HOSTS` *before* any HTTP request fires. Non-loopback URLs require explicit `enabled=true` plus signed-config attestation (deferred to a future federal-tier ADR).
**Consequences**: (+) Closes the config-poisoning exfiltration vector. (+) Personal-tier deployment is unaffected (default URL is `ws://localhost:8420/...`). (−) Cross-host UI deployments (operator on host A connecting agents from host B) require a deliberate non-default policy decision.
**Alternatives**: Allow non-loopback unconditionally (rejected — H-4 vector); allow with mTLS pinning (deferred to federal-tier ADR).

### ADR-019.7 — Parallel fan-out for FederatedTraceStore queries

**Status**: Accepted (2026-04-27, Wave 2 perf review)
**Context**: `_query_each_store` originally awaited each store's query in a `for` loop. With K=100 agents (federal target), page latency was 100 × per-store latency.
**Decision**: Per-store queries fire concurrently via `asyncio.gather`. Page latency becomes max(per-store) instead of sum(per-store).
**Consequences**: (+) ~100x latency improvement at federal scale. (+) Per-store I/O is independent (JSONL reads, no shared mutable state) so no synchronization required. (−) Slight increase in concurrent open file descriptors (one per store during query); negligible at K ≤ 100.
**Alternatives**: Bounded `asyncio.Semaphore` to cap concurrent reads (rejected — defaults already cap at OS file-descriptor limits; adding a manual cap is premature optimization).

