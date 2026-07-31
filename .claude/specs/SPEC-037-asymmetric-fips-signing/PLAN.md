# SPEC-037 — PLAN

**Feature:** Asymmetric + FIPS signing + out-of-process key custody
**Status:** COMPLETE
**Workflow:** TDD (RED → GREEN → REFACTOR) per task. One module per task. Each
task lists its REQ and the component it touches.

Quality gates per task: `ruff check`, `mypy --strict`, coverage ≥ 90% on new
core, 0 surviving HMAC-signing symbols (grep-gated in T-13).

---

## Traceability matrix

| REQ | Component | Task(s) |
|-----|-----------|---------|
| REQ-005, REQ-007 | `arctrust/signer.py` (`Signer`, `InProcessSigner`, `build_signer`) | T-01, T-02 |
| REQ-006 | `arctrust/signer.py` (`VaultTransit`, `VaultSigner`, `FileNotaryTransit`) | T-03, T-04 |
| REQ-008, REQ-009, REQ-010 | `arctrust/fips.py` | T-05 |
| REQ-005, REQ-003 | `arctrust/audit.py` `WormSink` → `Signer` | T-06 |
| REQ-005, REQ-006 | `arctrust/operator.py` loader → `Signer`/`VaultSigner` | T-07 |
| REQ-001, REQ-004, REQ-011 | `arcllm/_signing.py` + `arcllm/modules/security.py` | T-08, T-09 |
| REQ-008 (dedup) | `arcllm/_trace_crypto.py` imports arctrust FIPS gate | T-10 |
| REQ-002, REQ-003 | `arcteam/audit.py` + `arcteam/types.py` | T-11, T-12 |
| REQ-001, REQ-002, REQ-012 | HMAC-deletion verification (grep gate) | T-13 |
| REQ-009 | tier/config matrix integration | T-14 |

---

## Tasks

### T-01 — `Signer` Protocol + `InProcessSigner` (arctrust) · REQ-005, REQ-007
- RED: test `InProcessSigner(seed).sign(msg)` verifies with `.public_key` via
  `arctrust.keypair.verify`; `.algorithm == "ed25519"`.
- GREEN: define `Signer` Protocol; `InProcessSigner` (Ed25519 via
  `arctrust.keypair.sign`). Export from `arctrust/__init__.py`.
- Note: ECDSA-P256 branch is scaffolded but tested in T-08 alongside arcllm.

### T-02 — `build_signer` factory + `SignerConfig` (arctrust) · REQ-007
- RED: `build_signer(custody="in_process", seed=…)` → `InProcessSigner`;
  `custody="vault_transit"` with no transit client raises (fail-closed, NFR-3).
- GREEN: implement factory + Pydantic `SignerConfig`.

### T-03 — `VaultTransit` Protocol + `VaultSigner` (arctrust) · REQ-006
- RED: fake `VaultTransit` that **raises if the seed is ever requested**;
  `VaultSigner(fake, key_ref).sign(msg)` returns a signature that verifies with
  `fake.public_key(key_ref)`; assert seed never enters the process.
- GREEN: `VaultSigner.sign` delegates to `transit.sign(key_ref, message)`;
  `.public_key` cached from `transit.public_key`.

### T-04 — reference `FileNotaryTransit` out-of-process signer (arctrust) · REQ-006
- RED: `FileNotaryTransit` talks to a separate local signer process/socket;
  round-trips a signature without exposing the seed to the caller process.
- GREEN: minimal reference impl for CI/dev (documented as reference, not the
  federal HSM binding).

### T-05 — `arctrust/fips.py` generalised gate · REQ-008, REQ-009, REQ-010
- RED: `assert_fips_if_required(require_fips=True, algorithm="ed25519")` raises
  `ArcTrustFipsError` when backend flag is non-FIPS; `require_fips=False` passes;
  `algorithm_is_fips_approved("ecdsa-p256")` True.
- GREEN: move `fips_provider_active`/`assert_fips_provider_if_required` from
  `_trace_crypto`, rename, add `algorithm_is_fips_approved`, add
  `ArcTrustFipsError`. Export.

### T-06 — `WormSink` takes `Signer` (arctrust) · REQ-005, REQ-003
- RED: `WormSink(path, signer=fake_signer)` signs each record via the signer;
  `verify_chain(public_key)` unchanged in shape and still verifies; a fake
  `Signer` (no raw seed) works.
- GREEN: swap `operator_private_key: bytes` → `signer: Signer`; record
  `algorithm` per record. Update all in-package WORM callers in the same edit.

### T-07 — operator loader → `Signer`/`VaultSigner` (arctrust) · REQ-005, REQ-006
- RED: `custody=in_process` → `OperatorKey.into_signer()` (in-process);
  `custody=vault_transit` → `VaultSigner`, and `resolve_secret`/`from_seed` are
  **never called** (assert via fake). 
- GREEN: add `into_signer()` + a `vault_transit` branch to the loader; keep the
  existing on-disk custody hardening for `in_process`.

### T-08 — arcllm `Signer` adoption + ECDSA-P256 (arcllm) · REQ-001, REQ-004
- RED: `create_signer("ed25519", …)` and `create_signer("ecdsa-p256", …)` return
  arctrust `Signer`s whose signatures verify with the public key; verifier holds
  no private material; `hmac-sha256` is rejected.
- GREEN: rewrite `_signing.py` to build arctrust `Signer`s; implement the
  ECDSA-P256 path (PyCA `cryptography`); **delete** `HmacSigner`, `hmac`,
  `hashlib`. Keep `canonical_payload`.

### T-09 — `security.py` attaches asymmetric signature (arcllm) · REQ-001, REQ-011
- RED: response carries `signature` + `algorithm` + `public_key`/`key_ref`; the
  signed label equals the config-resolved `model_name` (not a response field).
- GREEN: update `_attach_signature` + metadata; default `config.toml`
  `signing_algorithm = "ed25519"`. Update `test_security.py`.

### T-10 — `_trace_crypto` imports arctrust FIPS gate (arcllm) · REQ-008
- RED: `_trace_crypto` FIPS behavior unchanged from the caller's view but sourced
  from arctrust.
- GREEN: delete the local FIPS functions; import from `arctrust.fips`. Update
  `assert_fips_provider_if_required` call sites.

### T-11 — arcteam `AuditRecord` signer fields (arcteam) · REQ-002, REQ-003
- RED: `AuditRecord` has `signature` + `key_ref`; no `hmac_sha256`.
- GREEN: edit `arcteam/types.py`; update `test_types.py`.

### T-12 — arcteam `AuditLogger` → `Signer` chain (arcteam) · REQ-002
- RED: `AuditLogger(signer)` signs each record over `prev_hash || canonical`;
  `verify_chain` verifies signatures; record/signature mutation fails; no
  `ARCTEAM_HMAC_KEY`.
- GREEN: replace `_compute_record_hmac` with a `Signer`-based chain; **delete**
  `hmac`, `hashlib`, `ARCTEAM_HMAC_KEY`, `load_hmac_key`. Update `test_audit.py`.

### T-13 — HMAC-deletion verification gate · REQ-001, REQ-002, REQ-012
- RED (assertion test / CI grep): `grep -rn "hmac" packages/arcllm/src packages/arcteam/src`
  returns **only** legitimate retained uses (none in signing paths); arctrust
  `identity.py` KDF + `compare_digest` remain.
- GREEN: fix any stragglers. Document the retained-HMAC allowlist.

### T-14 — tier/config matrix integration (arcagent/arccli) · REQ-009
- RED: personal (`in_process`, `require_fips=false`) signs Ed25519 end-to-end;
  federal (`vault_transit`, `require_fips=true`) fails closed on a non-FIPS
  backend and, when FIPS + a validated module, signs by reference with no seed
  in-process.
- GREEN: wire `custody`/`signing_algorithm`/`require_fips` from config into the
  injected `Signer`; startup runs `assert_fips_if_required`.

---

## Definition of done

- REQ-001..012 satisfied; matrix rows all have passing tasks.
- No HMAC-signing symbol in arcllm/arcteam (T-13 green); retained-HMAC allowlist
  documented.
- `WormSink`, operator loader, arcllm request signer, arcteam audit chain all
  resolve through one arctrust `Signer`; `vault_transit` proves seed never
  materialises (T-03/T-07/T-14).
- One arctrust FIPS gate covers signing + encryption; `_trace_crypto` copy gone.
- arctrust imports none of arcagent/arcllm/arcrun/arcteam (NFR-2, review).
- `ruff` + `mypy --strict` clean; coverage gates met.
