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
    RecordCipher        — Seals a record's captured content at rest (D-577);
                          the chain commits to the ciphertext, so verification
                          still needs no sealing key
    derive_record_key   — At-rest key from already-custodied seed material

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
    pin_key / unpin_key — Add / remove a trusted capability-verification key (persisted)
    persist_validators  — Atomic tomlkit rewrite of the validators block

Paths (``arctrust.paths`` — the ONE resolver; never compose your own):
    arc_home            — ``${ARC_CONFIG_DIR:-~/.arc}``, parent of the four roots
    arc_runtime         — ``<arc_home>/runtime/current``, replaced on update
    arc_config          — ``<arc_home>/config``, preserved on update
    arc_state           — ``<arc_home>/state``, never touched by an update
    arc_team            — ``<arc_home>/team``, never touched by an update
    config_file / env_file                — one config file under ``arc_config``
    operator_dir / default_operator_key_path / identity_dir / trust_dir /
    store_dir / nats_dir / bundles_dir / capabilities_dir / blueprints_dir /
    skills_dir / gateway_* / audit_dir / users_file — one path under ``arc_state``
    module_root         — ``<arc_runtime>/modules``
    activate_runtime    — atomic ``current`` symlink flip (update / rollback)
"""

__version__ = "0.9.0"

from arctrust.artifact import (
    ArtifactSignature,
    content_sha256,
    sign_artifact,
    sign_artifact_with_signer,
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
from arctrust.audit_cipher import RecordCipher, derive_record_key
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
from arctrust.paths import (
    activate_runtime,
    arc_config,
    arc_home,
    arc_runtime,
    arc_runtime_root,
    arc_runtime_version,
    arc_state,
    arc_team,
    audit_dir,
    blueprints_dir,
    bundles_dir,
    capabilities_dir,
    config_file,
    default_operator_key_path,
    env_file,
    gateway_dir,
    gateway_pairing_db,
    gateway_runtime_dir,
    identity_dir,
    module_root,
    nats_dir,
    operator_dir,
    skills_dir,
    store_dir,
    trust_dir,
    users_file,
)
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
from arctrust.redaction import (
    ALL_CATEGORIES,
    DEFAULT_OFF_ENTITIES,
    MAX_REGEX_SCAN_LENGTH,
    SECRETS_CATEGORY,
    EntityToggle,
    PiiDetector,
    PiiMatch,
    RedactionConfigError,
    RegexPiiDetector,
    aba_checksum_valid,
    iban_mod97_valid,
    luhn_valid,
    redact_text,
)
from arctrust.secrets import SECRET_PATTERNS
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
from arctrust.users import (
    OPERATOR,
    VIEWER,
    User,
    UserStore,
    UserStoreError,
    default_users_path,
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
    pin_key,
    unpin_key,
)
from arctrust.witness import (
    AppendOnlyMediumWitness,
    TransparencyLogWitness,
    WitnessAnchor,
    WitnessDivergenceError,
    verify_local_head_witnessed,
)

__all__ = [
    "ALL_CATEGORIES",
    "DEFAULT_OFF_ENTITIES",
    "ECDSA_P256",
    "ED25519",
    "MAX_REGEX_SCAN_LENGTH",
    "OPERATOR",
    "SECRETS_CATEGORY",
    "SECRET_PATTERNS",
    "VIEWER",
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
    "EntityToggle",
    "FileNotaryTransit",
    "InProcessSigner",
    "KeyPair",
    "NullSink",
    "OperatorKey",
    "OperatorKeyIntegrityError",
    "PiiDetector",
    "PiiMatch",
    "PolicyContext",
    "PolicyLayer",
    "PolicyPipeline",
    "RecordCipher",
    "RedactionConfigError",
    "RegexPiiDetector",
    "Signer",
    "SignerConfig",
    "SignerError",
    "TofuDecision",
    "TofuLayer",
    "ToolCall",
    "TransparencyLogWitness",
    "TrustStoreError",
    "User",
    "UserStore",
    "UserStoreError",
    "ValidatorEntry",
    "ValidatorsConfig",
    "VaultSigner",
    "VaultTransit",
    "WitnessAnchor",
    "WitnessDivergenceError",
    "WormSink",
    "__version__",
    "aba_checksum_valid",
    "activate_runtime",
    "algorithm_is_fips_approved",
    "approve",
    "approve_source",
    "arc_config",
    "arc_home",
    "arc_runtime",
    "arc_runtime_root",
    "arc_runtime_version",
    "arc_state",
    "arc_team",
    "assert_fips_if_required",
    "audit_dir",
    "blueprints_dir",
    "build_pipeline",
    "build_signer",
    "bundles_dir",
    "canonical_json",
    "capabilities_dir",
    "config_file",
    "content_sha256",
    "default_operator_key_path",
    "default_users_path",
    "derive_child_identity",
    "derive_record_key",
    "disapprove",
    "dominates",
    "emit",
    "env_file",
    "fips_backend_active",
    "gateway_dir",
    "gateway_pairing_db",
    "gateway_runtime_dir",
    "generate_did",
    "generate_keypair",
    "hash_source",
    "iban_mod97_valid",
    "identity_dir",
    "invalidate_cache",
    "load_issuer_pubkey",
    "load_operator_pubkey",
    "load_validators",
    "luhn_valid",
    "module_root",
    "nats_dir",
    "operator_dir",
    "parse_classification",
    "parse_did",
    "persist_validators",
    "pin_key",
    "read_verified_anchor",
    "redact_text",
    "register_operator",
    "sign",
    "sign_artifact",
    "sign_artifact_with_signer",
    "skills_dir",
    "store_dir",
    "trust_dir",
    "unpin_key",
    "users_file",
    "validate_did",
    "verify",
    "verify_artifact",
    "verify_chain",
    "verify_local_head_witnessed",
    "verify_signature",
    "worm_policy_sink",
]
