# Changelog

All notable changes to arctrust will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-07-06

SPEC-034: complete the PolicyPipeline — the three stub layers become real policy decisions, the `PolicyContext` grows a typed injected-state contract, and every decision is routed into the durable WORM chain.

### Added

- **Real `ProviderLayer`** (`policy.py`, LLM10) — a pure comparator. Holds per-provider `ProviderLimit` (budget + rate) floors from construction; reads live `PolicyContext.provider_usage` (filled later by SPEC-038). Denies `provider.budget_exceeded` / `provider.rate_exceeded`. **Configured-gate:** with no limits configured the layer is a no-op ALLOW (absence of a budget policy is not a violation); only once a limit IS configured does missing usage telemetry fail closed (`provider.state_missing`). Never calls arcllm, never decrements, holds no mutable store.
- **Real `TeamLayer`** (`policy.py`, ASI03/ASI07) — capability-scoping. Static role→scope floor from construction; per-call activated scope + delegation grant from `PolicyContext.team_scope`. Denies `team.scope_violation` (out-of-scope tool) and `team.delegation_exceeded` (delegated call wider than its grant — monotonic-narrowing). Absent team scope ALLOWs (admission is IdentityLayer's job).
- **Real `SandboxLayer`** (`policy.py`, ASI04/ASI05) — deliberately thin. Reads verification (SPEC-033) + isolation (SPEC-036) status from `PolicyContext.tool_runtime` and compares over the `host < container < vm` ladder. Denies `sandbox.unverified_tool` / `sandbox.isolation_unsatisfiable`. **Configured-gate:** with no runtime status in context the layer is a no-op ALLOW — the SPEC-033 load gate already verified any registry tool, so a blind sandbox layer has nothing to add. Re-runs no verification, starts no sandbox.
- **`PolicyContext` injected-state contract** — new frozen Pydantic models `ProviderUsage`, `TeamScope`, `ToolRuntimeStatus` and three optional fields (`provider_usage`, `team_scope`, `tool_runtime`), each defaulting `None` so existing 3-field constructions stay valid. `ProviderLimit` model + `build_pipeline(provider_limits=, team_roles=)` config threads.
- **`worm_policy_sink(sink)`** (`audit.py`, REQ-017) — adapts the pipeline's `(event_type, payload)` audit callback to a durable `AuditSink`, mapping each decision to an `AuditEvent(action="policy.evaluate")` and emitting it via `emit()`. Raw tool arguments are never copied (only `input_hash`, AU-9). Routes every ALLOW/DENY into the tamper-evident, Ed25519-signed WORM chain; `verify_chain()` passes over the result. Exported from the package root.

### Changed

- Policy audit payload key `matched_rule` → `rule_id` (AU-2 event-reconstruction payload now carries `tier`, `layer`, `rule_id`, `input_hash`, `classification`; no raw arguments).
- `build_pipeline` constructs the Provider/Team/Sandbox layers (enterprise/federal only) with their config. Each layer is a no-op when its policy is unconfigured and fails closed only when a configured policy meets missing telemetry — so default/empty config never bricks a tier.

## [0.4.0] - 2026-07-06

SPEC-033 A1: detached artifact signing — the crypto primitive behind arcagent's Sign-pillar enforcement on agent-authored capabilities.

### Added

- **Artifact signing** (`artifact.py`) — `sign_artifact`/`verify_artifact`/`content_sha256` and the `ArtifactSignature` model. Content-hash (`sha256:<hex>`) + Ed25519 detached signature over arbitrary bytes, serialisable to a `.arcsig` sidecar (`ArtifactSignature.to_json`/`from_json`). `verify_artifact` never raises — any malformed field, algorithm mismatch, digest mismatch, or (when pinned) public-key mismatch collapses to `False`, fail-closed. Exported from the package root: `ArtifactSignature`, `content_sha256`, `sign_artifact`, `verify_artifact`.
- **Honest semantics, documented** — a valid signature proves the bytes are *unmodified since the signer wrote them* and *attributed* to the signer's DID key. It does not prove the content is safe; a compromised signer produces a perfectly valid signature over malicious bytes. Safety is the caller's TOFU gate and execution sandbox, never this primitive — stated explicitly in the module docstring so downstream callers don't over-trust it.
- **Tests** — `test_artifact.py`.

## [0.3.0] - 2026-07-05

### Added

- **`read_verified_anchor`** (`audit.py`) — reads the newest `extra` payload
  from a WORM chain whose `event.action` matches a caller-supplied action
  (default `"trace.checkpoint"`), returning `None` if the chain itself fails
  `verify_chain()` (tampered/forged/gapped/absent) or no matching record
  exists. Checkpoints are ordinary `AuditEvent`s — no new schema. Pairs with
  `arcllm.trace_retention.verify_against_anchor` to close the
  head-truncation/rollback tamper-evidence gap in arcllm's trace store: the
  WORM chain durably anchors a store's `head_hash` at rotation time, and a
  later rollback that removes that head is detectable even though arcllm's
  own hash chain still self-verifies over the records present.

### Fixed

- The 0.2.0 entry below named the now-removed `JsonlSink`/`SignedChainSink`
  as current public API; both were in fact collapsed into `WormSink` in
  that same release. Noted here for doc accuracy — no behavior change.

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
