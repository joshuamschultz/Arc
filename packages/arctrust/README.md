<div align="center">

# 🪪 arctrust

### **The Cryptographic Foundation for Arc**
*Identity · Signing · Audit · Policy — the leaf every other Arc package depends on.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-470%2B-0055BC.svg)](#status)
[![Coverage](https://img.shields.io/badge/coverage-99%25-003B82.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-0073FE.svg)](#status)
[![Ed25519](https://img.shields.io/badge/crypto-Ed25519-F68D2E.svg)](#cryptography)

</div>

---

## ✨ What is arctrust?

`arctrust` is the cryptographic floor of the Arc stack. Every other Arc package depends on it — `arctrust` itself imports **no Arc package**, resting only on PyNaCl (libsodium), Pydantic, PyCA `cryptography` (ECDSA-P256 + the FIPS probe), and tomlkit.

It gives you the four pillars every secure agent needs (ADR-019):

- 🪪 **Identity** — Ed25519 keypairs and DIDs (`did:arc:{org}:{type}/{hash}`), plus human user identities and the operator audit key
- ✍️ **Sign** — sign and verify arbitrary bytes and artifacts through one `Signer` seam (in-process or vault/HSM custody)
- ✅ **Authorize** — a deny-by-default, fail-closed `PolicyPipeline` that decides whether a tool call is allowed
- 📜 **Audit** — structured events written to a durable, hash-chained, tamper-evident WORM sink

Around those pillars it also carries the shared crypto-adjacent primitives every layer needs and none may reimplement: the **one Arc-home path resolver** (`arctrust.paths`), **PII/secret redaction**, **at-rest audit sealing**, canonical JSON, classification, and FIPS gating.

If you're building anything that needs to *prove* what happened, this is where you start.

> 📖 **How a capability becomes allowed or blocked** — the end-to-end trust/capability
> pipeline (per-tier AST import gate → signing → TOFU → operator approval) is documented
> in [`docs/trust-model.md`](docs/trust-model.md).

---

## ⭐ Top Features

What makes `arctrust` the cryptographic foundation for accountable agents:

### **The Four Pillars (Built-In)**
- **DID-required identity** — Every agent, user, and operator has a `did:arc:{org}:{type}/{hash}` identity; no anonymous operations
- **Deny-by-default policy** — `PolicyPipeline` blocks everything not explicitly allowed; fail-closed security
- **Tamper-evident audit chains** — Hash-chained WORM records detect any modification; operators can verify integrity
- **Ed25519 signing everywhere** — One `Signer` seam for all cryptographic operations; supports in-process or vault/HSM custody

### **Shared Primitives**
- **PII/secret redaction** — Built-in patterns for sanitizing sensitive data; prevents credential leakage to LLMs
- **Canonical JSON** — Deterministic serialization for signing; prevents signature malleability attacks
- **FIPS gating** — Automatic detection of FIPS-compliant environments; enforces cryptographic standards

### **Tier-Aware Security**
- **Per-tier policy enforcement** — Personal, Enterprise, Federal tiers with increasing strictness; federal refuses unsigned code
- **Operator audit key** — Separate signing authority for audit events; agents cannot forge their own trails
- **TOFU with approval** — Trust-on-first-use requires operator approval at enterprise/federal tiers

### **Cryptographic Primitives**
- **Ed25519 DIDs** — `did:arc:{org}:{type}/{hash}` identity format; cryptographically verifiable
- **ECDSA-P256 + FIPS probe** — FIPS-compliant signatures when required; automatic detection
- **Canonical JSON serialization** — Deterministic encoding for signing; prevents signature malleability

### **Policy Engine**
- **Deny-by-default** — `PolicyPipeline` blocks everything not explicitly allowed
- **Fail-closed** — Security failures block operations; never degrade to insecure state
- **Classification labels** — Tag data with sensitivity levels; enforce no-read-up policy

### **Audit Infrastructure**
- **WORM sink** — Write-once-read-many audit log; tamper evidence built in
- **Hash-chained events** — Every event includes previous hash; detect any modification
- **Operator-signed chains** — Audit events signed by operator key, not agent's DID

---

## 🏗️ Where It Fits

```mermaid
flowchart TB
    classDef found fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef llm fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef agent fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef entry fill:#D6E6FF,stroke:#0073FE,color:#002550

    arctrust[arctrust<br/>identity · sign · audit · policy]:::found
    arcstore[arcstore]:::found
    arcrun[arcrun]:::runtime
    arcagent[arcagent]:::agent
    arcskill[arcskill]:::agent
    arcgateway[arcgateway]:::surface
    arccli[arccli]:::entry

    arcstore --> arctrust
    arcrun --> arctrust
    arcagent --> arctrust
    arcskill --> arctrust
    arcgateway --> arctrust
    arccli --> arctrust
```

`arctrust` is the **leaf node** — it imports nothing from Arc, and every other Arc package imports something from it.

---

## 🚀 Install

```bash
pip install arctrust          # standalone
# or
pip install arcmas            # full Arc stack
```

---

## 🧪 Quick Example

```python
from arctrust import AgentIdentity, emit, AuditEvent, WormSink, generate_keypair
from pathlib import Path

# 1. Generate a fresh agent identity
identity = AgentIdentity.generate(org="acme", agent_type="analyst")
print(identity.did)
# → did:arc:acme:analyst/a3f2c1...

# 2. Sign a message
msg = b"tool_call:read_file:/workspace/report.txt"
signature = identity.sign(msg)
assert identity.verify(msg, signature)

# 3. Emit to the durable, tamper-evident audit log (signed hash chain on disk).
# The chain is signed by the OPERATOR's key, never the agent's own DID key —
# an agent can never forge or silently rewrite its own audit trail (SPEC-053).
from arctrust import InProcessSigner

operator = generate_keypair()
operator_signer = InProcessSigner(operator.private_key)
sink = WormSink(Path("/var/log/arc/audit-chain.jsonl"), operator_signer)
emit(
    AuditEvent(
        actor_did=identity.did,
        action="tool.call",
        target="/workspace/report.txt",
        outcome="allow",
    ),
    sink,
)

# 4. Later — verify the chain survived restart and was not tampered with
assert sink.verify_chain()
```

---

## 🧩 What's Inside

### Identity (`arctrust.identity`)

| Symbol | What It Does |
|---|---|
| `AgentIdentity` | An Ed25519 keypair plus a `did:arc:{org}:{type}/{hash}` DID. Generates, persists, loads, signs, verifies |
| `ChildIdentity` | Ephemeral identity for spawned subagents. Derived deterministically from a parent via HKDF-SHA256 — no fresh randomness required, fully reproducible |
| `derive_child_identity` | Derive a child identity given a parent identity + context label |
| `generate_did` · `parse_did` · `validate_did` | DID string handling |

**Why HKDF-derived child identities matter:** when an agent spawns a subagent, you want the subagent to have its own DID (so its actions are attributable separately) without a key-distribution problem. HKDF lets the parent derive the child's keypair on-demand from a single secret, with a context label that prevents collisions.

### Cryptography (`arctrust.keypair`)

| Symbol | What It Does |
|---|---|
| `KeyPair` | Wraps a libsodium Ed25519 keypair |
| `generate_keypair` | New random keypair |
| `sign(message, secret_key)` | 64-byte Ed25519 signature |
| `verify(message, signature, public_key)` | Returns `bool`. Never raises. Constant-time |

Powered by **PyNaCl → libsodium**. Same primitive you'd find in WireGuard, age, OpenSSH-Ed25519. FIPS-validated builds available for federal deployments.

### Signing (`arctrust.signer`)

The asymmetric signing seam behind every non-repudiable signature Arc emits — WORM audit chains, artifact signatures, arcllm request signing, arcteam audit chains (SPEC-037).

| Symbol | What It Does |
|---|---|
| `Signer` (Protocol) | `public_key`, `algorithm`, `sign(message)` — the one seam every signature resolves through |
| `InProcessSigner` | Holds the private seed in memory. **Ed25519** (personal/enterprise default) or **ECDSA-P256** (FIPS/federal, via PyCA `cryptography`) |
| `VaultSigner` | Signs **by reference** — the seed never enters this process. Hands the message to a `VaultTransit` boundary and gets back a signature |
| `VaultTransit` (Protocol) | The out-of-process boundary: real deployments point this at HashiCorp Vault Transit, a PKCS#11 HSM, or a cloud KMS |
| `FileNotaryTransit` | The **local dev/test reference implementation** of `VaultTransit` — not a production signing backend. Swap in a real Vault/HSM/KMS client for production `vault_transit` custody |
| `build_signer(...)` | Factory — fails closed: `vault_transit` custody with no transit client configured is a hard error, never a silent fall-back to in-process signing |

Tier is stringency metadata, not a different code path: the same `Signer` seam runs at every tier — `custody` (`in_process` / `vault_transit`) and `algorithm` (`ed25519` / `ecdsa-p256`) are config-selected. Federal forces `algorithm=ecdsa-p256` + `custody=vault_transit`; personal defaults to `ed25519` + `in_process`.

### Audit (`arctrust.audit`)

| Symbol | What It Does |
|---|---|
| `AuditEvent` | Structured event: `event_type`, `actor_did`, `action`, `target`, `outcome`, `ts`, `metadata` |
| `AuditSink` (Protocol) | Anything that knows how to write events |
| `WormSink` | The durable system of record: an append-only, asymmetrically-signed SHA-256 hash chain on a `0600` file, signed by an `arctrust.signer.Signer` (Ed25519 or ECDSA-P256; in-process or vault-transit custody). **Tamper-evident** (flip one byte → verify fails), **restart-safe** (tip restored from the file tail), **single-writer** (`flock`), **crash-recoverable** (torn-tail truncate + signed recovery record), and rotates to bounded segments. Replaces the old unchained `JsonlSink` and the in-memory-only `SignedChainSink` |
| `verify_chain(path, public_key)` | Lock-free read-path verifier: streams every segment, checking hash links, signatures (AU-10), `seq` contiguity, and the genesis anchor. Powers `arc store verify` |
| `NullSink` | For tests |
| `emit(event, sink)` | Single emission point. Swallows sink failures (NIST AU-5) — a broken sink can never crash the agent |

**Audit-authority independence (SPEC-053):** the signer handed to `WormSink` is the **operator's** identity, never the agent's own DID key. An agent cannot forge, backdate, or silently rewrite its own audit trail — only an entity holding the operator key (or, at federal, an external witness anchor) can produce a valid chain signature.

### Policy (`arctrust.policy`)

| Symbol | What It Does |
|---|---|
| `PolicyPipeline` | Ordered, fail-closed evaluator. First DENY wins. Sub-1 ms p95 with LRU caching |
| `PolicyLayer` (Protocol) | A single policy stage. Takes `ToolCall` + `PolicyContext`, returns `Decision` (ALLOW / DENY / ABSTAIN) |
| `Decision` | `verdict` (ALLOW/DENY/ABSTAIN), `reason`, `policy_id`, `metadata` |
| `ToolCall` | `tool_name`, `args`, `caller_did`, `classification` |
| `PolicyContext` | Tier, tenant ID, run ID, timestamp, agent metadata |
| `build_pipeline(tier)` | Convenience builder — returns the right `PolicyPipeline` for a tier |

**Layer composition by tier:**

| Tier | Global | Provider | Agent | Team | Sandbox |
|---|---|---|---|---|---|
| Personal | ✅ | — | — | — | — |
| Enterprise | ✅ | ✅ | ✅ | — | — |
| Federal | ✅ | ✅ | ✅ | ✅ | ✅ |

**The Lethal Trifecta (`GlobalLayer` forbidden composition):**

Private data + external comms + untrusted input must never co-occur in one
session without human approval — Simon Willison's *lethal trifecta*, the
exfiltration path where untrusted content instructs an agent to send private
data to an adversary. The `GlobalLayer` denies a call whose session-accumulated
capability legs complete the forbidden set and routes it to a signed operator
approval instead of letting it through.

Each leg is a **context-resolved definition** — it fires on the *real* condition
evaluated per call, never on a static label. That is what keeps the gate honest:
stringency lives in the *definition* of each leg, never in disabling the gate.

| Leg | Fires on | Exempt when |
|---|---|---|
| `private_data` | reading **anything on the machine** — workspace files, `.env`, tomls, traces, agent files, memory, profile, recall | never — it is all private from the outside world |
| `external_comms` | outbound communication to a **non-owner** | the counterparty is the **paired owner** (arcui / Telegram / Slack / messaging) — talking to yourself is not exfiltration |
| `untrusted_input` | ingesting **unvetted** content | the source is **operator-vetted** — an allowlisted/trusted URL is not untrusted |

**Tier sets the trust default — not whether the gate exists:**

| Tier | Web-content trust | Effect |
|---|---|---|
| Personal | Trusted by default (denylist) | `untrusted_input` rarely present → agents research freely; the gate fires only on denylisted content + private data + non-owner egress |
| Federal | Untrusted by default (allowlist) | any unvetted fetch is `untrusted_input` → the gate fires whenever an agent egresses to a non-owner after touching unvetted content + private data |

A completed trifecta always requires a human. Approvals are operator-signed
(never the agent's own DID) and reachable via arcui, arccli, or the owner's
paired channel. Owner-identity pairing and per-tier URL trust resolution are
formalized in the arctrust user-identity spec.

### Artifact Signing (`arctrust.artifact`)

| Symbol | What It Does |
|---|---|
| `content_sha256(content)` | Returns the `sha256:<hex>` digest of `content` |
| `sign_artifact(content, signer_did, private_key)` | Signs `content` with an Ed25519 seed under `signer_did`; returns an `ArtifactSignature` |
| `sign_artifact_with_signer(content, signer_did, signer)` | Same, through the `Signer` seam — so artifact signing can use vault/HSM custody instead of a raw in-process seed |
| `verify_artifact(content, manifest, trusted_public_key=None)` | Re-verifies `content` against its `ArtifactSignature` at load time. Never raises — any malformed field, digest mismatch, or (when pinned) key mismatch is `False` |
| `ArtifactSignature` | Frozen Pydantic model serialisable to a `.arcsig` sidecar (`to_json`/`from_json`): content digest, signer DID, signer public key, signature, algorithm, timestamp |

Detached content-hash + Ed25519 signing for arbitrary bytes — the primitive behind arcagent's Sign-pillar enforcement on agent-authored capabilities (SPEC-033): sign on write, re-verify at load, independent of any install-time check.

**Honest semantics:** a valid signature proves the bytes are *unmodified since the signer wrote them* and *attributed* to the signer's DID key. It does **not** prove the content is safe — a compromised signer produces a perfectly valid signature over malicious bytes. Safety belongs to the caller's TOFU gate and execution sandbox, never to this primitive.

### Canonical Serialization (`arctrust.canonical`)

| Symbol | What It Does |
|---|---|
| `canonical_json(obj) -> bytes` | Deterministic canonical-JSON bytes a signature binds to: `sort_keys=True`, compact separators, `ensure_ascii=True`. Input must be JSON-serialisable with the stdlib encoder — no silent `default=` coercion |

The one serializer every signing package reuses (arcllm request signing, arcagent checkpoint
signing) instead of hand-rolling per-package JSON serialization — a compact-vs-default
separator or `ensure_ascii` mismatch would silently diverge the bytes and break
cross-package signature verification. A byte-identity test proves every adopter agrees.

### Trust Store (`arctrust.trust_store`)

| Symbol | What It Does |
|---|---|
| `load_operator_pubkey(name)` | Load operator Ed25519 pubkey from `~/.arc/trust/operators/{name}.pub` |
| `load_issuer_pubkey(name)` | Load skill-bundle issuer pubkey from `~/.arc/trust/issuers/{name}.pub` |
| `invalidate_cache()` | Clear the TTL cache (default 60s) |
| `TrustStoreError` | Raised on missing files, wrong permissions, or malformed keys |

**Trust store files must be `0600` permissions.** Loading a file with group- or world-readable bits is a hard error.

### Human Users (`arctrust.users`)

| Symbol | What It Does |
|---|---|
| `User` | One person: email, Argon2id password hash, and a signing DID of their own |
| `UserStore` | Loads / persists users from one `0600` file under `arc_state` (same custody as `operator.key`) |
| `OPERATOR` · `VIEWER` | The two roles |
| `default_users_path` · `UserStoreError` | Path accessor + structured load/permission error |

A bearer token proves someone holds a secret; it does not say *who*. Users give a deployment real people, so an approval and its audit record name a person — not "the operator token" (SPEC-057).

### PII / Secret Redaction (`arctrust.redaction`, `arctrust.secrets`)

| Symbol | What It Does |
|---|---|
| `RegexPiiDetector` · `PiiDetector` (Protocol) | Regex PII/secret detection with a pluggable override |
| `redact_text(text, matches)` | Replaces detected spans with typed placeholders (`[PII:…]` / `[SECRET:…]`) |
| `SECRET_PATTERNS` | Structured-prefix secret patterns (AWS / GitHub / JWT / PEM / DB-URL) folded into one togglable `SECRETS` category (ADR-423) |
| `luhn_valid` · `iban_mod97_valid` · `aba_checksum_valid` | Checksum gates that keep card/IBAN/routing matches from false-firing |

Deliberate scope boundary: **structured prefixes only** — no Shannon-entropy generic-secret tier, so a bare high-entropy token with no recognizable prefix is not caught here (one deterministic detect→redact path, ADR-423).

### Audit Encryption at Rest (`arctrust.audit_cipher`)

| Symbol | What It Does |
|---|---|
| `RecordCipher` | Seals a WORM record's captured content (`extra`) at rest — full connector inputs/outputs (D-577 / SPEC-062) |
| `derive_record_key` | Derives the at-rest key from material the deployment **already** custodies (the operator seed) — no second secret to store or rotate |

The seal sits **under the hash, not over it**: `WormSink` seals before it hashes, so the chain commits to the ciphertext. `verify_chain` therefore needs no key at all — an auditor can prove the chain is untampered while remaining unable to read what it says, and a flipped byte inside the ciphertext still fails verification. The envelope (`actor_did`, `action`, `target`, `outcome`, `ts`) stays in the clear as the operational index.

### Arc-Home Paths (`arctrust.paths`)

The **single resolver** for every path under `~/.arc`. Nothing in Arc composes its own home path — one split resolver is how a test once wrote into a developer's real `~/.arc`, and how one surface read a different directory than another.

| Symbol | What It Does |
|---|---|
| `arc_home` | `${ARC_CONFIG_DIR:-~/.arc}` — the **install**; disposable |
| `arc_runtime` · `arc_runtime_root` · `arc_runtime_version` | `<home>/runtime/current` — **replaced wholesale** on update |
| `arc_config` | `<home>/config` — **preserved** across updates |
| `arc_state` | `<home>/state` — operator key, identity, trust store, arcstore, NATS, bundles; **never touched** |
| `arc_team` | `${ARC_TEAM_ROOT:-~/arc}/team` — the fleet, **outside** the home, so deleting the home can't reach it |
| `activate_runtime` | Atomic `current` symlink flip — an update is a flip, a rollback is flipping it back |
| `trust_dir` · `operator_dir` · `identity_dir` · `store_dir` · `nats_dir` · `bundles_dir` · `capabilities_dir` · `blueprints_dir` · `skills_dir` · `audit_dir` · `users_file` · `gateway_*` | One named accessor per path under `arc_state` |
| `config_file` · `env_file` · `module_root` · `runtime_venv` · `runtime_bin` | Config / runtime path accessors |

Each accessor takes an optional explicit base for `--arc-dir`, and resolves the environment **per call** (never at import) because `ARC_CONFIG_DIR` is routinely exported after the module loads. Enforced by `tests/architecture/test_arc_home_single_resolver.py`.

---

## 🛡️ Security Properties

| Property | How |
|---|---|
| **Tamper-evident audit** | `WormSink` chains each event with a hash of the previous one (committing its `seq`), signs every record with the *operator's* key (Ed25519 or ECDSA-P256, never the agent's own DID key — SPEC-053), and persists to disk. Flip a single byte, forge a signature, or drop a record anywhere → chain verification fails |
| **No plaintext keys on disk** | Private keys live with `0600` permissions only. Group- or world-readable bits = hard error on load |
| **Constant-time verification** | `verify()` is constant-time (libsodium). No timing side channel |
| **Fail-closed policy** | Pipeline crashes → call denied. Exception in a layer → call denied. Default verdict on no match → DENY |
| **Single audit emission point** | All events go through `emit()`. Sinks fan out from there. No way to bypass |
| **Sink failure isolation** | A broken sink can't crash the agent (NIST AU-5). Failures swallowed silently and logged at WARN |

---

## 📋 Compliance Mapping

| NIST 800-53 | What `arctrust` Provides |
|---|---|
| AU-2, AU-3, AU-12 | `AuditEvent` schema + `emit()` single emission point |
| AU-5 | Sink failure isolation in `emit()` |
| AU-9, AU-10 | `WormSink` durable signed hash chain for tamper-evidence + non-repudiation |
| AU-8 | `ts` field on every `AuditEvent` |
| IA-3 | `AgentIdentity` Ed25519 DID |
| SC-12 | Ed25519 keys via libsodium; HKDF child derivation |
| SC-13 | Ed25519 / ECDSA-P256 (FIPS) asymmetric signing, SHA-256 hash chains |
| SC-28 | `0600` keyfile permissions enforced on load |
| AC-3 | `PolicyPipeline` deny-by-default |
| AU-10 | Non-repudiation via operator-signed (not agent-signed) WORM chains; federal external witness anchor (SPEC-053) |

| OWASP Agentic | What `arctrust` Provides |
|---|---|
| ASI03 (Identity & Privilege Abuse) | Per-agent DIDs, HKDF child identities, no shared keys |
| ASI02 (Tool Misuse) | `PolicyPipeline` with first-DENY-wins |
| ASI06 (Memory/Context Poisoning) | Tamper-evident audit trail catches modifications |
| ASI07 (Insecure Inter-Agent Comms) | Asymmetric (Ed25519 / ECDSA-P256) signing primitive every agent message can use |

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arctrust/tests
```

- **Tests:** 470+
- **Coverage:** 99%
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
