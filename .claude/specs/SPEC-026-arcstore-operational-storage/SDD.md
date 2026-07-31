# SPEC-026 — Arcstore Operational Storage: Solution Design

> Authoritative design decisions live in **ADR-022**. This SDD makes them buildable: module boundaries, contracts, schemas, file layout. Every design choice is pillar-traced (S/M/Sec/Sc).

## 1. Architecture Overview

```
WRITE  (each producer appends to its own durable file — the whole write path)
  arctrust.emit(event, sink=WormSink)  → arctrust WORM file   (signed chain, compliance)
  arcllm / arcrun / arcagent           → arcstore spool file  (operational, always-on)

INGEST
  arcstore store layer  ── tails + backfills ──►  SQLite (default backend)

READ
  arcui / arctui  ── poll, pause-on-inactive ──►  SQLite
```

Two durable files, one indexer, one read path. No live wire in between. arcstore is a **pure file-tailer** — it is never a sink in `emit()`.

## 2. Module Boundaries (hard contracts)

| Module | Owns | Imports | Never |
|---|---|---|---|
| `arctrust` (nucleus) | `AuditEvent`, signing, `emit()`, **durable WORM** | *no Arc package* (stdlib, pynacl, pydantic) | imports arcstore; owns operational telemetry |
| `arcstore.spool` | always-on local recorder, spool record schema, reader | stdlib, pydantic | imports a backend, DB driver, or server |
| `arcstore` (store layer) | `StorageBackend` Protocol, `SqliteBackend`, file tailer, backfill, query API | `arctrust` (to read/verify WORM), `arcstore.spool`, stdlib `sqlite3` | imports arcllm/arcrun/arcagent/arcui |
| `arcllm` / `arcrun` / `arcagent` | their existing concern + **default-on** spool recording | `arcstore.spool` only | imports the store/backends |
| `arcui` / `arctui` | read-only rendering from the store query API | `arcstore` (query layer) | acts as a sink/subscriber of `emit()` |

**Import DAG (enforced by test):** `arctrust ← arcstore.spool ← arcstore ← {arcllm, arcrun, arcagent} ← arcui/arctui`. No cycles. arctrust is the sink of the graph.

## 3. arctrust — Durable WORM (FR-1)

### 3.1 Contract

Replace `JsonlSink` and `SignedChainSink` with one `WormSink`:

```python
class WormSink:
    def __init__(self, path: Path, operator_private_key: bytes) -> None: ...
    @property
    def chain_tip(self) -> str: ...          # restored from file tail on init
    def write(self, event: AuditEvent) -> None: ...   # append signed, chained line; fail-open
    def verify_chain(self) -> bool: ...      # read file, validate links + signatures
```

- **Record line** (one JSON object per line): `{"event": {...}, "prev_hash": "...", "event_hash": "...", "signature": "<hex>"}`.
- `event_hash = sha256(prev_hash + canonical_json(event)).hexdigest()`, where `canonical_json` uses `sort_keys=True, default=str` (matches today's `SignedChainSink` so verify stays consistent).
- `signature = ed25519_sign(event_hash, operator_private_key)` — signing stays in arctrust (keypair lives here). **S, Sec.**
- **Startup recovery:** on `__init__`, read the file tail to restore `chain_tip` and `prev_hash`, so the chain continues across restarts (fixes the in-memory-only gap). **Sec.**
- **File mode:** opened append-only, created `0600` (NFR-5). **Sec.**
- **Fail-open:** `write()` swallows IO errors and logs at WARNING; `emit()` already enforces AU-5 (FR-1 AC-1.4). **Sec, Sc.**

### 3.2 emit() fan-out collapse

`emit(event, sink)` is unchanged in signature. The **default sink** becomes `WormSink`. `UIBridgeSink` is removed from arctrust's exports and from any default wiring (FR-5). Single emission point preserved (ADR-019); it just no longer fans to a UI wire. **S, M.**

### 3.3 Tier mapping (stringency, not existence)

| Knob | Personal | Enterprise | Federal |
|---|---|---|---|
| WORM | on (local file) | on (local file) | on (FIPS sign) |
| Retention | size-rollover | size-rollover | retention policy + cloud WORM-at-rest (future) |
| Crypto | Ed25519 | Ed25519 | FIPS-validated |

## 4. arcstore.spool — Always-On Recorder (FR-2, FR-4)

### 4.1 Record schema (NFR-1: one flat model)

```python
class SpoolRecord(BaseModel):           # frozen
    kind: Literal["llm_call", "run_event", "agent_event"]
    actor_did: str
    ts: str                              # ISO-8601 UTC, auto if omitted
    request_id: str | None = None
    # kind-specific, flat:
    model: str | None = None            # llm_call
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    outcome: str | None = None          # ok | error
    name: str | None = None             # run_event/agent_event: step/phase name
    extra: dict[str, Any] = {}
```

### 4.2 Contract

```python
# arcstore/spool.py — stdlib + pydantic only
def record(rec: SpoolRecord, *, path: Path | None = None) -> None: ...   # append one line, fail-open
def read(path: Path) -> Iterator[SpoolRecord]: ...                       # for the store tailer
def spool_path() -> Path: ...                                            # default under Arc data dir
```

- Default `spool_path()` resolves to `<arc_data_dir>/spool/operational.jsonl` (data dir from shared config; created on first write). **S.**
- `record()` is sync, fast, append-only, fail-open. No async, no lock contention beyond append. **Sc.**
- Importing this module loads no backend (FR-2 AC-2.2). **Sc.**

### 4.3 Auto-instrumentation hooks

- **arcllm**: in the client completion path, build a `SpoolRecord(kind="llm_call", ...)` from the response usage and call `record()` in a `finally` so errors still record (`outcome="error"`). Guarded by `arcstore.enabled` config (default true). **M.**
- **arcrun**: at loop start/step/finish emit `run_event`. **M.**
- Producers import only `arcstore.spool`. **M.**

## 5. arcstore Store Layer — Tailer + Backfill (FR-3)

### 5.1 StorageBackend Protocol (the seam)

```python
@runtime_checkable
class StorageBackend(Protocol):
    def upsert(self, table: str, key: str, row: dict[str, Any]) -> None: ...
    def query(self, table: str, *, where: dict | None = None,
              order_by: str | None = None, limit: int | None = None) -> list[dict]: ...
    def begin(self) -> AbstractContextManager[None]: ...   # transaction
```

No SQLite type leaks into the Protocol (FR-3 AC-3.4 proves it with a fake backend). **M.**

### 5.2 SqliteBackend (default)

- WAL mode; tables: `llm_calls`, `run_events`, `agent_events`, `audit_chain`. Each row keyed by a stable record identity (`request_id`+`ts`+`kind` hash) for idempotent ingest (FR-3 AC-3.3). **S, Sec.**
- DB path under the Arc data dir; created on first run. Driver = stdlib `sqlite3` (no extra dep). **Sc.**

### 5.3 Ingest engine

```python
class StoreIngest:
    def backfill(self) -> None: ...   # read spool + WORM from offset 0..EOF, upsert (idempotent)
    def tail(self) -> None: ...       # follow appends from last offset, upsert
```

- On `arc store up`: `backfill()` then `tail()` (FR-3 AC-3.1/3.2). **S.**
- Tailer tracks a per-file byte offset persisted in the backend so restarts resume, not re-scan. **Sc.**
- The WORM is read via `arctrust` (verify on ingest, store verified flag). **Sec.**

## 6. arcui / arctui — Read-Only (FR-5)

- Delete `arcui.bridge.UIBridgeSink` + bridge module + any registration of it as a sink. **S.**
- Routes that previously consumed pushed events now call the arcstore **query API** (`query(...)`), returning JSON for the existing front-end. **M.**
- Front-end polls on an interval with `document.hidden`/visibility-change gating (pause-on-inactive, FR-5 AC-5.3). **Sc.**
- No new transport; this reuses arcui's existing HTTP route layer. arcgateway's data plane (ADR-020) is untouched. **M.**

## 7. Data Flow — UC-1 "call now, see later"

```
t0  arc llm "..."        → arcllm builds SpoolRecord → arcstore.spool.record() → operational.jsonl (append)
     (no store/UI running; line is durable on disk)
t1  arc store up         → StoreIngest.backfill() reads operational.jsonl from offset 0
                         → upsert into llm_calls (idempotent)
                         → tail() follows further appends
t2  open arcui           → GET /api/llm-calls → backend.query("llm_calls", ...) → rendered
```

The t0 record is independent of t1/t2. This is the structural guarantee. **S, Sec, Sc.**

## 8. Package Layout

```
packages/arcstore/
  pyproject.toml          # name=arcstore; deps: pydantic; extras: [postgres],[cloud]; dep: arctrust
  src/arcstore/
    __init__.py
    spool.py              # FR-2: record/read/spool_path (stdlib+pydantic only)
    records.py            # SpoolRecord model
    config.py             # data dir, enabled flag, backend selection (TOML)
    backends/
      base.py             # StorageBackend Protocol
      sqlite.py           # SqliteBackend (default)
      memory.py           # FakeBackend for tests (FR-3 AC-3.4)
    ingest.py             # StoreIngest: backfill + tail
    query.py              # read API consumed by arcui/arctui
  tests/{unit,integration}/

packages/arctrust/src/arctrust/
    audit.py              # WormSink replaces JsonlSink + SignedChainSink; emit() default sink = WormSink
```

## 9. Failure Modes & Mitigations

| Failure | Behavior | Pillar |
|---|---|---|
| Spool/WORM disk write fails | logged, swallowed; call proceeds (AU-5) | Sec |
| Store down at call time | call records to file; backfill recovers it later | S, Sc |
| Corrupt spool line | tailer skips + logs the bad offset; continues | Sec |
| WORM tampered on disk | `verify_chain()` fails; ingest flags row unverified | Sec |
| Two store processes ingest same file | upsert idempotency prevents dup rows; offset persisted | Sc |
| arcllm imported without arcstore installed | hard error at import — arcstore is infrastructure, expected installed (D-010) | M |

## 10. Test Strategy

- **Unit (70%)**: WormSink durability/verify/tamper/forge; spool record fail-open + import isolation; SqliteBackend idempotent upsert; FakeBackend conformance.
- **Integration (20%)**: backfill-from-down-store; tail-while-running; arcllm→spool→store→query end-to-end (UC-1); arcllm error path records `outcome=error`.
- **Architecture**: import-graph test enforcing §2 DAG; grep test asserting no `UIBridgeSink` reference (FR-5 AC-5.1); `emit()` single-default-sink test.
- **Performance**: spool write p95 < 5 ms; cold-start import budget (NFR-2).

---

## 11. Research Insights (from /deepen, 2026-05-31)

Six parallel research agents (web + codebase) enriched this design. Solutions archive: empty (no prior learnings). Findings below are filtered Simplicity > Modularity > Security > Scalability, each with the three mandatory callouts (scalability ceiling / air-gapped posture / module boundary).

### 11.0 CRITICAL — defects & wrong defaults found in *existing* code (fix during implementation)

| # | Location | Issue | Fix | Pillar |
|---|---|---|---|---|
| C1 | `arctrust/audit.py` `verify_chain()` | **Signatures are written but never verified** — only hash links are checked. AU-10 non-repudiation is currently dead code. | `verify_chain(public_key)` must `arctrust.keypair.verify()` every record's signature. | Sec |
| C2 | `arcllm/modules/telemetry.py:160` | `store_raw_bodies` defaults to **`True`** — durable plaintext prompts/responses. Wrong secure default for federal/SCIF. | Flip default to **`False`**; raw capture is explicit opt-in + startup warning. Update `test_store_raw_bodies_defaults_to_true`. | Sec |
| C3 | `arcllm/modules/telemetry.py:405-486` | No `try/finally` — emission only on success path; a raising call records nothing (violates FR-4 AC-4.4). | Wrap inner call; emit in `finally` with `outcome` set. | Sec |
| C4 | `arctrust/audit.py` chain | **No sequence numbers** — end-truncation of a valid prefix is undetectable by hash-chain alone. | Add monotonic `seq` per record, included in the hash; verify checks for gaps. | Sec |
| C5 | `arcagent/session/index.py`, `arcgateway/pairing.py` schemas | `busy_timeout` PRAGMA not set — first concurrent write returns `SQLITE_BUSY` immediately. | Set `busy_timeout` on every connection in `SqliteBackend` (and backfill the precedents per "leave it correct"). | Sc |

### 11.1 arctrust WORM (FR-1)

- **Reuse precedent, don't invent:** `arcllm.trace_store.JSONLTraceStore` already does warm-start-from-file-tail + daily rotation + hash chaining; `WormSink` should mirror its shape. **S.**
- **Canonical serialization:** replace `json.dumps(..., default=str)` with Pydantic v2 `model_dump(mode="json")` + `sort_keys=True, ensure_ascii=True`. `default=str` is a determinism hazard if `extra` ever carries non-str types. For ASCII-only `AuditEvent` keys this is RFC-8785-equivalent — **do NOT add the `rfc8785` dep** (complexity for marginal gain). [RFC 8785]. **S, Sec.**
- **Restart tip-recovery:** seek backward from EOF for the last complete line, parse `event_hash`, *re-validate it* before trusting; binary append-mode positions correctly after a crash. **Sec.**
- **Sign each entry (keep current), not just the head** — head-only signing gives no per-record non-repudiation (AU-10). Signing `event_hash` is sufficient (it commits content + link). **Sec.**
- **Single-writer enforcement:** because `WormSink` holds in-memory tip, two writers fork the chain. Hold `fcntl.flock(LOCK_EX|LOCK_NB)` across writes; raise on contention (stdlib, no dep). arctrust stays a leaf — caller ensures one instance/process. **Sec, M.**
- **Torn last line on crash:** on restart, try/except the last-line parse; if bad, truncate to prior `\n` **and append a signed `recovery` record** (never silent — silent truncation == adversarial truncation). **Sec.**
- **Genesis attack:** an adversary can present a fresh empty file. Store the expected `chain_tip` out-of-band in `arctrust.trust_store` and assert it on startup. **Sec.**
- **Callouts:** *(ceiling)* the real ceiling is **RAM + verify time**, not file size — today `SignedChainSink` holds all records in a list (500 MB RAM at 1M records). Stream on verify; **rotate at 100k records / 50 MB** to keep verify < 2 s. Ed25519 verify ≈ 87k/s (PyNaCl) → annual chain verifies in ~100 s nightly. *(air-gapped)* no Rekor/RFC-3161 TSA available — timestamps are self-asserted; substitute = signed `checkpoint` records against an authoritative internal stratum-1 NTP (document this explicitly). *(module)* all needs met by stdlib (`hashlib`,`os`,`fcntl`,`pathlib`) + pynacl — **no new dep**; only API change is `verify_chain(public_key)` sourcing the key from same-package `trust_store`.

### 11.2 arcstore.spool (FR-2)

- **Use `os.write(fd, line.encode())`, not `file.write()`** — CPython slices large `file.write()` into 4 KB chunks, breaking multi-process append atomicity. Open via `os.open(path, O_WRONLY|O_APPEND|O_CREAT, 0o600)`; one newline-terminated record = one atomic syscall on Linux (inode mutex serializes writers). **No `fcntl` lock needed** (unlike WORM — spool keeps no in-memory state). [Python bug #15723; nullprogram O_APPEND]. **S, Sc.**
- **No per-record fsync** — "survives process crash, not OS crash" is the right contract for regenerable operational telemetry (fsync is a ~1750× penalty). Hard durability lives in arctrust WORM, not the spool. **S, Sc.**
- **`spool.read()` may live in the same module** (stdlib `json`+`pathlib` only) without breaking the no-DB-driver rule. **M.**
- **Daily rotation in v1** (`operational-YYYY-MM-DD.jsonl`) — matches `JSONLTraceStore`; gives the ingester a clean per-file inode/offset model. Unbounded single file is ~3 GB/day at 10k tx/min. **S, Sc.**
- **No `asyncio.Lock`** in `record()` — that lock exists in `JSONLTraceStore` only because it advances an in-memory hash chain; the spool has none. **Sc.**
- **Callouts:** *(ceiling)* single JSONL ≈ 10k–50k records/s on SSD ≫ 167 tx/s target; shard to `spool/<actor_did>/…` only when benchmarks show contention (≥100k tx/min). *(air-gapped)* fully local, stdlib only. *(module)* enforce by import-graph test: `import arcstore.spool; assert "sqlite3" not in sys.modules`.

### 11.3 Store layer: backend + ingest (FR-3)

- **Clone `arcagent.session.index.SessionIndex` verbatim** — it is already the exact pattern: WAL, single writer, `executemany` batching of 500, `ON CONFLICT DO UPDATE` idempotency, `asyncio.to_thread` wrapping, JSONL-as-truth + SQLite-as-derived-index, byte-offset incremental ingest. **S.**
- **A `StorageBackend` Protocol already exists** in `arcteam.storage` (`@runtime_checkable`, async, with `FileBackend`+`MemoryBackend`, and a `@pytest.fixture(params=["file","memory"])`). Match this house style: **async Protocol**, sqlite bridged via `asyncio.to_thread` (not `aiosqlite` in the default path), plain stdlib return types (no driver cursors leak). **M.**
- **Design refinement — drop `begin()` from the Protocol.** Per the arcteam precedent, transactions are an *implementation detail* of backends that support them; exposing `begin()` forces `MemoryBackend`/`SqliteBackend` to fake it. Model the ingest transaction *inside* `SqliteBackend` (batch upsert + offset advance in one `BEGIN IMMEDIATE`…`COMMIT`). Protocol surface stays `upsert`/`query`. **S, M.** *(supersedes SDD §5.1 `begin()`.)*
- **PRAGMA stack:** `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `journal_size_limit=64MB`, `wal_autocheckpoint` tuned; `BEGIN IMMEDIATE` for writes (avoids lock-upgrade `SQLITE_BUSY` that ignores busy_timeout). **Sc.**
- **At-least-once is safe:** advance the persisted offset only on `COMMIT`; replay after crash is deduped by `INSERT OR IGNORE` on the content-derived key (`sha256(kind,actor_did,ts,request_id)`) — **never** a byte offset or rowid as the key. Batch 100–500/txn (not 10k → smaller replay). **Sec, Sc.**
- **WAL checkpoint starvation:** a long-lived UI read transaction pins the WAL and it grows unbounded (real incident: 20 GB WAL). Keep UI reads short-lived (open→query→close); `journal_size_limit` caps growth. Alert if WAL > 2× DB. **Sc.**
- **Callouts:** *(ceiling, quantified)* single-writer SQLite = 20k–100k inserts/s; 10k tx/min (167 tx/s) is trivial — **but only with one writer per file.** 10–20 instances sharing one file = `SQLITE_BUSY` storms; **each instance owns its own DB file** (shared-nothing, CLAUDE.md). Switch-to-Postgres signals: cross-instance JOIN/aggregation queries, >5k tx/s sustained per instance, or WAL persistently >64 MB. *(air-gapped)* SQLite is ideal — zero network/daemon/creds, single-file backup. *(module)* `sqlite3` stdlib = builtin; `asyncpg`/`boto3`/`azure-*` are **optional extras, lazy-imported inside the backend class**, never at module top level.

### 11.4 Pluggable backend + cloud/on-prem WORM (FR-3, future)

- **Cloud object stores cannot append** — map the chain as **object-per-segment** (`audit/2026/05/31/00001-00250.jsonl`, PUT-once then locked) + a locked **manifest** holding each segment's SHA-256 and the chain tip. Azure `AppendBlob` then seal is a cleaner streaming fit than S3 pure-PUT. **Sec.**
- **Federal authorization:** S3 Object Lock **Compliance mode** in GovCloud (FedRAMP High); Azure immutable blobs in Azure Government (IL4/FedRAMP High). **Neither is authorized above IL4** and **neither works air-gapped** (SDKs need egress). **Sec.**
- **Air-gapped WORM equivalents (the deciding constraint for DOE/Labs/NASA):** `chattr +a` (soft, root-strippable — but the signed chain still *detects* tamper), **WORM NAS (NetApp SnapLock / Dell DataDomain)** = hardware-enforced, SCIF-deployable (S3-Compliance equivalent), LTO-9 WORM tape for archival. **The signed chain shifts "WORM" from "storage prevents writes" to "any change is cryptographically detectable"** — sufficient with `chattr +a` for FedRAMP Low/Mod + CMMC L1-L2; hardware WORM NAS for High. **Sec.**
- **Don't over-build:** no `StorageCapabilities` matrix and no extra backends until ≥2 real backends diverge (the arcrun `BackendCapabilities` precedent earned its complexity; this hasn't). **S.**

### 11.5 LLM telemetry auto-instrumentation (FR-4)

- **Locked-by-default = metadata only.** `SpoolRecord` already excludes prompt/response text by design (correct). The *companion* fix is flipping `store_raw_bodies` to `False` (C2): the durable file becomes an exfiltration target (LLM02) and raw system prompts leak operational/classified context (LLM07); regex PII redaction can't recognize CUI/export-controlled data, so it cannot justify raw storage. **Sec dominates here.**
- **Align fields to OTel GenAI conventions:** `gen_ai.request/response.model`, `gen_ai.usage.input_tokens`/`output_tokens`, `finish_reasons`, `duration_ms`, `cost_usd` (cache read/write pricing already in `telemetry_cost.py`). **M.**
- **Side-channel discipline:** call `arcstore.spool.record()` **outside** the OTel span scope (don't inflate span duration), never `await` it in a way that can raise into the caller, swallow all errors (AU-5). Spool records *every* call (it's the durable record); sampling belongs at OTel/ingest, not the spool. **M, Sec.**
- **Encryption-at-rest gap (SC-28):** spool/WORM are plaintext `0600`. Metadata-only default bounds exposure, but for SCIF/CUI sessions either require an encrypted FS (LUKS) or per-line vault-key encryption. **New NFR-7.** **Sec.**
- **Callouts:** *(ceiling)* per-agent spool files at high volume; separate `spool_sample_rate` from OTel sampling. *(air-gapped)* OTel `endpoint=localhost:4317` or `exporter="none"`; spool is the durable fallback. *(module)* hook lives only in `TelemetryModule`, imports only `arcstore.spool`, must not alter `LLMResponse`.

### 11.6 arcui/arctui read-only (FR-5)

- **The scalability lever is cursor-incremental fetch, not transport.** Backend already returns `next_after_seq` (and `FederatedTraceStore` has a watermark+`skip_ids` cursor) — the frontend currently **ignores it** and re-fetches the full 100-row window every 5 s. Thread the cursor: fetch only rows since last `seq`; payload drops from O(history) to O(new). **S, Sc.**
- **Polling pause is nearly free:** TanStack Query's `focusManager` already pauses `refetchInterval` when `document.hidden` (keep `refetchIntervalInBackground:false`) — no custom listener needed. Use an adaptive `refetchInterval` fn returning `false` after an empty diff. **S, Sc.**
- **Deferred push-signal: use `PRAGMA data_version`, NOT `sqlite3_update_hook`.** `update_hook` fires only on the *same connection* (breaks across processes / future Postgres). `data_version` bumps on any commit, readable cross-process from a read-only conn — poll it at 500 ms server-side, push a lightweight `{"type":"data_changed"}` over **SSE** (simpler than WS for one-way signals); browser still *fetches from the DB* (single source of truth, signal ≠ data). Postgres path = `LISTEN/NOTIFY` → same SSE frame, zero UI change. **S, M.**
- **Delete dead push machinery:** `useLiveStore` (Zustand, fed by WS `handleEventBatch`), the data-receiving role of `arcSocket`, `EventBuffer`, `SubscriptionManager` broadcast. Connection status moves to React Query `isError`/`failureCount`. The subscribe/agent-filter role can collapse to a `?agents=` query param. **S, M.**
- **Missed-row hazard:** advancing the cursor to the true max can skip an in-flight commit timestamped below it. Prefer a **monotonic `seq`/rowid** cursor (all writers local → no clock skew) over wall-clock; or read with a small "safe lag." SQLite WAL snapshot isolation already hides mid-commit rows within one read txn. **Sec.**
- **Callouts:** *(ceiling)* 10–20 instances × 1 tab × 5 s ≈ 2–4 QPS — trivial for SQLite; switch to `data_version`+SSE only above ~50 concurrent sessions or when polls are >80% empty. `BroadcastChannel` tab-leader election is a *future* multi-tab optimization (over-engineering for the single-operator case). *(air-gapped)* all mechanisms work offline. *(module)* UI calls only `arcstore` query REST routes — never `emit()`/`UIBridgeSink`/`EventBuffer`.

### Source-of-truth precedents to clone (codebase)

| Build target | Clone from |
|---|---|
| `WormSink` durable chain + warm-start | `arcllm/trace_store.py` `JSONLTraceStore` |
| `StoreIngest` forward-scan + byte offset | `arcllm/trace_query.py` `_read_lines_reverse`; `arcagent/session/index.py` `SessionIndex` |
| `SqliteBackend` (WAL, batch, idempotent, to_thread) | `arcagent/session/index.py` `SessionIndex` |
| `StorageBackend` Protocol + fake-backend tests | `arcteam/storage.py` + `arcteam/tests/unit/test_storage.py` |
| Spool fail-open write | `arctrust/audit.py` `JsonlSink` (upgrade to `os.write`) |

---

## 12. CLI Lifecycle & Cross-Package Integration (FR-6)

### 12.1 Who touches arcstore, and how

| Package | Change | Imports |
|---|---|---|
| `arctrust` | `WormSink` durable signed chain; `verify_chain(public_key)`. arcstore *reads/verifies* this file. | none (nucleus) |
| `arcllm` | `TelemetryModule` emits `SpoolRecord(kind="llm_call")` in a `finally`; `store_raw_bodies` default → `False`. | `arcstore.spool` only |
| `arcrun` | loop emits `SpoolRecord(kind="run_event")` at start/step/finish. | `arcstore.spool` only |
| `arcagent` | on construct, ensures spool dir; agent loop owns the `StoreIngest` lifecycle when serving. | `arcstore` |
| `arccli` | `arc agent create/serve/run`, `arc team`, and new `arc store` group spin up / manage arcstore. | `arcstore` |

### 12.2 Spin-up sequence (`arc agent serve` / `run` / `arc team`)

```
load config → resolve arcstore data_dir (§13.2)
ensure spool dir exists (ALWAYS — even if store disabled)        ← guarantees call-now-see-later
if [arcstore].enabled and backend configured:
    open backend (SqliteBackend, per-instance file)
    StoreIngest.backfill()        # recover anything recorded while down
    start StoreIngest.tail() as a managed background task
    register shutdown hook → stop tail, close backend
  on ANY failure here: log WARNING, mark store degraded, CONTINUE   ← fail-open (AC-6.3)
start agent loop
```

The spool writer is independent of this sequence — producers write to it whether or not the ingester started. The ingester is a managed task owned by the serving process (mirrors how arcrun/arcgateway own their background tasks today).

### 12.3 `arc store` command group

| Command | Action |
|---|---|
| `arc store up` | start ingest standalone (backfill + tail) against the resolved store |
| `arc store status` | report spool path, store path, backend, ingest offset, degraded flag |
| `arc store verify` | `arctrust.verify_chain()` over the WORM; non-zero exit on tamper (AC-6.4) |
| `arc store backfill` | force a full re-ingest from spool + WORM (idempotent) |
| `arc store init` | create data dir + DB + spool idempotently |

## 13. Config Schema — `[arcstore]` (FR-7)

### 13.1 One canonical model

`arcstore.config.ArcStoreConfig` is the single Pydantic schema; arcllm/arcrun/arcagent/arccli **reference** it (no redefinition — DRY, AC-7.3). It sits beside existing blocks (`[telemetry]`, `[session]` in arcagent; `[defaults]`/`[modules]` in arcllm).

```toml
[arcstore]
enabled = true              # FR-2/FR-4 on-by-default; false disables spool+store for this entry point
data_dir = ""               # "" → resolved per §13.2
backend = "sqlite"          # sqlite (default) | postgres | (future) cloud-worm
store_raw_bodies = false     # FR-4 AC-4.5 — metadata only by default (federal-safe)
rotation = "daily"          # spool/WORM file rotation
retention = ""              # optional retention policy (federal)
sample_rate = 1.0           # spool records every call by default
```

Env overrides follow existing convention: `ARCAGENT_ARCSTORE__ENABLED=false`, etc.

### 13.2 Data-dir resolution (identical across arcllm / arcrun / arccli)

Precedence — **must be the same function in all three entry points** so a direct `arc llm` call and a later `arc agent serve` agree on the path (AC-7.1, AC-7.2):

```
ARCSTORE_DATA_DIR (env)  >  [arcstore].data_dir (toml)  >  ~/.arc/store (default)
```

Resolution lives in `arcstore.config.resolve_data_dir()` — imported, never reimplemented. Layout under the resolved dir:

```
<data_dir>/
  spool/operational-YYYY-MM-DD.jsonl     # arcstore.spool writes (always-on)
  worm/audit-chain.jsonl                 # arctrust.WormSink writes (always-on)
  store/<instance>.db                    # SqliteBackend (per-instance, shared-nothing)
```

### 13.3 set / use / create

- **set** — `arc agent create` and `arc store init` write the `[arcstore]` block.
- **use** — every entry point loads `ArcStoreConfig` and calls `resolve_data_dir()`.
- **create** — `StoreIngest`/`spool` create dirs + DB idempotently on first use; `arc store init` does it eagerly.
