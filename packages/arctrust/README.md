# arctrust

Leaf shared library providing cryptographic identity, keypair, audit emission, policy pipeline, and trust-store primitives for Arc.

## Layer position

arctrust is the leaf node in the Arc dependency graph — it depends only on PyNaCl and Pydantic. Every other Arc package (arcagent, arcrun, arcgateway, arcskill, arcteam) depends on arctrust. arctrust never imports from them.

## What it provides

- `AgentIdentity`, `ChildIdentity`, `derive_child_identity`, `generate_did`, `parse_did`, `validate_did` — DID identity primitives; `AgentIdentity` wraps an Ed25519 keypair and a `did:arc:{org}:{type}/{hash}` DID; `ChildIdentity` derives ephemeral identities for spawned subagents via HKDF-SHA256
- `KeyPair`, `generate_keypair`, `sign`, `verify` — Ed25519 keypair generation and sign/verify via PyNaCl (libsodium); `sign` returns 64-byte signatures; `verify` returns bool and never raises
- `AuditEvent`, `AuditSink`, `JsonlSink`, `NullSink`, `SignedChainSink`, `emit` — structured audit emission; `SignedChainSink` produces Ed25519 hash-chained tamper-evident logs; `emit` swallows sink failures per NIST AU-5 so a broken sink never crashes the agent
- `PolicyPipeline`, `Decision`, `PolicyLayer`, `ToolCall`, `PolicyContext`, `TierConfig`, `build_pipeline` — ordered, fail-closed policy evaluator; `build_pipeline` assembles the correct layer set for a given tier (Personal / Enterprise / Federal); first-DENY-wins with sub-1ms p95 latency
- `load_operator_pubkey`, `load_issuer_pubkey`, `TrustStoreError`, `invalidate_cache` — TTL-cached Ed25519 public key loading from trust store files at `~/.arc/trust/`; files must be 0600 permissions

## Quick example

```python
from arctrust import AgentIdentity, emit, AuditEvent, JsonlSink
import time

# Generate identity
identity = AgentIdentity.generate(org="acme", agent_type="analyst")
print(identity.did)  # did:arc:acme:analyst/<hash>

# Sign and verify
msg = b"tool_call:read_file:/workspace/report.txt"
sig = identity.sign(msg)
assert identity.verify(msg, sig)

# Emit an audit event
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

## Architecture references

- SPEC-007: DID Identity Unification — defines the `did:arc:{org}:{type}/{hash}` scheme and derivation rules
- SPEC-017: Arc Core Hardening — four-pillar requirement: every tier must Identity, Sign, Authorize, Audit
- ADR-019: Four Pillars Universal — pairing signature required at all tiers; `UnsafeNoOp` skill verification bypass eliminated

## Status

- Tests: 176 (run with `uv run --no-sync pytest packages/arctrust/tests`)
- Coverage: 99%
- ruff + mypy --strict: clean
