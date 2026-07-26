# ADR-022: Storage Split — arctrust Owns WORM Audit, arcstore Owns Operational/Observability Persistence

**Status**: Proposed
**Date**: 2026-05-31
**Builds on**: ADR-019 (Four Pillars Universal), ADR-020 (arcgateway as Data Plane)
**Supersedes**: `UIBridgeSink`-as-audit-sink from ADR-019 §Decision 4. The single emission point stands, but it no longer fans out to a live UI sink — the UI reads the durable store instead.

## Context

Two problems surfaced together.

**1. arcui misses live agent histories.** arcui depends on `arcui.bridge.UIBridgeSink` — a *live push* sink. When an agent runs while arcui is down, when the bridge drops, or when a sink raises (and `emit()` swallows it per AU-5), those events never reach the UI and there is no reconciliation path. The UI has no durable source to catch up from. Operational data (sessions, runs, token/cost, agent status, traces) is written ad hoc by each package, so there is no single place arcui can read to answer "what are my agents doing / what did they do."

**2. There is no durable WORM.** `arctrust.audit` ships two sinks:

| Sink | Durable? | Chained? | Signed? |
|---|---|---|---|
| `JsonlSink` | yes (append JSONL) | no | no |
| `SignedChainSink` | **no (in-memory `_records` only)** | yes | yes (Ed25519) |

Neither alone is a compliance-grade write-once record. The signed chain evaporates on process exit; the durable log is unchained and tamperable.

A separate need also emerged: a storage layer that defaults to SQLite but can be swapped for Postgres, Supabase, or cloud (Azure/AWS) — needed not just by arcui but by arcrun, arcagent, and direct arcllm calls. The open question was whether append-only/WORM and this operational store are one concern or two, and where each lives.

## Decision

They are **two concerns with two homes and a one-directional dependency.**

### arctrust — compliance system of record (the legal record)

- Owns append-only, **WORM-able**, signed hash-chained audit. The durable record merges what `JsonlSink` and `SignedChainSink` do today: a chained, Ed25519-signed event log persisted to an append-only file.
- **Always on by default, at every tier** — this is the audit pillar of ADR-019, not a federal-only feature. Personal/enterprise/federal all get a durable local WORM out of the box; tier sets stringency (sink default, retention, FIPS crypto), never existence.
- `AuditEvent`, `emit()`, and signing stay here. **arctrust imports no other Arc package** — preserving the ADR-019 nucleus. WORM lives here precisely so this stays true.

### arcstore — operational/observability data plane (new package)

arcstore is **ambient infrastructure**, not an opt-in dependency. Any arcllm, arcrun, or arcagent/mas usage is auto-tracked by default — a direct `arcllm` CLI call records its history with nothing else configured. It is built as **two layers** with different liveness requirements:

- **`arcstore.spool` — always-on local recorder (the infrastructure write path).** Every LLM call, run, and agent action appends a durable record to a local append-only spool (JSONL under the shared Arc data dir). **Zero dependency on a running store, server, or DB.** Dependency-light (stdlib + pydantic) so it never slows cold start (<500ms) or pulls DB drivers. This is what guarantees "make a direct call now, spin up arcstore later, still see it."
- **`arcstore` store/query layer — DB-backed, queryable.** A **pure file-tailer**: it ingests the two canonical durable files (the spool and the arctrust WORM) into queryable tables. It is not a sink in the emit path and owns no live wire. Exposes a `StorageBackend` Protocol (the seam). Default `SqliteBackend` (WAL). On startup it **backfills from both files** (everything recorded while the store was down) and then tails them live. Postgres/Supabase and cloud WORM-at-rest (S3 Object Lock / Azure immutable blob) are *additive* implementations of the proven Protocol — not built speculatively. DB/cloud drivers are optional extras (`arcstore[postgres]`, `arcstore[cloud]`), never pulled by the spool path.

**Auto-instrumentation is baked into the call boundaries** — arcllm's client and arcrun's loop emit spool records as a default behavior (config-toggleable off), not a sink each call site must wire. "Part of the infrastructure" = on unless deliberately disabled.

- **Imports arctrust; arctrust never imports arcstore.** Import direction: `arctrust ← arcstore ← {arcrun, arcagent, arcllm, arcui, arctui}`. Layer purity preserved. arcllm depending on `arcstore.spool` is acceptable because the spool is server-independent and dependency-light (installed ≠ running).

### How they connect

One write path per concern, one read path, no live wires in between:

- **Producers append to durable files.** `arctrust.emit()` writes only the WORM (compliance, signed). arcllm/arcrun/arcagent append to the spool (operational). Both write locally regardless of whether any server is up. A tool call that is both audited *and* operationally relevant is emitted once to each concern's file; LLM-usage telemetry (prompt/model/tokens/cost/latency) is distinctly the spool's and never passes through arctrust.
- **System of record (compliance) = arctrust's append-only signed chain** — the immutable legal record.
- **arcstore tails both files** into SQLite — a pure indexer, no emit-path sink, no UI bridge.
- **arcui/arctui read SQLite only** (poll, pause-on-inactive). Because both durable files always exist and arcstore backfills on startup, the "misses / doesn't update" failure is structurally impossible — there is no parallel push channel left to drop.
- **No `UIBridgeSink`.** The emit fan-out collapses to a single WORM sink. Lower-latency push, if ever needed, is added *from the database* (`sqlite3_update_hook` / Postgres `LISTEN/NOTIFY`) by the UI server — never as a second emission path.

This mirrors how framework-agnostic control planes (e.g. builderz-labs/mission-control) stay consistent: a durable canonical store read on a reconciliation path, with live push as an optimization — never the source of truth.

### Relationship to ADR-020

ADR-020 makes arcgateway the **data plane** (transport/control of inter-agent traffic). arcstore is the **persistence layer** that plane and the UIs read from. Distinct concerns: gateway moves data, arcstore durably stores the operational/observability slice. arcstore does not transport; arcgateway does not persist the operational record.

## Consequences

**Positive**

- arctrust gains a real durable WORM; the in-memory-only signed chain gap is closed without leaving the nucleus.
- arcui's miss/no-update class of bug is eliminated by design — it reconciles against an always-on durable store.
- One pluggable storage layer serves arcrun, arcagent, arcllm, and the UIs instead of ad hoc per-package persistence (the root of the current inconsistency).
- Usage is captured even when no store/server/UI is running. A direct `arcllm` CLI call is durably recorded to the spool; bringing arcstore up later backfills the full history. Observability is ambient, not something you must remember to enable.
- Backend portability (SQLite → Postgres → cloud) is additive behind one Protocol; the compliance guarantee (signed chain) is backend-independent because it lives in arctrust, not in any DB.
- Layer purity (ADR-019) intact: dependency is strictly one-directional.

**Negative / Cost**

- A new top-level package (`arcstore`) to scaffold, version, and wire.
- arctrust's two audit sinks must be reworked into one durable signed-chain WORM; callers and tests that distinguish `JsonlSink`/`SignedChainSink` change. Per the no-backward-compat directive, the old split is deleted, not shimmed.
- arcui must be refactored from push-only consumption to read-only-from-arcstore. `UIBridgeSink` and its bridge plumbing are deleted (no shim, per the no-backward-compat directive).

**Mitigation**

- v1 scope is bounded: `arcstore.spool` (always-on local recorder) + `StorageBackend` Protocol + `SqliteBackend` that tails/backfills the spool **and** the arctrust WORM + arctrust durable WORM + auto-instrumentation in arcllm/arcrun + arcui switched to read-only + on-by-default wiring. Postgres and cloud WORM are deferred until a concrete target exists.

## Compliance Mapping

| Concern | Control | Where |
|---|---|---|
| Protection of audit information | AU-9 | arctrust durable signed chain |
| Non-repudiation | AU-10 | arctrust Ed25519 signature |
| Audit retention | AU-11 | arctrust WORM file + arcstore retention policy |
| Audit storage capacity | AU-4 | arcstore backend (rollover, capacity alerts) |
| Protection of information at rest | SC-28 | cloud WORM backend (S3 Object Lock / Azure immutable) |

Federal layers (FIPS crypto, immutable-blob retention, FedRAMP boundary) build on this universal floor; they do not replace it.

## References

- ADR-019: Four Pillars Universal (`docs/architecture/decisions/ADR-019-four-pillars-universal.md`)
- ADR-020: arcgateway as Data Plane (`docs/architecture/decisions/ADR-020-arcgateway-as-data-plane.md`)
- `arctrust.audit` current sinks (`packages/arctrust/src/arctrust/audit.py`)
- Research: builderz-labs/mission-control framework-agnostic capture (canonical durable store + reconciliation + live push)
