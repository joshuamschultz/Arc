# Solution Design Document: Editable System Prompts (arcprompt)

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

A new leaf package `arcprompt` owns prompt storage and resolution for every Arc package. Stock prompts ship as markdown under `src/<pkg>/context/`; an agent may override any prompt with a signed markdown file in its config root. Resolution is two layers, first-match-wins, snapshotted once per run. arcui gains a prompts API and a Prompts tab that enumerates every prompt (stock read server-side), shows stock vs effective vs diff, and writes signed overlays. See `.claude/steering/structure.md#layer-model` and `#dependency-direction`.

## Architecture

`arcprompt` sits at arctrust's level in the dependency DAG (`.claude/steering/structure.md#dependency-direction`) — it imports only arctrust (for signature verification) and is imported by arcrun, arcagent, arcmemory, and arcskill. It never imports upward. Flow: agent startup constructs a PromptResolver with the agent's overlay root, pinned verification key, and tier posture (construction-time, never per-call — SPEC-017 precedent). At run start, arcagent asks the resolver for a PromptSnapshot: every prompt the run may use, resolved, verified, hashed, and frozen. The snapshot feeds the existing `agent:assemble_prompt` bus flow and arcrun's `get_strategy_prompts`, and emits one provenance audit event through `arctrust.audit.emit`. arcui reads stock directly from installed packages server-side and writes overlays through a SigningAuthority seam that resolves the signer from the request principal. Error handling follows `.claude/steering/tech.md#error-handling-pattern`.

## Components

### COMP-001: PromptResolver (arcprompt)
**Responsibility:** Two-layer first-match resolution of a prompt to its effective body. Absent overlay resolves to stock silently; a present-but-invalid overlay raises; a missing stock prompt raises a packaging error. Constructed once per agent with overlay root, pinned key, and posture — never resolved per call.
**Dependencies:** PromptDocument, SignatureVerifier, PromptCatalog
**Inputs:** resolve(package: str, name: str) -> PromptDocument; constructed with (overlay_root: Path, trusted_public_key: bytes | None, posture: TrustPosture)
**Outputs:** PromptDocument with body, source ('stock'|'overlay'), sha256. Raises PromptUnparseable / PromptUnsigned / PromptMissing — never returns a silent fallback for a present-but-broken overlay.

### COMP-002: PromptDocument + frontmatter model (arcprompt)
**Responsibility:** Parse markdown-with-frontmatter into a validated Pydantic model carrying name, description, tunable. Derive sha256 content digest at load. No authored version field is read or honored.
**Dependencies:** pydantic
**Inputs:** raw file bytes
**Outputs:** PromptDocument {name, description, tunable, body, sha256, source}. ValidationError on malformed frontmatter propagates as PromptUnparseable.

### COMP-003: SignatureVerifier (arcprompt)
**Responsibility:** Verify an overlay's Ed25519 signature against a pinned public key before its text is returned to any caller. Applies at every tier with no bypass flag. Delegates crypto to arctrust rather than reimplementing it.
**Dependencies:** arctrust.artifact.verify_artifact, arctrust.keypair
**Inputs:** verify(content: bytes, manifest: ArtifactSignature, trusted_public_key: bytes) -> bool
**Outputs:** True only when digest matches, signature verifies, AND the manifest key equals the pinned key. A pinned key is mandatory — an unpinned floor is no floor, so a None pinned key is refused, not skipped.

### COMP-004: PromptSnapshot + provenance audit (arcprompt)
**Responsibility:** Resolve the full prompt set once at run start and freeze it for the run's duration; emit exactly one audit event enumerating each prompt with package, name, source, and sha256, plus the resolved signer DID where the source is an overlay.
**Dependencies:** PromptResolver, arctrust.audit.emit
**Inputs:** snapshot(packages: Sequence[str]) -> PromptSnapshot, called once per run
**Outputs:** Immutable mapping (package, name) -> PromptDocument; one AuditEvent whose payload carries the resolved values, never a hardcoded tier or signer default.

### COMP-005: PromptCatalog (arcprompt)
**Responsibility:** Discover every packaged `context/` directory across installed Arc packages and enumerate the prompts within, so callers can list prompts that have no overlay. Locates packaged files via `Path(__file__).parent`, following the blueprints/loader.py convention.
**Dependencies:** filesystem
**Inputs:** catalog() -> list[PromptRef]
**Outputs:** PromptRef {package, name, description, stock_path}. Reads stock only; never touches overlay state.

### COMP-006: Agent prompt wiring (arcagent)
**Responsibility:** Construct the PromptResolver during agent setup with tier posture, pinned key, and overlay root resolved once at construction; obtain the run-start snapshot and feed it into the existing `agent:assemble_prompt` bus flow and arcrun's strategy prompt call.
**Dependencies:** COMP-001, COMP-004, arcagent.core.agent_lifecycle, arcagent.capabilities.inventory.resolve_trust_posture
**Inputs:** agent config + identity at setup; run start signal
**Outputs:** Prompt sections injected into assemble_system_prompt; snapshot handle held for the run. Posture values flow through construction (SPEC-017), never rebuilt per call with defaults.

### COMP-007: Overlay location + reset (arcagent)
**Responsibility:** Define the overlay path as `<agent_root>/context/<package>/<name>.md` — inside the agent config root, outside the workspace subtree agent file tools are confined to. Deleting an overlay is the reset-to-stock operation.
**Dependencies:** arcagent agent-root layout
**Inputs:** (agent_root, package, name)
**Outputs:** Path under the agent config root. Guaranteed non-addressable by workspace-confined agent tools; resolvable by arcui via the existing 'agent' root in _VALID_ROOTS.

### COMP-008: Agent self-write guard (arcagent)
**Responsibility:** Assert, by test, that no agent-invoked file tool can write into the overlay tree — the structural counterpart to COMP-007's placement. Complements rather than replaces placement, per the defense-in-depth solutions entry.
**Dependencies:** arcagent.tools._validation, COMP-007
**Inputs:** attempted tool write paths
**Outputs:** Denial for any path resolving into the overlay tree, with an audit event. Security test asserts traversal attempts cannot reach it.

### COMP-009: Externalized prompt content + byte-identity harness
**Responsibility:** The migrated markdown files themselves across arcrun, arcagent, arcmemory, and arcskill, plus the temporary tests asserting each loaded file equals its original Python constant byte-for-byte. Constant and its assertion are deleted together once green.
**Dependencies:** COMP-001, COMP-005
**Inputs:** existing prompt constants (arcrun.prompts, strategy prompt_guidance/description, workpad CONTEXT_MAINTAINER_SYSTEM_PROMPT, orchestration SPAWN_GUIDANCE, arcmemory CONSOLIDATION_SYSTEM_PROMPT, arcskill improver builders)
**Outputs:** One .md per prompt under the owning package's context/; a passing equality + sha256 assertion per prompt prior to constant deletion. Newline handling decided explicitly and asserted.

### COMP-010: Prompts API router (arcui)
**Responsibility:** List all prompts with overridden state; read stock + effective + unified diff; write an overlay (operator-gated, secret-scanned, signed); delete an overlay to reset. Stock is read server-side; the client never supplies a filesystem path.
**Dependencies:** COMP-005, COMP-001, COMP-011, arcui.routes.agent_detail._common, arcui.routes.agent_detail.files_write helpers
**Inputs:** GET /api/agents/{id}/prompts; GET|PUT|DELETE /api/agents/{id}/prompts/{package}/{name}
**Outputs:** Pydantic response models in arcui/schemas.py. Reuses _confine and _find_secret rather than reimplementing them; distinguishes 200-empty from 503-unreadable per the skill_versions error convention.

### COMP-011: SigningAuthority seam (arcui)
**Responsibility:** Resolve the signing identity from the authenticated principal on the request and sign the overlay. Today returns the single deployment operator key for an operator-role caller; under future per-user login returns that user's key with no call-site change. Reports the resolved signer DID for audit.
**Dependencies:** arctrust.artifact.sign_artifact, arcui.auth, arccli operator key location
**Inputs:** signer_for(request) -> SigningIdentity; sign(content: bytes, identity) -> ArtifactSignature
**Outputs:** Signature manifest plus the resolved signer DID. The write path never names a key directly; the audit payload never carries a constant signer.

### COMP-012: Prompts tab + drawer (arcui web)
**Responsibility:** List prompts grouped by package with stock/overridden chips; drawer with stock | effective | diff toggle, edit, save, reset. Reuses the skill-drawer shell, operator-mode gating, and banner conventions; renders the server-computed diff.
**Dependencies:** COMP-010, arcui web ui primitives, useOperatorMode
**Inputs:** agentId; selected (package, name)
**Outputs:** React components plus new query hooks in web/src/lib/queries.ts and types in types.ts. Diff is rendered from the server-supplied unified diff string — the first diff renderer in the product, as none exists today.

### COMP-013: Packaging + architecture guards
**Responsibility:** Ensure packaged markdown ships in every wheel, register arcprompt as a canonical package, and add the leaf-ness import guard that is not generically enforced.
**Dependencies:** hatchling build config, tests/architecture, scripts/check_loc_budgets.py
**Inputs:** each touched pyproject.toml; tests/architecture/; Makefile install target
**Outputs:** artifacts = ["src/<pkg>/**/*.md"] per shipping package; tests/architecture/test_no_arcprompt_imports_*.py; arcprompt added to _CANONICAL_PACKAGES and make install; optional LOC ceiling for arcprompt.

### COMP-014: Prompt-write policy gate
**Responsibility:** Route overlay writes through the existing PolicyPipeline so a deployment can deny prompt writes as policy content — freezing prompts post-authorization — without any tier branching inside arcprompt.
**Dependencies:** arctrust.policy.PolicyPipeline, COMP-010
**Inputs:** a prompt:write action with the caller's principal and the target prompt
**Outputs:** ALLOW when no rule is configured (no-op gate), DENY when a rule forbids it. Follows the configured-gate convention: unconfigured is a no-op, configured-but-blind denies.


## Data Model

No database tables. Prompts are files. Stock: `packages/<pkg>/src/<pkg>/context/<name>.md` — markdown with YAML frontmatter {name, description, tunable}. Overlay: `<agent_root>/context/<package>/<name>.md`, same schema, plus an Ed25519 signature whose storage form (detached `.arcsig` sidecar vs. a reserved frontmatter field) is an open question. Derived and never authored: sha256 content digest (version identity) and resolution source. In-memory: PromptDocument (frozen Pydantic model) and PromptSnapshot (immutable mapping, one per run). Audit payload per run: list of {package, name, source, sha256} plus resolved signer DID for overlays. See `.claude/steering/tech.md#conventions`.

## External Integrations

No external services. All integration is intra-repo: arctrust (signature verify/sign, audit emission, policy pipeline), arcagent (construction-time wiring, overlay root, bus injection), arcrun (strategy prompt consumption), arcmemory and arcskill (prompt consumers), arcui (HTTP surface and web UI). arcprompt itself performs only local filesystem reads. Note: arcrun mounts identity.md/context.md read-only in the docker backend — overlay visibility to a containerized agent must be verified against the next-run assumption.

## Traceability

| Requirement | Components |
|---|---|
| REQ-121 | COMP-001, COMP-005, COMP-013 |
| REQ-122 | COMP-001, COMP-007 |
| REQ-123 | COMP-004, COMP-006 |
| REQ-124 | COMP-001 |
| REQ-125 | COMP-001, COMP-002, COMP-003 |
| REQ-126 | COMP-001, COMP-005 |
| REQ-127 | COMP-002 |
| REQ-128 | COMP-007, COMP-008 |
| REQ-129 | COMP-003, COMP-001 |
| REQ-130 | COMP-011, COMP-010 |
| REQ-131 | COMP-011, COMP-004 |
| REQ-132 | COMP-004, COMP-006 |
| REQ-133 | COMP-010 |
| REQ-134 | COMP-010, COMP-005 |
| REQ-135 | COMP-010, COMP-001 |
| REQ-136 | COMP-010, COMP-007 |
| REQ-137 | COMP-012 |
| REQ-138 | COMP-009 |
| REQ-139 | COMP-014, COMP-010 |
| REQ-140 | COMP-013 |

## Alternatives Considered

Central registry with import-time registration (rejected: import side effects, and prompts discoverable only when a package is loaded — D-459). arcstore DB-backed prompts (rejected: prompts leave git, cold start touches storage, core path gains a DB dependency — D-459). Three-layer stock/fleet/agent overlays (rejected: a third resolution layer to reason about and display, and load-order precedence bugs are a documented failure mode of multi-file cascades — D-460). Seeding stock into every agent at install (rejected: a stale copy becomes indistinguishable from a deliberate override and upgrades stop reaching deployed agents — D-465; the residual divergence risk was raised in research and explicitly accepted in D-480). Per-call re-read like identity.md (rejected: a prompt could shift between turns of one run, leaving no single version attributable — D-461). Hash-and-audit without signatures, following the identity.md precedent (rejected: records tampering only after the model consumed the text, and identity.md's unsigned state is a confirmed gap rather than a precedent — D-467). Extending the generic files API with a stock-read redirect (rejected: would require building enumerate-all, diff, and reset anyway, while widening file-API confinement into installed package code — D-464). arcui holding one process-level operator key referenced at the call site (rejected: per-user login is a stated direction and would require unpicking every handler — D-481). Browser-side WebCrypto signing (rejected for v1: requires a key-management UI and auth model arcui does not have — D-481).

## Risks and Mitigations

Byte-identity drift during migration: `workpad/prompt.py` suppresses a leading newline via a backslash-continued quote while `read_text()` returns a trailing newline verbatim — COMP-009 must decide newline handling explicitly and assert it. `CONTEXT_MAINTAINER_SYSTEM_PROMPT` (~4.9 KB, the largest) has zero coverage today, so tests precede the move. Packaged markdown silently absent from wheels unless COMP-013 lands, which would surface only post-install as REQ-126 errors. Audit-lies risk: SPEC-017 showed enforcement correct while the audit event carried a hardcoded fallback — COMP-004 and COMP-011 must emit resolved values, never defaults. Signing friction on the default path, since `tier = "personal"` is hardcoded in eight scaffold sections. Interim custody exposure: until per-user login, the shared operator token confers signing authority — still an improvement on identity.md, which is writable through the same token unsigned. Coverage gates (line >= 80%, branch >= 75%) can be dragged down by a thin arcprompt with sparse tests. No existing fixture assembles a full system prompt, so COMP-009's integration assertion needs a new harness. Mitigations are the components themselves plus the test strategy in PLAN.

## Open Questions

- Signature storage form: detached `.arcsig` sidecar (matches arc's existing convention, zero canonicalization risk, can desync from its file) vs. a reserved frontmatter field using git's gpgsig excluded-field pattern (cannot desync, but no established convention exists for signing markdown+frontmatter, and CRLF/trailing-newline/YAML-round-trip traps all apply).
- Overlays live under a gitignored path, so the git-history half of REQ-127's version story does not exist for overlays. Either overlays gain store-backed history (as skill candidates do via arcstore) or the audit trail is the sole record of change.
- Whether `tunable: false` requires enforcement in v1 or stays inert metadata until the v2 optimizer exists.
- Whether identity.md's confirmed absent integrity check is closed here or tracked as separate work.
- Whether arcskill's four per-call improver builders belong in arcprompt at all, given they interpolate trace data rather than being authored harness text.
- Whether a containerized agent (docker backend, read-only identity/context mounts) observes overlay writes on the next run without a restart.
