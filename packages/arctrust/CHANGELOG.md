# Changelog

All notable changes to arctrust will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-04-26

Major expansion: arctrust becomes the canonical leaf for identity, keypair, audit, and policy primitives across the Arc monorepo. Promoted from "trust-store TTL cache" to the four-pillar shared library that every Arc package depends on.

### Added

- **Identity primitives** (`identity.py`) — `AgentIdentity`, `ChildIdentity`, `derive_child_identity`, `generate_did`, `parse_did`, `validate_did`. Migrated from `arcagent.core.identity`. `AgentIdentity` wraps an Ed25519 keypair and a `did:arc:{org}:{type}/{hash}` DID; `ChildIdentity` derives ephemeral identities for spawned subagents via HKDF-SHA256.
- **Keypair primitives** (`keypair.py`) — `KeyPair`, `generate_keypair`, `sign`, `verify`. Ed25519 via PyNaCl/libsodium. `sign` returns 64-byte signatures; `verify` returns bool and never raises.
- **Audit emission** (`audit.py`) — `AuditEvent` Pydantic schema, `AuditSink` Protocol, `JsonlSink` (append-only local), `NullSink` (tests / air-gapped), `SignedChainSink` (Ed25519 hash-chained tamper-evident), and `emit()` that swallows sink failures per NIST AU-5. Single canonical schema; sinks fan out across arcagent, arcrun, arcllm, arcgateway, arcteam, arcskill, arcui.
- **Policy pipeline** (`policy.py`) — `PolicyPipeline`, `Decision`, `PolicyLayer`, `ToolCall`, `PolicyContext`, `TierConfig`, `build_pipeline`. First-DENY-wins, fail-closed evaluator with sub-1ms p95 latency. `build_pipeline()` factory returns the correct layer set per tier (Personal=1, Enterprise=4, Federal=5). Migrated from `arcagent.core.tool_policy` (614-LOC implementation now lives here; arcagent keeps a thin shim).
- **Tests** — `test_identity.py`, `test_keypair.py`, `test_audit.py`, `test_policy.py`, `test_trust_store_security.py`. 176 tests / 99% coverage on the new surface.

### Changed

- **Public API surface** — `__init__.py` now exports the full identity / keypair / audit / policy / trust-store catalog. Comprehensive docstring on every export.
- **README rewritten** — Now describes arctrust's four-pillar role; lists every public name with a one-line description; shows a quick example covering identity, sign, and audit emission.

### Security

- **ADR-019 alignment** — arctrust is the canonical implementation of the four pillars (Identity, Sign, Authorize, Audit). Personal/enterprise/federal differ only in stringency; every tier still verifies, authorizes, audits, and identifies through this package.
- **Eliminated SPEC-018 §HIGH-1 latent circular dependency** — arcagent and arcrun no longer reach into each other's internal trust-store modules; both depend on arctrust as the leaf.
- **Trust store hardening preserved** — Trust files still required at `0o600` permissions; 60-second TTL cache; `invalidate_cache()` for explicit flush.

## [0.1.0] - prior

Initial release: Ed25519 trust-store primitives. `load_operator_pubkey`, `load_issuer_pubkey`, `TrustStoreError`, `invalidate_cache`. TOML files at `~/.arc/trust/`; 0o600 enforcement; TTL cache.
