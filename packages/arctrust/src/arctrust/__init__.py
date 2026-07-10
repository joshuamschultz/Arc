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

Operator (audit authority):
    OperatorKey         — deployment audit-signing seed, deliberately NOT an
                          AgentIdentity (no sign/did); signs every WORM chain

Audit:
    AuditEvent          — Pydantic schema for structured audit events
    AuditSink           — Protocol for sink implementations
    NullSink            — No-op sink (tests, air-gapped evaluation)
    WormSink            — Durable, append-only, Ed25519-signed hash chain
                          (the compliance system of record; survives restart)
    emit                — Safe dispatch (swallows sink failures per AU-5)
    read_verified_anchor — Read the newest verified "trace.checkpoint"
                          anchor from a WORM chain, or None if the chain
                          fails verify_chain() or carries no such record

Policy:
    Decision            — Immutable policy evaluation result
    PolicyContext       — Tier, bundle version, age for a single evaluation
    PolicyLayer         — Protocol all layers must satisfy
    PolicyPipeline      — Ordered, fail-closed, short-circuiting evaluator
    ToolCall            — Immutable tool invocation request
    build_pipeline      — Factory: assemble correct layers for a tier

Canonical serialization:
    canonical_json      — Deterministic canonical-JSON bytes a signature binds
                          (the one serializer every signing package reuses)

Trust store:
    TrustStoreError     — Structured trust-store load / key failure
    load_operator_pubkey — Load Ed25519 pubkey for an operator DID
    load_issuer_pubkey  — Load Ed25519 pubkey for a manifest-issuer DID
    invalidate_cache    — Flush the in-process TTL cache
"""

__version__ = "0.9.0"

from arctrust.artifact import (
    ArtifactSignature,
    content_sha256,
    sign_artifact,
    verify_artifact,
)
from arctrust.audit import (
    AuditEvent,
    AuditSink,
    NullSink,
    WormSink,
    emit,
    read_verified_anchor,
    verify_chain,
    worm_policy_sink,
)
from arctrust.canonical import canonical_json
from arctrust.classification import (
    Classification,
    dominates,
    parse_classification,
)
from arctrust.fips import (
    ArcTrustFipsError,
    algorithm_is_fips_approved,
    assert_fips_if_required,
    fips_backend_active,
)
from arctrust.identity import (
    AgentIdentity,
    ChildIdentity,
    derive_child_identity,
    generate_did,
    parse_did,
    validate_did,
)
from arctrust.keypair import KeyPair, generate_keypair, sign, verify
from arctrust.operator import OperatorKey, OperatorKeyIntegrityError
from arctrust.policy import (
    ClassificationLayer,
    ClearanceContext,
    Decision,
    PolicyContext,
    PolicyLayer,
    PolicyPipeline,
    ToolCall,
    build_pipeline,
)
from arctrust.signer import (
    ECDSA_P256,
    ED25519,
    FileNotaryTransit,
    InProcessSigner,
    Signer,
    SignerConfig,
    SignerError,
    VaultSigner,
    VaultTransit,
    build_signer,
    verify_signature,
)
from arctrust.trust_store import (
    TrustStoreError,
    invalidate_cache,
    load_issuer_pubkey,
    load_operator_pubkey,
    register_operator,
)
from arctrust.witness import (
    AppendOnlyMediumWitness,
    TransparencyLogWitness,
    WitnessAnchor,
    WitnessDivergenceError,
    verify_local_head_witnessed,
)

__all__ = [
    "ECDSA_P256",
    "ED25519",
    "AgentIdentity",
    "AppendOnlyMediumWitness",
    "ArcTrustFipsError",
    "ArtifactSignature",
    "AuditEvent",
    "AuditSink",
    "ChildIdentity",
    "Classification",
    "ClassificationLayer",
    "ClearanceContext",
    "Decision",
    "FileNotaryTransit",
    "InProcessSigner",
    "KeyPair",
    "NullSink",
    "OperatorKey",
    "OperatorKeyIntegrityError",
    "PolicyContext",
    "PolicyLayer",
    "PolicyPipeline",
    "Signer",
    "SignerConfig",
    "SignerError",
    "ToolCall",
    "TransparencyLogWitness",
    "TrustStoreError",
    "VaultSigner",
    "VaultTransit",
    "WitnessAnchor",
    "WitnessDivergenceError",
    "WormSink",
    "__version__",
    "algorithm_is_fips_approved",
    "assert_fips_if_required",
    "build_pipeline",
    "build_signer",
    "canonical_json",
    "content_sha256",
    "derive_child_identity",
    "dominates",
    "emit",
    "fips_backend_active",
    "generate_did",
    "generate_keypair",
    "invalidate_cache",
    "load_issuer_pubkey",
    "load_operator_pubkey",
    "parse_classification",
    "parse_did",
    "read_verified_anchor",
    "register_operator",
    "sign",
    "sign_artifact",
    "validate_did",
    "verify",
    "verify_artifact",
    "verify_chain",
    "verify_local_head_witnessed",
    "verify_signature",
    "worm_policy_sink",
]
