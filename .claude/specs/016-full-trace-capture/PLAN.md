# PLAN — Full Trace Capture & Replay (Step 18)

**Status**: COMPLETE
**Spec**: 016-full-trace-capture
**Estimated tasks**: 12
**Estimated new tests**: ~42 (10 capture + 10 crypto + 6 retention + 10 replay + 6 registry/integration)
**Boundary**: arcllm only. Replay EXECUTION and lineage CONSTRUCTION are arcrun/arcagent (do NOT implement here).

**Scope note**: Right-to-erasure was REMOVED from this spec per implementation
directive. No erase/crypto-shred path was implemented — this is capture +
encryption + classification + retention purge + replay + lineage only.

---

## Phase 1: Schema & Config Foundation (Tasks 1-3)

### T16.1 — Extend TraceRecord schema (test-first)
- [x] Write `test_trace_capture.py` cases (RED): new `classification` default, `encryption` field optional, `lineage` optional.
- [x] Add `EncryptedEnvelope` frozen model (alg, wrapped_key, key_ref, nonce, ciphertext, aad) to `trace_store.py`.
- [x] Add `classification: str = "unclassified"`, `encryption: EncryptedEnvelope | None = None`, `lineage: dict[str, Any] | None = None` to `TraceRecord`.

**Acceptance**:
- [x] New fields validate with defaults; existing records (no new fields) still parse.
- [x] `compute_hash()` unchanged and now covers new fields (verified by a hash-changes test).
- [x] `mypy --strict` and `ruff check` clean.

### T16.2 — Add config models
- [x] Add `TraceEncryptionConfig` (enabled, backend, key_ref, key_env, cache_ttl_seconds, require_fips) to `config.py`.
- [x] Add `TraceRetentionConfig` (max_age_days, max_bytes) to `config.py`.
- [x] Extend `[modules.telemetry]` parsing to accept `store_raw_bodies`, `classification`, nested `encryption`, `retention` (via the existing generic `ModuleConfig(extra="allow")` mechanism — see Boundaries/Disagreements note below).

**Acceptance**:
- [x] Config loads with new sections; unspecified fields take documented defaults (encryption off, retention unlimited).
- [x] Existing config tests pass.

### T16.3 — Update config.toml and pyproject extras
- [x] `config.toml [modules.telemetry]`: `store_raw_bodies = true`, `classification = "unclassified"`.
- [x] Add `[modules.telemetry.encryption]` and `[modules.telemetry.retention]` default sections.
- [x] Add `trace-encryption = ["cryptography>=42.0"]` to `[project.optional-dependencies]`.

**Acceptance**:
- [x] Config loads without error; `cryptography` (the `trace-encryption` extra's sole dependency) is already present in the dev venv — verified via direct import.
- [x] Default config yields capture ON, encryption OFF.

---

## Phase 2: The Default Flip + Audited Disable (Task 4)

### T16.4 — Flip store_raw_bodies default, wire classification/lineage, audit the disable
- [x] Extend `test_trace_capture.py` (RED): no-config call populates bodies; disable emits `config_change`; classification + lineage attach.
- [x] In `TelemetryModule.__init__`, change `store_raw_bodies` default to `True`; remove the "raw = warn" inversion (now the *disable* warns/audits).
- [x] On `store_raw_bodies=false`, emit a `config_change` `TraceRecord` (from→to, resolver identity) via `_emit_trace`.
- [x] Attach `classification` (config default) and `lineage` (from kwargs, verbatim) in `_build_trace_record`.

**Acceptance**:
- [x] Default call → `request_body`/`response_body` populated (SC-1/2/3).
- [x] Disable path emits exactly one `config_change` audit record (SC-15).
- [x] `lineage` stored byte-for-byte; arcllm constructs none (SC-14).
- [x] `verify_chain()` passes over body-bearing records (SC-4).

---

## Phase 3: Envelope Encryption (Tasks 5-6)

### T16.5 — Implement _trace_crypto.py (test-first)
- [x] Write `test_trace_crypto.py` (RED): seal→unseal round-trip; AAD tamper → raise; missing extra → `ArcLLMConfigError`.
- [x] Implement `seal()` / `unseal()` with AES-256-GCM, 256-bit data key, AES Key Wrap envelope, AAD = `"<trace_id>:<timestamp>"`.
- [x] Guard the `cryptography` import; clear error when `arcllm[trace-encryption]` absent.

**Acceptance**:
- [x] Round-trip equals original bodies (SC-6).
- [x] Altered `trace_id`/`timestamp` fails decryption — raises `ArcLLMTraceIntegrityError` (SC-7).
- [x] Encryption-off path never imports `cryptography` (NFR-4) — lazy import inside `seal()`/`unseal()`/`fips_provider_active()` only.

### T16.6 — Wire encryption into telemetry + reuse VaultResolver for the wrapping key
- [x] Extend `test_trace_crypto.py` / capture tests (RED): encryption-on record has null bodies + envelope; wrapping key from mock `VaultBackend`; unresolvable key → fail-closed.
- [x] Resolve wrapping key via existing `VaultResolver.resolve_api_key(key_env, key_ref)` (D-447) in `registry.py`; pass into `TelemetryModule`.
- [x] When `encryption.enabled`, seal bodies into `encryption`, set `request_body`/`response_body` to `None`.

**Acceptance**:
- [x] On-disk record: bodies `None`, `encryption` present (SC-5).
- [x] Wrapping key resolves via reused vault path; missing key raises (no silent plaintext fallback).
- [x] `verify_chain()` passes over encrypted records.

---

## Phase 4: Retention (Task 7)

### T16.7 — Implement retention purge (test-first)
- [x] Write `test_trace_retention.py` purge cases (RED): age purge, size purge oldest-first, live file preserved.
- [x] Implement `trace_retention.purge(traces_dir, max_age_days, max_bytes)` — whole-file deletion, never rewrite live lines.
- [x] Wire an optional purge trigger into `JSONLTraceStore` rotation (`_maybe_purge()`, called from `_maybe_rotate()`).

**Acceptance**:
- [x] Files past `max_age_days` deleted; oldest deleted until under `max_bytes` (SC-9).
- [x] Current-day file never purged; `verify_chain()` passes over survivors (required loosening `verify_chain()`'s genesis assumption — see Disagreements note).

---

## Phase 5: Replay Reconstruction (Task 9)

### T16.9 — Implement load_for_replay + ReplayRequest (test-first)
- [x] Write `test_trace_replay.py` (RED): reconstruct plaintext; decrypt encrypted; missing resolver → error; legacy metadata-only → clear error; lineage round-trip; boundary (no execute method).
- [x] Add frozen `ReplayRequest` dataclass (provider, model, messages, tools, options, lineage, classification) to `trace_query.py`.
- [x] Implement `load_for_replay(traces_dir, trace_id, *, wrapping_key_resolver=None)` — reuse `get_record`, transparent unseal, rebuild `Message`/`Tool`.

**Acceptance**:
- [x] Reconstructed request equals original inputs, plaintext and encrypted (SC-12).
- [x] `ReplayRequest` has NO re-invoke/I/O method — boundary test asserts it (SC-13).
- [x] Metadata-only legacy record → explicit "not reconstructable" error.

---

## Phase 6: Registry, Exports, Tier Presets (Tasks 10-13)

### T16.10 — Thread lineage kwarg and encryption config through registry
- [x] `load_model()` accepts `lineage=`; passed into telemetry config → record (verbatim). (`invoke()` also accepts a per-call `lineage=` kwarg, threaded through `TelemetryModule.invoke(**kwargs)` — overrides the `load_model()`-level default per call.)
- [x] Resolve `[modules.telemetry.encryption]` + wrapping key at construction (in `registry.py`, before `TelemetryModule(...)` is built — AU-2: never re-resolved per call).

**Acceptance**:
- [x] `load_model(lineage=...)` → record carries the exact dict (FR-20).
- [x] Encryption config flows to `TelemetryModule`.

### T16.11 — Tier presets
- [x] Personal/enterprise: plaintext (encryption off by default), classification floor "unclassified"/"internal".
- [x] Federal: encryption on + `require_fips` fail-closed self-check, classification floor raised, retention configured.

**Acceptance**:
- [x] Tier-derived settings (`store_raw_bodies`, `encryption`, `classification` floor) are resolved ONCE at `TelemetryModule.__init__` and never re-read per call — enforced structurally (no per-call kwarg can flip them) and covered by `TestTierFlowsThroughConstruction` in `test_trace_capture.py` (AU-2 regression test, mirrors `test_registry_propagates_tier_to_policy_context` from arcagent). See Disagreements note on tier-preset scoping.

### T16.12 — Exports & docstrings
- [x] Export `ReplayRequest`, `EncryptedEnvelope`, `TraceEncryptionConfig`, `TraceRetentionConfig`, `load_for_replay`, `load_telemetry_retention_config`, `ArcLLMTraceNotFoundError`, `ArcLLMTraceIntegrityError` from `arcllm/__init__.py`.
- [x] Docstrings on all new public API; comment the WHY on the default flip and the retention-purge/append-only reconciliation.

**Acceptance**:
- [x] Public API importable; docstrings present.
- [x] `ruff check` clean.

### T16.13 — Full-suite verification
- [x] Run full test suite: all existing trace/telemetry/registry tests pass + 104 new tests (41 capture + 20 crypto + 21 retention + 14 replay + 8 registry integration).
- [x] `mypy --strict` clean; `ruff check` clean.
- [x] Confirm no re-invoke/lineage-construction logic leaked into arcllm (boundary review — `ReplayRequest` is a pure frozen dataclass with zero public methods; verified by `TestReplayRequestBoundary`).

**Acceptance**:
- [x] All tests green (existing + new): 1261 passed, 1 skipped.
- [x] >=90% coverage on new files: `_trace_crypto.py` 100%, `trace_retention.py` 100%, `trace_query.py`/`trace_store.py`/`modules/telemetry.py` 98-99% (remaining gaps are pre-existing, unrelated lines).
- [x] Lint/type gates pass (SC-18).

---

## Research-Derived Additions (from /deepen)

Additive test + hardening notes. They extend existing tasks; they do not replace or reorder them. IDs reference the task each note attaches to.

### Adopted deployment controls (from /deepen research)
- [x] **DEC-B (T16.5/T16.11): FIPS-validated crypto for the federal tier (SC-13) — adopted default.** Implemented as `TraceEncryptionConfig.require_fips` + `_trace_crypto.assert_fips_provider_if_required()`, called at `TelemetryModule.__init__` when `encryption.enabled`. Fails closed with `ArcLLMConfigError` when the loaded provider is not FIPS-approved.

### T16.5 — _trace_crypto.py (add)
- [x] **Fixed the wrap-nonce schema gap**: wraps the DEK with AES Key Wrap (RFC 3394 / SP 800-38F) via `cryptography.hazmat.primitives.keywrap` — no second GCM call, no `wrap_nonce` field needed (nonce-free primitive).
- [x] **GCM nonce-uniqueness test**: `TestNonceUniqueness` asserts unique nonces across 500 seals (500 was chosen over 10k for CI runtime; the property being tested — `os.urandom(12)` per call — does not depend on N) and each is exactly 96 bits (12 bytes).
- [x] **FIPS provider self-check test**: `TestFipsSelfCheck` — `require_fips=True` + non-FIPS provider → `ArcLLMConfigError` at construction.

### T16.6 — encryption wiring (add)
- [x] **KEK-rotation test**: `TestKekRotation` — seal under `key_ref="v1"`, then a new record under `key_ref="v2"`; both unseal via their own stored `key_ref`.

### T16.7 — retention purge (add)
- [x] **encrypt-then-hash integrity test** (load-bearing): `test_verify_chain_passes_over_encrypted_records` — chain hashes the `encryption` field's ciphertext; `verify_chain()` passes without decrypting.
- [x] **Truncation/rollback test**: `TestBuildCheckpoint.test_checkpoint_detects_purge_of_a_rotated_file` — documents that `verify_chain()` alone still passes after a purge (loosened genesis assumption, see Disagreements), while `build_checkpoint()`'s shrinking file inventory makes the purge externally observable. Signing/anchoring that checkpoint is explicitly out of scope (arctrust concern, not arcllm — see docstring).
- [x] **Concurrency race test**: `TestPurgeConcurrentAppendRace` — real `asyncio.create_task` interleaving (not a sequential mock) between an appender and a purger; purge never touches the live file; batch size bounded via `max_files_per_run`; `except Exception` (never `BaseException`) around delete failures.

### T16.9 — load_for_replay (add)
- [x] **CUI-on-replay test**: `test_carries_classification_for_access_control` — `ReplayRequest.classification` is populated verbatim from the record.
- [x] **NFKC/size-cap test**: `test_oversized_lineage_truncated_with_marker` (capture tests) — lineage is NFKC-normalized (classification) and size-capped (both) at write time; round-trips as inert data on replay.

### T16.10 / T16.11 — tier flow (add)
- [x] **Tier-through-construction test**: `TestTierFlowsThroughConstruction` in `test_trace_capture.py`.
- [x] **Classification-floor test**: `TestClassificationFloor` — a per-call classification below the tier floor is clamped to the floor, never downgraded; unrecognized labels are fail-safe clamped to the floor.

### T16.13 — verification (add)
- [x] **Large-payload / storage-growth check**: `TestBodySizeCap` — an oversized body engages the truncation marker (`{"truncated": true, "original_bytes": N}`); per-body cap is configurable via `max_body_bytes`.
- [x] **Crash-safety test**: covered by the pre-existing `test_trace_store.py`/`test_trace_store_iter.py` suite (unchanged by this spec — the append/warm-start/tail-verification code path was not modified except for the documented `verify_chain()` genesis loosening).

## Completion Checklist

- [x] All 12 tasks complete
- [x] All tests pass (existing + new): 1261 passed, 1 skipped
- [x] Coverage >=90% on new code (100% on `_trace_crypto.py`/`trace_retention.py`; 98-99% on modified files)
- [ ] Decision log updated (D-435 through D-448) — NOT done in this pass; repo-root `.claude/decisions-log.md` is explicitly out of scope per implementation constraints. Flagged for the user/spec owner to update separately.
- [x] Boundary honored: no replay execution, no lineage construction in arcllm
- [x] Spec status set COMPLETE and committed with the implementation

## Disagreements / Resolutions (spec vs. implementation reality)

1. **Retention config location**: The SDD's literal TOML shape (`[modules.telemetry.retention]` feeding straight into `TelemetryModule`) doesn't fit the actual data flow — retention purges a `JSONLTraceStore`'s directory, and that store is constructed independently of `load_model()` by whoever owns `agent_root` (arcagent). Resolution: kept the TOML section exactly as specified (for config-schema fidelity), but `registry.py` pops `"retention"` out of the telemetry config dict before constructing `TelemetryModule` (which has no use for it), and a new `arcllm.config.load_telemetry_retention_config()` accessor lets the store-owning caller read and wire it into `JSONLTraceStore(..., retention_max_age_days=..., retention_max_bytes=...)` directly.

2. **`verify_chain()` genesis assumption loosened**: Whole-file retention purge can delete the oldest rotated file(s), leaving the new oldest surviving record with a legitimately non-zero `prev_hash` pointing at a file that no longer exists. The pre-existing `verify_chain()` hardcoded `"0"*64` as the required starting `prev_hash`, which would have made every purge look like tampering. Changed `verify_chain()` to trust the first-verified record's own `prev_hash` as the baseline (a deliberate, documented tamper-evidence limitation — the chain proves internal consistency among records present, not that no earlier records were removed). No existing test depended on the old strict-genesis behavior; `build_checkpoint()` provides the external anchor the SDD's Research Insights call for.

3. **Classification kwarg collision with pre-existing `RoutingModule`**: `classification` was already a load-bearing per-call kwarg consumed by the pre-SPEC-016 `RoutingModule` (content-classification-based provider routing). Initial implementation stripped `classification` from the kwargs `TelemetryModule` forwards to its inner module, which silently broke routing (caught by the existing `test_routing_stack.py` suite). Resolved by leaving `classification` in the forwarded kwargs (so `RoutingModule`, further inside the stack, still receives and pops it) while `TelemetryModule` independently reads `kwargs.get("classification")` for its own watermark-floor resolution — the same kwarg name serves two independent, non-conflicting consumers at different stack layers.

4. **FIPS self-check implementation**: `cryptography` exposes no public API for FIPS-provider status (pyca/cryptography#7722). Implemented `fips_provider_active()` against the internal `cryptography.hazmat.backends.openssl.backend.backend._fips_enabled` attribute, documented as such. In this dev environment (vendored PyPI wheel OpenSSL) it correctly reports `False`; `require_fips` therefore defaults to `False` and is a federal-tier-only opt-in.

5. **Right-to-erasure removal**: Per the task's explicit instruction, no erase/crypto-shred path was implemented anywhere (no per-subject/per-record shred keys, no erasure API). The single-KEK envelope model is used throughout, exactly as the (updated) SDD specifies.
