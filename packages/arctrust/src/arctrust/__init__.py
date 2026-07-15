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

TOFU (source approval — gates capability LOAD, not tool invocation):
    TofuDecision        — ALLOW / DENY / NEW_SIGHTING (distinct from policy.Decision)
    TofuLayer           — Per-tier source-approval gate over a validator config
    CapabilitySource    — Source bundle (name, source, signed) evaluated by TofuLayer
    ValidatorEntry      — One persisted approval (name → sha256 pin)
    ValidatorsConfig    — The ``[security.validators]`` block (auto_run + approvals)
    hash_source         — Canonical ``sha256:<hex>`` a pin binds to
    approve_source      — Pure ValidatorsConfig mutation: pin name → source hash
    load_validators     — Read ``[security.validators]`` from an arcagent.toml
    approve / disapprove — Pin / unpin a source hash in an arcagent.toml (persisted)
    persist_validators  — Atomic tomlkit rewrite of the validators block

Paths:
    arc_home            — ``${ARC_CONFIG_DIR:-~/.arc}`` user-wide config root
    default_operator_key_path — ``<arc_home>/operator/operator.key``
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
from arctrust.paths import arc_home, default_operator_key_path
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
from arctrust.tofu import CapabilitySource, TofuDecision, TofuLayer
from arctrust.trust_store import (
    TrustStoreError,
    invalidate_cache,
    load_issuer_pubkey,
    load_operator_pubkey,
    register_operator,
)
from arctrust.validators import (
    ValidatorEntry,
    ValidatorsConfig,
    approve,
    approve_source,
    disapprove,
    hash_source,
    load_validators,
    persist_validators,
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
    "CapabilitySource",
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
    "TofuDecision",
    "TofuLayer",
    "ToolCall",
    "TransparencyLogWitness",
    "TrustStoreError",
    "ValidatorEntry",
    "ValidatorsConfig",
    "VaultSigner",
    "VaultTransit",
    "WitnessAnchor",
    "WitnessDivergenceError",
    "WormSink",
    "__version__",
    "algorithm_is_fips_approved",
    "approve",
    "approve_source",
    "arc_home",
    "assert_fips_if_required",
    "build_pipeline",
    "build_signer",
    "canonical_json",
    "content_sha256",
    "default_operator_key_path",
    "derive_child_identity",
    "disapprove",
    "dominates",
    "emit",
    "fips_backend_active",
    "generate_did",
    "generate_keypair",
    "hash_source",
    "invalidate_cache",
    "load_issuer_pubkey",
    "load_operator_pubkey",
    "load_validators",
    "parse_classification",
    "parse_did",
    "persist_validators",
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
