# PRD — Full Trace Capture & Replay (Step 18)

## Problem Statement

ArcLLM records a `TraceRecord` per LLM call, but raw prompt/response bodies are OFF by default (`store_raw_bodies=False` in `TelemetryModule`). Traces are metadata-only: timing, tokens, cost, stop reason, phase timings. In federal production with thousands of concurrent agents, this is insufficient:

1. **You cannot see what was actually sent or received.** When an agent misbehaves — a rogue action (ASI10), a hallucinated tool call (LLM05), a misinformation incident (LLM09) — metadata tells you a call happened but not *what prompt produced it*. Root-cause analysis is impossible without the exact request and response.

2. **You cannot replay.** Reproducing a failure, A/B-diffing a prompt change, or re-running a call against a patched model requires the full, byte-exact request (messages, model, provider, tools, options). Metadata-only traces are not reconstructable.

3. **The safety rationale for metadata-only is really an encryption gap.** Metadata-only exists because durable plaintext prompts are an exfiltration target (LLM02) and leak system prompts (LLM07). That is an argument for *encrypting the bodies*, not for *not capturing them*. With envelope encryption, classification tagging, chmod-locking, and retention, full capture is federal-safe.

**Owner mandate**: flip the default. Capture full raw request-in / response-out on every call so every call is reconstructable for replay and forensic analysis. Reconcile with federal safety via encryption + classification + retention + audited disable.

## Goals

Each goal is tied to a principled-coder pillar (Simplicity → Modularity → Security → Scalability).

| # | Goal | Pillar | Success Metric |
|---|------|--------|----------------|
| G1 | Full raw capture by default: every call reconstructable | Security | `store_raw_bodies` defaults True; `request_body`/`response_body` populated on every `llm_call` record |
| G2 | Reversal stays federal-safe via compensating controls | Security | Federal tier: bodies AES-256-GCM encrypted at rest, classification-tagged, chmod 0600, retained/purged per policy |
| G3 | Raw bodies are tamper-evident, no new integrity code | Simplicity | Existing SHA-256 hash chain covers bodies + encryption envelope; `verify_chain()` still passes |
| G4 | Reuse existing schema + vault, add the minimum | Modularity | Reuse `request_body`/`response_body` + `VaultBackend`/`VaultResolver`; only encryption/classification/lineage are new |
| G5 | Replay reconstruction in arcllm; execution stays out | Modularity | `load_for_replay(trace_id)` returns a request object; arcllm never re-invokes a model |
| G6 | Lineage persisted verbatim, never fabricated | Modularity | `lineage` stored exactly as passed by arcrun/arcagent; arcllm constructs none |
| G7 | Disabling capture is an audited, deliberate act | Security | `store_raw_bodies=false` emits a `config_change` `TraceRecord` |
| G8 | Bounded growth under raw-by-default at scale | Scalability | Retention purge caps disk (`max_age_days`/`max_bytes`); per-body size cap prevents unbounded lines |

## Success Criteria

- [ ] SC-1: `store_raw_bodies` defaults to `True`; a call with no config produces a record with populated `request_body` and `response_body`.
- [ ] SC-2: `request_body` fully reconstructs the request — messages, model, provider, tools, and options (temperature, max_tokens, etc.).
- [ ] SC-3: `response_body` captures full response content + tool_calls + stop_reason.
- [ ] SC-4: `verify_chain()` succeeds over records containing raw bodies (hash covers bodies).
- [ ] SC-5: Federal tier: with encryption enabled, on-disk `request_body`/`response_body` are `null` and an `encryption` envelope (alg, wrapped_key, nonce, ciphertext, aad) is present.
- [ ] SC-6: Envelope round-trips: `_trace_crypto` decrypt(encrypt(bodies)) == bodies, using a KMS/vault-wrapped data key.
- [ ] SC-7: GCM AAD is bound to `trace_id`+`timestamp`; altering either fails decryption.
- [ ] SC-8: Each record carries a `classification` tag (config default when unspecified).
- [ ] SC-9: Retention purge deletes rotated files older than `max_age_days` or beyond `max_bytes`, oldest-first; live chain lines are never rewritten.
- [ ] SC-12: `load_for_replay(trace_id)` returns a fully-formed `ReplayRequest` (provider, model, messages, tools, options, lineage); decrypts transparently when encrypted.
- [ ] SC-13: arcllm exposes no execution/re-invoke path — `load_for_replay` only reconstructs (enforced by test + boundary doc).
- [ ] SC-14: `lineage` passed via `load_model`/`invoke` kwarg is persisted verbatim on the record.
- [ ] SC-15: Setting `store_raw_bodies=false` emits a `config_change` `TraceRecord` recording the downgrade.
- [ ] SC-16: `arcllm[trace-encryption]` installs `cryptography`; core install has zero crypto deps and encryption-disabled path never imports it.
- [ ] SC-17: Per-body size cap truncates oversized bodies with a truncation marker rather than writing an unbounded line.
- [ ] SC-18: All existing trace/telemetry tests pass; `mypy --strict` and `ruff check` clean.

## Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|-------------|----------|------------|
| FR-1 | `store_raw_bodies` defaults to `True` in `TelemetryModule` and `config.toml [modules.telemetry]` | P0 | Unit: record has populated bodies with no config override |
| FR-2 | `request_body` captures messages, model, provider, tools, and passthrough options | P0 | Unit: every field present and equal to the invoke inputs |
| FR-3 | `response_body` captures content, tool_calls, stop_reason | P0 | Unit: response fields round-trip |
| FR-4 | Hash chain covers `request_body`/`response_body`/`encryption` (no new integrity code) | P0 | Unit: `compute_hash` changes when a body changes; `verify_chain` passes |
| FR-5 | New `TraceEncryptionConfig` (backend, key ref, cache TTL) under `[modules.telemetry.encryption]` | P0 | Unit: config parses; disabled by default |
| FR-6 | `_trace_crypto.py` envelope: AES-256-GCM content key, data key wrapped by resolved KMS/vault key | P0 | Unit: encrypt→decrypt round-trips |
| FR-7 | Envelope stored in a new `encryption` field; plaintext bodies `null` when encryption on | P0 | Unit: on-disk record has null bodies + envelope |
| FR-8 | GCM AAD binds ciphertext to `trace_id`+`timestamp` | P0 | Unit: tamper AAD → decryption raises |
| FR-9 | Encryption key resolved via reused `VaultBackend`/`VaultResolver` (allowlisted `module:Class`, TTL cache) | P0 | Unit: mock backend returns wrapping key |
| FR-10 | New `classification: str` field on `TraceRecord`, default from config | P0 | Unit: default applied; explicit override honored |
| FR-11 | `TraceRetentionConfig` (max_age_days, max_bytes) under `[modules.telemetry.retention]` | P0 | Unit: config parses; unlimited when unset |
| FR-12 | Retention purge removes rotated files past age/size, oldest-first; never rewrites live lines | P0 | Unit: aged/oversized files deleted, current file kept |
| FR-17 | `load_for_replay(trace_id)` in `trace_query.py` returns a `ReplayRequest`; decrypts if needed | P0 | Unit: reconstructed request equals original inputs |
| FR-18 | `ReplayRequest` carries provider, model, messages, tools, options, lineage — no execute method | P0 | Unit: no re-invoke API surface (boundary test) |
| FR-19 | Optional `lineage: dict[str, Any] \| None` field on `TraceRecord`, persisted verbatim | P0 | Unit: lineage kwarg stored unchanged |
| FR-20 | `load_model`/`invoke` accept and thread a `lineage=` kwarg into the record; arcllm never builds it | P0 | Integration: lineage flows through untouched |
| FR-21 | `store_raw_bodies=false` emits a `config_change` `TraceRecord` (from→to, resolver identity) | P0 | Unit: downgrade emits an audited record |
| FR-22 | `arcllm[trace-encryption]` extra pulls `cryptography`; encryption-off path never imports it | P0 | Unit: import guarded; clear error when extra missing but encryption on |
| FR-23 | Per-body size cap: oversized body truncated with a marker, size recorded | P1 | Unit: body over cap → truncated + `truncated=true` marker |
| FR-24 | Tier defaults: personal plaintext chmod-locked; federal encryption+classification+retention on | P1 | Integration: tier presets resolve to correct config |

## Non-Functional Requirements

| ID | Requirement | Target | Measurement |
|----|-------------|--------|-------------|
| NFR-1 | Capture overhead (plaintext) per call | <2ms added | Benchmark vs metadata-only |
| NFR-2 | Envelope encryption overhead per call | <5ms for typical body | Benchmark |
| NFR-3 | Wrapping-key resolution on cache hit | 0ms (no vault call) | Reuse `VaultResolver` TTL cache |
| NFR-4 | Zero crypto deps when encryption disabled | No `cryptography` import | Lazy import verification |
| NFR-5 | Bounded memory on retention purge | O(file list), not O(records) | Purge operates on whole files |
| NFR-6 | Replay reconstruction memory | O(one record) | Single-record lookup, no full scan when indexed by id |

## User Stories

### US-1: Incident Responder
As an incident responder, I want the exact prompt and response for a flagged call so that I can reconstruct what the agent actually saw and did (ASI10), instead of guessing from token counts.

### US-2: Federal ISSO
As an ISSO in a SCIF, I want raw bodies encrypted at rest with vault-wrapped keys and classification-tagged so that full capture satisfies AU-9/SC-28 and I can prove no plaintext prompt is durable on disk.

### US-3: Agent Developer
As a developer debugging a prompt regression, I want `load_for_replay(trace_id)` to hand me the byte-exact request so that I can re-run it (via arcrun tooling) against a fixed prompt and diff outputs.

### US-4: Compliance Officer
As a compliance officer, I want retention limits so that we honor data-handling policy (SI-12, AU-11) even though we now capture everything — and I want any *disabling* of capture to itself be audited.

### US-5: Platform Architect
As the platform architect, I want lineage (template source, RAG docs, variable substitution) attached to each trace so that I can trace an output back to its inputs — while keeping lineage construction in arcrun/arcagent, not arcllm.

## Compliance & Threat Mapping

### NIST 800-53

| Control | Requirement | This spec |
|---------|-------------|-----------|
| AU-2 | Auditable events | Full request/response captured; disabling capture is itself an audited `config_change` (D-444) |
| AU-3 | Content of audit records | Raw bodies give complete "what happened" content (D-435) |
| AU-9 | Protection of audit information | chmod 0600/0700 outside workspace sandbox + envelope encryption (D-438) |
| AU-10 | Non-repudiation | SHA-256 hash chain covers raw bodies + envelope; GCM AAD binds to record identity (D-437, D-448) |
| AU-11 | Audit record retention | `max_age_days`/`max_bytes` retention + purge (D-440) |
| SI-12 | Information handling & retention | Classification tag + retention (D-439, D-440) |
| SC-28 | Protection of information at rest | AES-256-GCM envelope, vault-wrapped data key (D-438, D-447) |

### OWASP

| Code | Relationship | Handling |
|------|-------------|----------|
| ASI10 | **Enables** rogue-agent forensics | Full capture is the evidence base for behavioral post-mortems |
| LLM05 | **Enables** improper-output-handling review | Raw response shows exactly what the model emitted before downstream use |
| LLM09 | **Enables** misinformation post-hoc review | Prompt+response pair lets reviewers judge grounding after the fact |
| LLM02 | **Tension** (durable sensitive data) | Envelope encryption + classification + chmod + access-scoped read mitigate exfiltration risk |
| LLM07 | **Tension** (system-prompt leakage) | System prompt now durable → encrypted at rest, disabling capture audited, retention-bounded |

## Out of Scope

- Replay **execution** — re-invoking the model, diffing outputs, scoring (belongs to arcrun/tooling; see SDD Boundaries).
- **Lineage construction** — template/RAG/variable provenance is built by arcrun/arcagent (arcllm only persists it verbatim).
- Concrete KMS/vault backend implementations (shipped as extras packages; this spec reuses the protocol).
- Reversible tokenization / de-identification of bodies (encryption, not redaction, is the control here).
- PII redaction of bodies (owned by `SecurityModule`, Spec 012 — orthogonal to capture).
- Key-rotation orchestration beyond re-wrapping support (envelope stores which key wrapped it; rotation policy is operational).
- UI rendering of raw bodies (arcui/arcstore concern).

## Dependencies

| Dependency | Type | Notes |
|------------|------|-------|
| `trace_store.py` (`TraceRecord`, `JSONLTraceStore`) | Internal | Schema fields, hash chain, rotation, chmod (Spec 009/019) |
| `trace_query.py` | Internal | Read path extended with `load_for_replay` (Spec 009) |
| `modules/telemetry.py` (`TelemetryModule`, `_raw_bodies`) | Internal | Default flip + encryption hook (Spec 009) |
| `vault.py` (`VaultBackend`, `VaultResolver`) | Internal | Reused for wrapping-key resolution (Spec 012) |
| `config.py` | Internal | New `TraceEncryptionConfig`, `TraceRetentionConfig` models |
| `jcs` | External | Canonical JSON for hash (already a dep) |
| `hashlib` (stdlib) | External | SHA-256 chain |
| `cryptography` (optional) | External | AES-256-GCM via `arcllm[trace-encryption]` |
