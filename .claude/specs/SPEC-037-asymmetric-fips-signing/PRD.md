# SPEC-037 — PRD

**Feature:** Asymmetric + FIPS signing + out-of-process key custody
**Status:** COMPLETE
**Pillars (principled-coder order):** Simplicity → Modularity → Security → Scalability

---

## 1. Problem & goal

Arc's attestation surface still contains two symmetric-HMAC signing paths
(`arcllm/_signing.py`, `arcteam/audit.py`) that structurally cannot provide
non-repudiation, and its strongest audit authority (the operator seed) still
materialises in agent process memory even when a vault is configured — the
CRITICAL residual SPEC-053 explicitly deferred here. Federal deployments also
require FIPS-validated crypto for any signing (SC-13/IA-7), which Arc does not
yet gate on.

**Goal:** every signature Arc emits is asymmetric (non-repudiable); the private
key can sign **out-of-process** so it never materialises in the agent (closing
the SPEC-053 audit-forgery composite); and federal tier **fails closed** unless
the crypto backend and algorithm are FIPS-approved — all behind one `Signer`
seam owned by arctrust, tier-selected by config (ADR-019).

## 2. Users & context

- **Federal operator / ISSO** — needs AU-10 non-repudiation and SC-13 FIPS
  crypto to authorize under NIST 800-53 / FedRAMP; needs assurance the audit
  authority cannot be forged by a compromised agent process.
- **Enterprise operator** — wants vault-custodied signing keys (no seed on disk)
  without a full federal accreditation.
- **Personal / single-user** — wants the zero-config "easy button": asymmetric
  signing with an auto-generated on-disk key, no vault required.

## 3. Threat mapping

| Threat | Relevance | Requirement |
|--------|-----------|-------------|
| **AU-10 non-repudiation** | HMAC verifier holds the signing key → no proof of origin | REQ-001, REQ-002, REQ-003 |
| **ASI04 Agentic Supply Chain** / **LLM03 Supply Chain** | signed artifacts must be verifiable to a key the signer alone holds | REQ-001, REQ-004 |
| **AU-9 audit repudiation** (SPEC-053 residual) | in-process seed → compromised process forges WORM chain | REQ-005, REQ-006 (out-of-process signing) |
| **SC-13 Cryptographic Protection / IA-7 Crypto Module Auth** | federal must use FIPS 140-3 validated crypto | REQ-008, REQ-009 |
| **SC-12 / IA-5(2) key custody** | private key lifetime/location | REQ-005, REQ-007 |
| **LLM07 System Prompt / secret leakage** | no signing secret reachable from agent memory when vault configured | REQ-006 |

## 4. Requirements (EARS · MoSCoW · pillar-tied acceptance)

### Asymmetric signing (replace HMAC) — #1

**REQ-001 (Must)** — WHERE request signing is enabled, the arcllm security module
SHALL sign the canonical request payload with an **asymmetric** `Signer`
(Ed25519 or ECDSA-P256) resolved from arctrust, and SHALL NOT use HMAC.
- *Acceptance (Security):* a signature verifies with only the public key; the
  verifier never possesses signing material. Verified by a test that verifies
  with the public key and asserts the private key is absent from the verifier.
- *Acceptance (Simplicity):* `arcllm/_signing.py`'s `HmacSigner` and the
  `hmac`/`hashlib` imports are **deleted**, not deprecated.

**REQ-002 (Must)** — THE arcteam `AuditLogger` SHALL chain and sign each audit
record with an asymmetric `Signer` (per-record Ed25519 signature over
`prev_hash || record`), replacing the chained HMAC.
- *Acceptance (Security):* `verify_chain()` verifies each record's signature
  against the operator public key; tamper of any record or signature fails
  verification. Verified by a mutation test.
- *Acceptance (Simplicity):* `_compute_record_hmac`, the `hmac_sha256` field,
  and `ARCTEAM_HMAC_KEY` are removed; `AuditRecord` carries `signature` +
  `public_key`/`key_ref` instead.

**REQ-003 (Must)** — THE verification interfaces (verify-a-request,
`verify_chain`) SHALL keep stable call shapes so existing callers/UI need no
signature-scheme awareness beyond supplying a public key.
- *Acceptance (Modularity):* callers of `verify_chain` compile unchanged; only
  the key material type (public key vs shared secret) differs. Verified by
  caller-site review.

**REQ-004 (Should)** — THE arcllm signer SHALL support both `ed25519` and
`ecdsa-p256` selected by the existing `signing_algorithm` config key; the
default config SHALL be `ed25519` (asymmetric), never `hmac-sha256`.
- *Acceptance (Modularity):* switching algorithm is a config change, no code
  branch at the call site. Verified by a parametrised test over both algorithms.

### Out-of-process key custody — #2 (closes the SPEC-053 composite)

**REQ-005 (Must)** — arctrust SHALL define a `Signer` Protocol
(`public_key: bytes`, `algorithm: str`, `sign(message: bytes) -> bytes`) with an
**in-process** implementation (holds the seed) and a **vault/transit-backed**
implementation (signs by reference). All WORM/attestation signers
(`WormSink`, operator-key sinks, arcllm request signer, arcteam audit chain)
SHALL resolve through a `Signer`, not a raw seed.
- *Acceptance (Modularity):* `WormSink.__init__` takes `signer: Signer` in place
  of `operator_private_key: bytes`; no WORM caller constructs a keypair from a
  raw seed. Verified by signature review + a fake `Signer` in tests.

**REQ-006 (Must)** — WHERE `custody = vault_transit` is configured, signing SHALL
occur via the vault/transit API such that the private seed **NEVER materialises
in the agent process** (Arc sends bytes, receives a signature); no code path
SHALL call `resolve_secret`/`from_seed` to obtain the seed under this custody
mode.
- *Acceptance (Security):* a memory/seam test proves the seed bytes are never
  returned to the agent under `vault_transit`; the transit client receives the
  message and returns a signature. This is the concrete closure of SPEC-053's
  in-process-seed residual. Verified by a `VaultTransit` fake that raises if the
  seed is ever requested.

**REQ-007 (Should)** — THE in-process→vault switch SHALL be the *same* `Signer`
seam selected by config; personal SHALL default to `in_process` (on-disk `0600`
key, zero-config), enterprise/federal to `vault_transit`.
- *Acceptance (Simplicity):* one config key (`custody`) selects the impl; no
  duplicate signing code per tier. Verified by config-matrix test.

### FIPS gate + OpenSSL supply — #3

**REQ-008 (Must)** — WHEN the deployment tier is federal (or `require_fips=true`),
THE system SHALL run a **startup gate** that fails closed unless the loaded
crypto backend is FIPS 140-3 validated AND the selected `signing_algorithm` is
FIPS-approved for that backend.
- *Acceptance (Security):* startup raises `ArcTrustFipsError` when
  `require_fips` and the backend is non-validated; personal/enterprise proceed.
  Verified by a test that forces the non-FIPS backend flag under `require_fips`.
- *Acceptance (Simplicity):* the existing `_trace_crypto.assert_fips_provider_if_required`
  is generalised into one arctrust gate covering signing **and** encryption; the
  arcllm copy is removed (single source of truth).

**REQ-009 (Must)** — THE FIPS gate SHALL treat tier as **stringency metadata, not
a feature gate** (ADR-019): asymmetric signing runs at every tier; only the
FIPS requirement and vault custody are federal *floors*, toggled by config.
- *Acceptance (Modularity):* personal tier with `require_fips=false` runs
  Ed25519/PyNaCl unchanged; the same code at federal adds the gate. Verified by
  tier-matrix test.

**REQ-010 (Should)** — THE spec SHALL document the OpenSSL FIPS provider
supply/provenance path (who supplies the CMVP-validated module, how it is bound
to PyCA `cryptography`) as an SC-13/IA-7 deployment artifact.
- *Acceptance (Security):* SDD Research Insights records the provenance path and
  citations. Verified by SDD review.

### Tie-off — #4

**REQ-011 (Should)** — THE provider/model label bound into the signed canonical
payload SHALL originate from the trusted provider configuration, not from any
attacker-suppliable response field (SPEC-034 review item).
- *Acceptance (Security):* the label fed to `canonical_payload` is the
  config-resolved `model_name`, asserted by test. If already satisfied, record
  as confirmed-covered.

**REQ-012 (Must / negative)** — Legitimate non-signing HMAC uses
(`arctrust/identity.py` HKDF child-key derivation; `hmac.compare_digest`
constant-time compares) SHALL remain and SHALL be explicitly out of scope — they
are not signatures and provide no non-repudiation claim to break.
- *Acceptance (Correctness):* SDD lists these as intentionally retained.

## 5. MoSCoW summary

| Priority | Requirements |
|----------|--------------|
| **Must** | REQ-001, REQ-002, REQ-003, REQ-005, REQ-006, REQ-008, REQ-009, REQ-012 |
| **Should** | REQ-004, REQ-007, REQ-010, REQ-011 |
| **Could** | Operator-key/algorithm rotation keyring (key-id per signature); PKCS#11 HSM concrete binding |
| **Won't (this spec)** | Building a production HashiCorp Vault / cloud-KMS deployment; changing the WORM record schema beyond signer fields; witness-anchor logic (SPEC-053, reused as-is); replacing AES-GCM trace encryption |

## 6. Non-functional requirements

- **NFR-1 (Simplicity):** net LOC bounded; deleting HMAC offsets the `Signer`
  seam. No new top-level abstraction beyond the `Signer` + `VaultTransit`
  protocols.
- **NFR-2 (Modularity):** arctrust imports none of arcagent/arcllm/arcrun/arcteam.
- **NFR-3 (Security):** fail-closed on FIPS gate and on any vault-transit error
  (no silent fallback to in-process signing).
- **NFR-4 (Scalability):** `VaultSigner` reuses a pooled transit client;
  per-signature latency bounded by one transit round-trip; the encryption-off /
  signing-off paths pay nothing (lazy import preserved).

## 7. Out of scope

Production vault/HSM provisioning; NATS/mTLS transport; SPEC-035 in-process
transport confinement (complementary); AES-GCM trace-envelope crypto (only its
FIPS gate is generalised).
