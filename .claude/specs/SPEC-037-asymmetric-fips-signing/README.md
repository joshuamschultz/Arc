# SPEC-037 — Asymmetric + FIPS signing + out-of-process key custody

**Status:** COMPLETE
**Owner:** Josh (product owner)
**Depends on / references:** SPEC-053 (operator-key audit-authority independence — MERGED `4bb8f00`; leaves the vault seam + the `WormSink`-sign-callback follow-up explicitly to this spec), SPEC-034 (policy WORM chain), SPEC-033 (skill-improver WORM chain), SPEC-016/D-438/D-448 (trace-envelope encryption + the `assert_fips_provider_if_required` gate this spec generalises), commit `e63f3a8` (signed checkpoint anchor), ADR-019 (tier = stringency, not a gate).

---

## Why this spec exists

Two structural gaps remain in Arc's signing/attestation surface after SPEC-053:

**1. Symmetric HMAC cannot provide non-repudiation.** Two hot paths still sign
with a shared secret — anyone who can verify also holds the signing key, so a
"signature" proves nothing about *who* produced it (NIST AU-10):

| Path | File | What it protects | Today |
|------|------|------------------|-------|
| LLM request attestation | `arcllm/_signing.py` (`HmacSigner`), wired at `arcllm/modules/security.py:139,165` | provenance of an outbound LLM request | **HMAC-SHA256**; `ecdsa-p256` is a `raise …not yet fully implemented` stub |
| Inter-agent audit chain | `arcteam/audit.py` (`AuditLogger._compute_record_hmac`) | tamper-evidence of the messaging audit log | **chained HMAC-SHA256**, random session-key fallback |

Meanwhile arctrust is *already* fully Ed25519 (`keypair`, `audit.WormSink`,
`operator.OperatorKey`, `identity.AgentIdentity`) and arcteam's *message*
envelopes are already Ed25519-signed. So #1 is **arcllm + arcteam catch-up onto
arctrust primitives**, not new crypto.

**2. The operator audit seed still materialises in agent process memory — the
SPEC-053 CRITICAL residual.** SPEC-053's vault seam
(`OperatorKey.load(vault_resolver, vault_path)` and
`AgentIdentity._load_from_vault`) calls `resolve_secret(vault_path, id)` which
returns the **seed** and constructs the signer **in-process**. In-process code
(a compromised tool, a prompt-injected subprocess) can therefore dereference the
audit authority and forge the WORM chain. SPEC-053 REQ-021 and its SDD name the
fix explicitly and defer it here:

> "a follow-up may move `WormSink` to a sign-callback so the seed never
> materialises; noted as future work, not required here." — SPEC-053 SDD §HSM

This spec closes that composite by making signing **out-of-process** behind a
`Signer` seam: when a vault/transit backend is configured, the private key never
leaves the vault — Arc sends bytes and gets a signature back (Vault Transit /
PKCS#11 shape).

**3. Federal requires FIPS-validated crypto (SC-13 / IA-7).** The FIPS gate that
already exists for trace *encryption* (`_trace_crypto.assert_fips_provider_if_required`)
must be generalised to *signing* and made a startup floor at federal tier —
fail-closed unless the crypto backend is FIPS 140-3 validated and the selected
signing algorithm is FIPS-approved.

## The fix (tiered — ADR-019)

- **Every tier:** asymmetric signing everywhere. Delete the HMAC path outright
  (local-only repo — no compat shim). arctrust owns a `Signer` Protocol; arcllm
  and arcteam consume it instead of hand-rolling HMAC.
- **Personal / enterprise:** `InProcessSigner` — seed on `0600` disk (or dev
  env), Ed25519 via PyNaCl. Zero-config "easy button".
- **Enterprise / federal:** `VaultSigner` (Transit-style, out-of-process) — seed
  never materialises; + FIPS gate fails closed unless the backend is validated.
  Same `Signer` seam, config-selected (`custody = in_process | vault_transit`).

## Documents

| Doc | Purpose |
|-----|---------|
| [PRD.md](PRD.md) | Requirements — EARS, MoSCoW, pillar-tied acceptance, threat mapping |
| [SDD.md](SDD.md) | Components, concern boundaries, how #2 closes the SPEC-053 composite, Research Insights |
| [PLAN.md](PLAN.md) | TDD tasks, REQ→component→task traceability, HMAC-deletion migration |

## Concern boundaries (one-line summary)

- **arctrust** owns crypto primitives, the `Signer` Protocol + in-process/vault
  impls, key custody, and the generalised FIPS gate. Imports none of
  arcagent / arcllm / arcrun / arcteam.
- **arcllm** replaces its request-signing HMAC by consuming arctrust's `Signer`.
- **arcteam** replaces its audit-chain HMAC by consuming arctrust's `Signer`.
- **arcagent / arccli** select `custody` + `signing_algorithm` via config and
  inject the resolved `Signer` (WIRE, don't rebuild).

## Open questions for the product owner

1. **Ed25519 vs ECDSA-P256 at federal tier.** Ed25519 is approved in FIPS 186-5
   (2023), but PyNaCl/libsodium is not CMVP-validated and OpenSSL FIPS-provider
   Ed25519 validation is newer/patchier than its long-validated ECDSA P-256/P-384.
   Recommendation: **Ed25519 for personal/enterprise (via PyNaCl), ECDSA-P256 via
   PyCA `cryptography` for the FIPS/federal path** — algorithm chosen by the same
   `signing_algorithm` config. Confirm, or mandate ECDSA everywhere for one code
   path. (Full tradeoff in SDD Research Insights.)
2. **Real Vault/HSM in the target environment?** Should `VaultSigner` ship
   against a concrete backend (HashiCorp Vault Transit / PKCS#11 HSM), or ship as
   a **documented `VaultTransit` Protocol seam + a reference file-notary**
   out-of-process signer for CI/dev, leaving the real HSM binding to deployment?
   Recommendation: seam + reference file-notary now; concrete binding when a
   target HSM is named.
3. **OpenSSL FIPS provider supply/provenance.** Who supplies the CMVP-validated
   OpenSSL build on DOE machines (platform module vs Arc-vendored), and is that
   an accreditation-boundary artifact (SC-13/IA-7)? Confirm the provenance owner.
