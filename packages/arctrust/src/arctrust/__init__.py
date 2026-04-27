"""arctrust — Identity, keypair, audit, and policy primitives for Arc.

This is the leaf shared library in the Arc dependency graph. All other Arc
packages (arcagent, arcrun, arcgateway) depend on arctrust; arctrust never
imports from them.

Public surface
--------------
Identity:
    AgentIdentity       — DID + Ed25519 keypair, sign/verify
    ChildIdentity       — Derived ephemeral identity for spawned child agents
    derive_child_identity — HKDF-SHA256 child key derivation
    generate_did        — Derive DID string from an Ed25519 verify key
    parse_did           — Parse DID string into {org, agent_type, hash}
    validate_did        — Validate DID format; raise ValueError if malformed

Keypair:
    KeyPair             — Frozen dataclass (public_key, private_key bytes)
    generate_keypair    — Generate a fresh Ed25519 keypair
    sign                — Ed25519 sign → 64-byte signature
    verify              — Ed25519 verify → bool (never raises)

Audit:
    AuditEvent          — Pydantic schema for structured audit events
    AuditSink           — Protocol for sink implementations
    JsonlSink           — Append-only JSONL file sink
    NullSink            — No-op sink (tests, air-gapped evaluation)
    SignedChainSink     — Ed25519 hash-chained tamper-evident sink
    emit                — Safe dispatch (swallows sink failures per AU-5)

Policy:
    Decision            — Immutable policy evaluation result
    PolicyContext       — Tier, bundle version, age for a single evaluation
    PolicyLayer         — Protocol all layers must satisfy
    PolicyPipeline      — Ordered, fail-closed, short-circuiting evaluator
    TierConfig          — Tier metadata (layers, max_parallel_tools)
    ToolCall            — Immutable tool invocation request
    build_pipeline      — Factory: assemble correct layers for a tier

Trust store:
    TrustStoreError     — Structured trust-store load / key failure
    load_operator_pubkey — Load Ed25519 pubkey for an operator DID
    load_issuer_pubkey  — Load Ed25519 pubkey for a manifest-issuer DID
    invalidate_cache    — Flush the in-process TTL cache
"""

__version__ = "0.2.0"

from arctrust.audit import AuditEvent, AuditSink, JsonlSink, NullSink, SignedChainSink, emit
from arctrust.identity import (
    AgentIdentity,
    ChildIdentity,
    derive_child_identity,
    generate_did,
    parse_did,
    validate_did,
)
from arctrust.keypair import KeyPair, generate_keypair, sign, verify
from arctrust.policy import (
    Decision,
    PolicyContext,
    PolicyLayer,
    PolicyPipeline,
    TierConfig,
    ToolCall,
    build_pipeline,
)
from arctrust.trust_store import (
    TrustStoreError,
    invalidate_cache,
    load_issuer_pubkey,
    load_operator_pubkey,
)

__all__ = [
    "__version__",
    "AgentIdentity",
    "AuditEvent",
    "AuditSink",
    "ChildIdentity",
    "Decision",
    "JsonlSink",
    "KeyPair",
    "NullSink",
    "PolicyContext",
    "PolicyLayer",
    "PolicyPipeline",
    "SignedChainSink",
    "TierConfig",
    "ToolCall",
    "TrustStoreError",
    "build_pipeline",
    "derive_child_identity",
    "emit",
    "generate_did",
    "generate_keypair",
    "invalidate_cache",
    "load_issuer_pubkey",
    "load_operator_pubkey",
    "parse_did",
    "sign",
    "validate_did",
    "verify",
]
