# SPEC-037 — SDD

**Feature:** Asymmetric + FIPS signing + out-of-process key custody
**Status:** COMPLETE
**Pillars:** Simplicity → Modularity → Security → Scalability

---

## 1. Current state (investigation)

### Already asymmetric (Ed25519) — the norm in arctrust

| Component | File | Note |
|-----------|------|------|
| `keypair` (generate/sign/verify) | `arctrust/keypair.py` | PyNaCl Ed25519, canonical primitive; nothing else imports PyNaCl directly |
| `WormSink` | `arctrust/audit.py` | Ed25519-signed hash chain; takes `operator_private_key: bytes` |
| `OperatorKey` | `arctrust/operator.py` | Ed25519 audit authority; `load(vault_resolver, vault_path)` seam |
| `AgentIdentity` | `arctrust/identity.py` | Ed25519 `SigningKey`; `_load_from_vault` seam |
| arcteam **message** envelopes | `arcteam/types.py` | already `sig`/`signer_did`/`public_key`/`nonce` (Ed25519) |

### Symmetric HMAC still in use (the targets)

| Path | File / line | Protects | Replace with |
|------|-------------|----------|--------------|
| LLM request attestation | `arcllm/_signing.py` `HmacSigner`; wired `arcllm/modules/security.py:135-139,162-166` | provenance of an outbound request payload | arctrust `Signer` (Ed25519/ECDSA-P256). `ecdsa-p256` today is a `raise …not yet fully implemented` stub |
| Inter-agent **audit** chain | `arcteam/audit.py` `_compute_record_hmac`, `AuditLogger`, `ARCTEAM_HMAC_KEY` | tamper-evidence of the messaging audit log | arctrust `Signer` per-record Ed25519 signature |

### Legitimate HMAC — RETAINED, out of scope (REQ-012)

- `arctrust/identity.py:445-446` — HKDF-style child-key derivation (HMAC as a
  KDF, not a signature).
- `arctrust/identity.py:86`, `arcteam/audit.py` verify path — `hmac.compare_digest`
  constant-time comparison (not a signature).

### The vault seam today (the #2 gap)

`OperatorKey.load` and `AgentIdentity._load_from_vault` both call
`vault_resolver.resolve_secret(vault_path, id) -> str` returning a **hex seed**,
then construct the signer **in-process** (`OperatorKey(seed=…)` /
`SigningKey(bytes.fromhex(…))`). The seed therefore materialises in agent memory.
SPEC-053 REQ-021 + SDD (§HSM: "move `WormSink` to a sign-callback so the seed
never materialises; noted as future work") name this as SPEC-037's job.

### Crypto library / FIPS posture on this stack

- **arctrust:** `pynacl>=1.5,<2` — Ed25519 via bundled libsodium. **libsodium is
  not CMVP-validated.**
- **arcllm:** `cryptography>=46.0.7,<47` (PyCA) — AES-GCM for trace encryption.
  Already has `_trace_crypto.fips_provider_active()` (reads
  `backend._fips_enabled`; pyca/cryptography#7722 — no public API) and
  `assert_fips_provider_if_required(require_fips=…)` (SC-13 fail-closed). **This
  gate exists only for encryption today and lives in the wrong package** — it is
  generalised and moved to arctrust here.
- PyPI `cryptography` wheels bundle non-FIPS OpenSSL; a CMVP-validated OpenSSL
  provider must be supplied by the deployment (see Research Insights).

## 2. Target design

### 2.1 New: `arctrust/signer.py` — the `Signer` seam (owns #1 + #2)

```python
class Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...
    @property
    def algorithm(self) -> str: ...        # "ed25519" | "ecdsa-p256"
    def sign(self, message: bytes) -> bytes: ...

class InProcessSigner:                      # personal / enterprise / dev
    """Holds the seed; Ed25519 via arctrust.keypair, or ECDSA-P256 via PyCA."""
    def __init__(self, seed: bytes, algorithm: str = "ed25519") -> None: ...

class VaultSigner:                          # enterprise / federal (out-of-process)
    """Signs by reference — the seed NEVER enters this process (REQ-006)."""
    def __init__(self, transit: VaultTransit, key_ref: str) -> None: ...
    def sign(self, message: bytes) -> bytes:  # transit.sign(key_ref, message)

class VaultTransit(Protocol):               # the out-of-process boundary
    def sign(self, key_ref: str, message: bytes) -> bytes: ...
    def public_key(self, key_ref: str) -> bytes: ...

def build_signer(config: SignerConfig, *, vault_transit: VaultTransit | None) -> Signer:
    """custody=in_process → InProcessSigner(seed); custody=vault_transit → VaultSigner.
    Fail-closed: vault_transit custody with no transit client is an error, never
    a silent in-process fallback (NFR-3)."""
```

- **How #2 closes the SPEC-053 composite:** `VaultSigner.sign` calls
  `transit.sign(key_ref, message)` — the seed lives behind the transit boundary
  (Vault Transit / PKCS#11 HSM) and is never returned. `resolve_secret`
  (seed-fetch) is used ONLY under `custody=in_process`. `WormSink`,
  `OperatorKey`-backed sinks, the arcllm request signer, and the arcteam audit
  chain all take a `Signer` — so under `vault_transit` **no Arc process ever
  holds the operator seed**, which is exactly the in-process-seed residual
  SPEC-053 could not close.
- **Reference file-notary** (Open Question 2): a `FileNotaryTransit`
  implementing `VaultTransit` by talking to a separate local signing process
  over a socket/file — a real out-of-process signer for CI/dev without a
  production HSM. The concrete HashiCorp/PKCS#11 binding is a deployment adapter,
  not core.

### 2.2 Refactor: `arctrust/audit.py` `WormSink`

`operator_private_key: bytes` → `signer: Signer`. Internally
`sign(event_hash) = signer.sign(event_hash.encode())`; `public_key =
signer.public_key`; `algorithm` recorded per record for verify-time dispatch.
`OperatorKey` gains `.into_signer(...)` (in-process) and the operator loader
gains a `vault_transit` path that returns a `VaultSigner` instead of a seed.
`verify_chain` unchanged in shape (REQ-003) — it already takes a public key.

### 2.3 New: `arctrust/fips.py` — generalised FIPS gate (owns #3)

Move `fips_provider_active()` + `assert_fips_provider_if_required()` out of
`arcllm/_trace_crypto.py` into arctrust; extend to cover **signing**:

```python
def fips_backend_active() -> bool: ...       # reads PyCA OpenSSL _fips_enabled
def algorithm_is_fips_approved(algorithm: str) -> bool: ...   # ecdsa-p256 ✓; ed25519 iff validated
def assert_fips_if_required(*, require_fips: bool, algorithm: str) -> None:
    """Federal startup floor (SC-13/IA-7). Raises ArcTrustFipsError fail-closed."""
```

`_trace_crypto` imports the arctrust gate (single source of truth); the arcllm
copy is deleted. Under `require_fips`, `build_signer` must select a FIPS
backend: Ed25519 only if the loaded module exposes a validated Ed25519, else the
gate rejects and the operator must select `ecdsa-p256` (PyCA against FIPS
OpenSSL).

### 2.4 arcllm & arcteam consumers

- `arcllm/_signing.py`: delete `HmacSigner` + `hmac`/`hashlib`; `create_signer`
  returns an arctrust `Signer` (`ed25519`/`ecdsa-p256`); `canonical_payload`
  unchanged. `modules/security.py` attaches `signature` + `algorithm` +
  `public_key`/`key_ref` (REQ-011: label from config `model_name`).
- `arcteam/audit.py`: `AuditLogger(signer: Signer)`; per-record signature over
  `prev_hash || canonical(record)`; `verify_chain` verifies signatures; drop
  `hmac_sha256`/`ARCTEAM_HMAC_KEY`; `AuditRecord` gains `signature` + `key_ref`.

## 3. Concern boundaries (explicit)

| Package | Owns | Must NOT |
|---------|------|----------|
| **arctrust** | `Signer` Protocol + `InProcessSigner`/`VaultSigner`, `VaultTransit` boundary + reference `FileNotaryTransit`, `WormSink`/operator custody refactor, `arctrust/fips.py` gate, key custody | import arcagent / arcllm / arcrun / arcteam |
| **arcllm** | replace its own request-signing HMAC by consuming arctrust `Signer`; import (not redefine) the FIPS gate | define its own signing primitives or FIPS logic |
| **arcteam** | replace its audit-chain HMAC by consuming arctrust `Signer` | define signing primitives |
| **arcagent / arccli** | select `custody`/`signing_algorithm`/`require_fips` from config; inject the resolved `Signer`/`VaultTransit` | hold the seed under `vault_transit` |

Tier = stringency (ADR-019): asymmetric signing at all tiers; `vault_transit`
custody + FIPS gate are federal floors, config-selected on the same seam.

## 4. Data / config

`arcagent.toml` (security/policy section) and `arcllm/config.toml`:

```toml
signing_algorithm = "ed25519"     # was "hmac-sha256"; ed25519 | ecdsa-p256
custody           = "in_process"  # in_process | vault_transit
require_fips      = false         # federal → true (startup floor)
# vault_transit only:
vault_transit_addr = ""
key_ref            = ""
```

## 5. Migration (HMAC deletion — replace outright, no shim)

1. Land `arctrust/signer.py` + `arctrust/fips.py` (+ tests) — additive.
2. Refactor `WormSink`/operator loader to `Signer` (arctrust callers updated in
   the same edit).
3. Swap arcllm `_signing.py` to arctrust `Signer`; **delete** `HmacSigner`,
   `hmac`, `hashlib`; update `security.py` + `config.toml` default; update
   `test_signing.py`/`test_security.py`/`test_coverage_gaps.py`.
4. Swap arcteam `audit.py` to `Signer`; **delete** `_compute_record_hmac`,
   `hmac_sha256`, `ARCTEAM_HMAC_KEY`; update `AuditRecord` + tests.
5. Delete `_trace_crypto`'s FIPS functions; import arctrust's.

No `hmac`-signing symbol survives in arcllm/arcteam after step 5 (grep-gated in
PLAN). Legitimate HMAC (KDF, `compare_digest`) stays (REQ-012).

## 6. Research Insights (deepen enrichment)

### 6.1 FIPS 140-3 validated crypto for Python

- **PyNaCl / libsodium is not CMVP-validated** and there is no FIPS validation
  path for it — so Arc's Ed25519 (PyNaCl) is fine for personal/enterprise but
  cannot satisfy SC-13 at federal tier. (libsodium FAQ; NIST CMVP search shows
  no libsodium certificate.)
- **PyCA `cryptography`** delegates to OpenSSL; FIPS status depends entirely on
  the *linked* OpenSSL. PyPI wheels bundle a non-FIPS OpenSSL. Federal must run
  `cryptography` against a **CMVP-validated OpenSSL 3.x FIPS provider** (OpenSSL
  FIPS module cert; `fips=yes` in `openssl.cnf` / `openssl fipsinstall`).
  There is **no public `cryptography` API** to query FIPS status
  (pyca/cryptography #7722); Arc reads the internal `backend._fips_enabled`
  flag, already done in `_trace_crypto.fips_provider_active()`.
- **Alternatives** the ISSO may already mandate: **wolfCrypt-py / wolfCrypt FIPS**
  (CMVP-validated module with Python bindings) and platform modules (RHEL system
  crypto policy `FIPS`, which forces OpenSSL into the validated provider). These
  are deployment choices; Arc's job is the fail-closed **gate**, not shipping the
  module.
- *Design consequence:* keep the gate at the boundary (startup), keep the
  primitive pluggable (`Signer.algorithm`), let the deployment supply the
  validated module. Fail closed, never downgrade.

### 6.2 Out-of-process signing — Vault Transit / PKCS#11 HSM

- **HashiCorp Vault Transit** ("cryptography as a service") signs/HMACs/encrypts
  with keys that are **non-exportable**: the caller sends bytes to
  `transit/sign/<key>` and receives a signature; the private key never leaves
  Vault. This is the exact shape of `VaultSigner`/`VaultTransit`. Vault Transit
  can itself be backed by an HSM seal.
- **PKCS#11 / KMIP HSM** — the FIPS 140-2/3 Level 3 answer: keys are generated in
  and never leave the HSM; `C_Sign` is an oracle call. `python-pkcs11` or a
  vendor PKCS#11 lib implements `VaultTransit.sign` against `C_Sign`.
- **Cloud KMS** (AWS KMS / GCP KMS / Azure Key Vault) offer non-exportable
  asymmetric keys with a `Sign` API — same by-reference pattern, FIPS 140-3
  validated HSMs behind them.
- *Why this closes the composite:* the SPEC-053 forgery requires reading the
  operator seed from the agent process. Under sign-by-reference the seed is
  never in the process, so a compromised in-process actor can request signatures
  only for messages it can already produce — it cannot *rewrite* history without
  the vault also honouring each forged record, which the vault's own audit and
  (federal) the external witness expose. The `Signer` seam + `WormSink`
  sign-callback is the minimal change that makes this possible without touching
  the record format.

### 6.3 Ed25519 (FIPS 186-5) vs ECDSA-P256 — the tradeoff

- **Ed25519 is FIPS-approved as of FIPS 186-5 (Feb 2023)** (EdDSA with edwards25519).
  But approval-in-standard ≠ available-validated-module: CMVP module validations
  that expose EdDSA are **newer and less ubiquitous** than ECDSA. OpenSSL 3.0's
  first FIPS provider validation (cert #4282) did **not** include EdDSA in its
  approved algorithm set; EdDSA in the FIPS provider is a later-3.x addition and
  depends on which validated build the deployment has.
- **ECDSA P-256 / P-384** has been FIPS-approved and CMVP-validated for a decade+
  and is present in essentially every FIPS OpenSSL/HSM. It is the **lower-risk
  federal choice today**, at the cost of non-deterministic nonces (mitigated by
  RFC 6979 deterministic ECDSA where the module supports it) and slightly more
  complex signing.
- **Recommendation:** keep Ed25519 as the personal/enterprise default (fast,
  deterministic, misuse-resistant, already the arctrust norm); select
  **ECDSA-P256 via PyCA `cryptography` for the FIPS/federal path** unless the
  target deployment's validated module certifies Ed25519. Both live behind
  `Signer.algorithm`; the FIPS gate rejects Ed25519 when the loaded module
  doesn't expose a validated EdDSA. This is Open Question 1 for the product
  owner.
- *Citations:* NIST FIPS 186-5 (2023) §7 EdDSA; NIST SP 800-186; CMVP cert #4282
  (OpenSSL 3.0 FIPS provider) approved-algorithm list; RFC 8032 (EdDSA);
  RFC 6979 (deterministic ECDSA); FIPS 140-3.

### 6.4 OpenSSL FIPS provider provenance (SC-13 / IA-7)

- The CMVP-validated artifact is the **OpenSSL FIPS provider (`fips.so` +
  `fipsmodule.cnf`)** produced by `openssl fipsinstall`, tied to a specific
  source build and cert number. On DOE machines this is typically the
  **platform-supplied** validated module (e.g. RHEL in FIPS mode), not one Arc
  vendors — Arc's `cryptography` must link the system OpenSSL, not a bundled
  wheel.
- **Provenance chain to document (accreditation boundary):** CMVP certificate
  number → validated OpenSSL source/version → `fipsmodule.cnf` integrity
  (self-test on load) → `openssl.cnf` activating the fips provider → the runtime
  self-check Arc performs at startup (`fips_backend_active()`). IA-7 requires
  the crypto module to authenticate; SC-13 requires FIPS-validated crypto for
  the protected function (here, audit signing).
- *Design consequence:* Arc **documents and gates**, deployment **supplies**.
  The gate's fail-closed startup self-check is the enforcement point; the
  provenance doc is an SC-13/IA-7 deliverable, not code.
- *Citations:* NIST 800-53 Rev.5 SC-13, IA-7, SC-12, IA-5(2), AU-9(3), AU-10;
  OpenSSL 3.x FIPS module documentation (`fips_module.html`, `fipsinstall`);
  NIST CMVP program.
