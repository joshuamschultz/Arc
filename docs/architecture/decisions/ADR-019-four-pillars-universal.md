# ADR-019: Four Pillars (Identity, Sign, Authorize, Audit) Are Universal Defaults

**Status**: Accepted
**Date**: 2026-04-26
**Supersedes**: tier-conditional pillar enforcement in pre-wave-2 code

## Context

Arc's foundational specs (SPEC-007 DID Identity Unification, SPEC-017 Arc Core Hardening) established four security pillars: every entity has a **DID** (identity), every artifact is **signed and verified**, every action is **authorized via policy pipeline**, every operation is **audited**.

The implementation drifted. Multiple modules gated these pillars behind a `tier == "federal"` check, with non-federal tiers either skipping verification (`UnsafeNoOp`), skipping sandboxing (`skip_sandbox`), accepting unsigned manifests/pairings, allowing empty allowlists (= allow-all), or omitting audit emission. This conflated *stringency* (which signers are trusted, what max_turns is, whether dynamic tool creation is permitted) with *the existence of the pillar itself*.

Documented bypasses removed in wave-2 Phase C:

| Location | Bypass |
|---|---|
| `arcskill/hub/verify.py` | `UnsafeNoOp` skipped Sigstore SAN+issuer at non-federal |
| `arcskill/hub/verify.py` | early return skipped SLSA predicate check at non-federal |
| `arcskill/hub/dry_run.py` | `skip_sandbox=True` honored at non-federal |
| `arcrun/backends/policy.py` | `require_manifest = (tier == "federal")` |
| `arcrun/backends/policy.py` | `allow_entry_points = (tier != "federal")` |
| `arcgateway/pairing_signature.py` | personal tier returned None on missing signature |
| `arcagent/modules/web/web_module.py` | empty allowlist accepted at non-federal (= allow-all) |
| `arcagent/modules/vault/resolver.py` | `vault.unreachable` audit suppressed at non-federal |

These are not edge cases. They are the framework's security model. Allowing non-federal deployments to run without them produced a two-tier framework where the personal/enterprise paths were materially less safe than federal — directly contradicting the foundational specs.

## Decision

The four pillars are **universal, non-optional defaults at every tier**:

1. **Identity** — every agent has a DID at construction. `ArcAgent.__init__` requires it; missing DID is a clear error pointing to `arc agent init` (which auto-generates and persists). Every tool dispatch carries `caller_did` (`arctrust.identity`, `arcagent.core.tool_registry`).

2. **Sign** — every loaded artifact (skill, extension, backend, pairing) is verified before use. `arctrust.keypair.verify`, `arctrust.trust_store`, Sigstore + Rekor + cert chain. No `UnsafeNoOp`. No `skip_sandbox`. No `require_manifest = federal-only`.

3. **Authorize** — `arctrust.policy.PolicyPipeline` evaluates every tool call. First-DENY-wins. Fail-closed on exceptions. Tier sets *which layers run* (Personal=Global only / Enterprise=Global+Provider+Agent+Sandbox / Federal=all 5 incl. Team), but the layer that runs at every tier *actually evaluates* — never bypassed.

4. **Audit** — `arctrust.audit.emit(AuditEvent, sink)` on every operation. Sinks: `JsonlSink` for compliance, `SignedChainSink` for tamper-evident chain, `arcui.bridge.UIBridgeSink` for live observability. Single emission point per call; sinks fan out.

Tier is **stringency metadata**, not a gate:

| Knob | Personal | Enterprise | Federal |
|---|---|---|---|
| Trusted issuers | self-signed OK + audit warn | operator-chain | operator-chain (FIPS) |
| Policy layers | Global | +Provider+Agent+Sandbox | +Team (5) |
| Dynamic tool creation | allowed | approval gate | DENIED |
| Egress allowlist | warn-only on misses | deny-default | deny-default + signed |
| `max_turns` default | always approve | auto-approve 2x | hard cap |
| Audit sink default | `JsonlSink` | `JsonlSink` | `SignedChainSink` |
| mTLS | optional (localhost) | required | required + FIPS |

Every tier still verifies. Every tier still authorizes. Every tier still audits. Every entity still has a DID.

## Consequences

**Positive**

- The framework's security guarantees no longer depend on a config flag.
- arctrust is the canonical home for identity / keypair / audit / policy primitives. Other packages import from arctrust; arctrust imports from no other Arc package. Layer purity preserved.
- Phase C wave-2 work cleanly migrated audit emission across packages to the canonical `arctrust.audit.emit` API. Single emission point feeds compliance log + live UI bridge.
- `arcui.bridge.UIBridgeSink` ties observability to the same event stream that compliance reads. No double-emit, no path divergence.
- Stale tests asserting "personal mode skips X" became regression tests asserting "personal mode still does X."

**Negative / Cost**

- Wave-2 broke backwards compatibility for:
  - Agents constructed without a DID — must regenerate via `arc agent init` (auto-generates and persists).
  - Skills loaded without a Sigstore signature at personal tier — now require self-signed bundle or operator-trusted source.
  - Backends loaded via `setuptools entry_points` — entry-points are now disabled at all tiers; signed manifests are required.
  - Pairing requests at personal tier — now require a signature (self-signed accepted).
- Test fixtures that constructed agents without DIDs needed updating.
- Per the `dont worry about backward compatability. this needs to be fresh and new` directive, no shim modules or deprecation paths were left behind.

**Mitigation**

- `arc agent init` auto-generates DID + keypair on first run; existing `arcagent.toml` files without `did` are repaired in place by `AgentIdentity.from_config`.
- Each Phase C agent left a clear error message + remediation pointer for the corresponding refactor.

## Compliance Mapping

The four pillars map to NIST 800-53 controls that are now enforced regardless of tier:

| Pillar | Control | Where |
|---|---|---|
| Identity | IA-2, IA-5, IA-8 | arctrust.identity, arcagent ArcAgent.__init__ |
| Sign | SI-7, CM-5, CM-8 | arctrust.keypair, arcskill.hub.verify, arcrun.backends.loader, arcgateway.pairing_signature |
| Authorize | AC-3, AC-6 | arctrust.policy.PolicyPipeline |
| Audit | AU-2, AU-3, AU-9, AU-11 | arctrust.audit.* |

Federal-only controls layered on top (FIPS-validated crypto, FedRAMP boundary, CMMC L3) remain federal-tier knobs that build on — not replace — the universal floor.

## References

- SPEC-007: DID Identity Unification (`. claude/specs/SPEC-007-did-identity-unification/`)
- SPEC-017: Arc Core Hardening (`. claude/specs/SPEC-017-arc-core-hardening/`)
- SPEC-018: Hermes Parity Roadmap (`. claude/specs/SPEC-018-hermes-parity-roadmap/`)
- ADR-004: arcagent core LOC ceiling raise (referenced for the post-consolidation core size)
