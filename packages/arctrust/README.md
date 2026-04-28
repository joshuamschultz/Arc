<div align="center">

# 🪪 arctrust

### **The Cryptographic Foundation for Arc**
*Identity · Signing · Audit · Policy — the leaf every other Arc package depends on.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-176-success.svg)](#status)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen.svg)](#status)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-2563EB.svg)](#status)
[![Ed25519](https://img.shields.io/badge/crypto-Ed25519-DC2626.svg)](#cryptography)

</div>

---

## ✨ What is arctrust?

`arctrust` is the cryptographic floor of the Arc stack. Every other Arc package depends on it — `arctrust` itself depends on nothing but PyNaCl (libsodium) and Pydantic.

It gives you the four primitives every secure agent needs:

- 🪪 **Identity** — Ed25519 keypairs and DIDs (`did:arc:{org}:{type}/{hash}`)
- ✍️ **Signing** — sign and verify arbitrary bytes with libsodium
- 📜 **Audit** — structured events with hash-chained tamper-evident sinks
- ✅ **Policy** — a deny-by-default, fail-closed policy pipeline that decides whether a tool call is allowed

If you're building anything that needs to *prove* what happened, this is where you start.

---

## 🏗️ Where It Fits

```mermaid
flowchart TB
    classDef tr fill:#94A3B8,stroke:#1E293B,color:#0F172A
    classDef other fill:#E5E7EB,stroke:#6B7280,color:#111827

    arctrust[arctrust<br/>identity · sign · audit · policy]:::tr
    arcllm[arcllm]:::other
    arcrun[arcrun]:::other
    arcagent[arcagent]:::other
    arcskill[arcskill]:::other
    arcteam[arcteam]:::other
    arcgateway[arcgateway]:::other

    arcllm --> arctrust
    arcrun --> arctrust
    arcagent --> arctrust
    arcskill --> arctrust
    arcteam --> arctrust
    arcgateway --> arctrust
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
from arctrust import AgentIdentity, emit, AuditEvent, JsonlSink
import time

# 1. Generate a fresh agent identity
identity = AgentIdentity.generate(org="acme", agent_type="analyst")
print(identity.did)
# → did:arc:acme:analyst/a3f2c1...

# 2. Sign a message
msg = b"tool_call:read_file:/workspace/report.txt"
signature = identity.sign(msg)
assert identity.verify(msg, signature)

# 3. Emit a tamper-evident audit event
sink = JsonlSink("/var/log/arc/audit.jsonl")
emit(
    AuditEvent(
        event_type="tool.call",
        actor_did=identity.did,
        action="read_file",
        target="/workspace/report.txt",
        outcome="allowed",
        ts=time.time(),
    ),
    sink,
)
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

### Audit (`arctrust.audit`)

| Symbol | What It Does |
|---|---|
| `AuditEvent` | Structured event: `event_type`, `actor_did`, `action`, `target`, `outcome`, `ts`, `metadata` |
| `AuditSink` (Protocol) | Anything that knows how to write events |
| `JsonlSink` | Append-only newline-delimited JSON. Compliance-friendly, grep-friendly |
| `SignedChainSink` | Each event includes a hash of the previous event, signed with Ed25519. Makes the log **tamper-evident** — flipping one bit anywhere invalidates the whole chain from that point forward |
| `NullSink` | For tests |
| `emit(event, sink)` | Single emission point. Swallows sink failures (NIST AU-5) — a broken sink can never crash the agent |

### Policy (`arctrust.policy`)

| Symbol | What It Does |
|---|---|
| `PolicyPipeline` | Ordered, fail-closed evaluator. First DENY wins. Sub-1 ms p95 with LRU caching |
| `PolicyLayer` (Protocol) | A single policy stage. Takes `ToolCall` + `PolicyContext`, returns `Decision` (ALLOW / DENY / ABSTAIN) |
| `Decision` | `verdict` (ALLOW/DENY/ABSTAIN), `reason`, `policy_id`, `metadata` |
| `ToolCall` | `tool_name`, `args`, `caller_did`, `classification` |
| `PolicyContext` | Tier, tenant ID, run ID, timestamp, agent metadata |
| `TierConfig` | Maps a deployment tier (Personal / Enterprise / Federal) to its layer set |
| `build_pipeline(tier)` | Convenience builder — returns the right `PolicyPipeline` for a tier |

**Layer composition by tier:**

| Tier | Global | Provider | Agent | Team | Sandbox |
|---|---|---|---|---|---|
| Personal | ✅ | — | — | — | — |
| Enterprise | ✅ | ✅ | ✅ | — | — |
| Federal | ✅ | ✅ | ✅ | ✅ | ✅ |

### Trust Store (`arctrust.trust_store`)

| Symbol | What It Does |
|---|---|
| `load_operator_pubkey(name)` | Load operator Ed25519 pubkey from `~/.arc/trust/operators/{name}.pub` |
| `load_issuer_pubkey(name)` | Load skill-bundle issuer pubkey from `~/.arc/trust/issuers/{name}.pub` |
| `invalidate_cache()` | Clear the TTL cache (default 60s) |
| `TrustStoreError` | Raised on missing files, wrong permissions, or malformed keys |

**Trust store files must be `0600` permissions.** Loading a file with group- or world-readable bits is a hard error.

---

## 🛡️ Security Properties

| Property | How |
|---|---|
| **Tamper-evident audit** | `SignedChainSink` chains each event with a hash of the previous one, signed with Ed25519. Flip a single byte anywhere → chain verification fails |
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
| AU-9 | `SignedChainSink` for tamper-evidence |
| AU-8 | `ts` field on every `AuditEvent` |
| IA-3 | `AgentIdentity` Ed25519 DID |
| SC-12 | Ed25519 keys via libsodium; HKDF child derivation |
| SC-13 | Ed25519, HMAC, SHA-256 hash chains |
| SC-28 | `0600` keyfile permissions enforced on load |
| AC-3 | `PolicyPipeline` deny-by-default |

| OWASP Agentic | What `arctrust` Provides |
|---|---|
| ASI03 (Identity & Privilege Abuse) | Per-agent DIDs, HKDF child identities, no shared keys |
| ASI02 (Tool Misuse) | `PolicyPipeline` with first-DENY-wins |
| ASI06 (Memory/Context Poisoning) | Tamper-evident audit trail catches modifications |
| ASI07 (Insecure Inter-Agent Comms) | Ed25519 signing primitive every agent message can use |

---

## 🧪 Status

```bash
uv run --no-sync pytest packages/arctrust/tests
```

- **Tests:** 176
- **Coverage:** 99%
- **Type check:** `mypy --strict` clean
- **Lint:** `ruff check` clean

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
