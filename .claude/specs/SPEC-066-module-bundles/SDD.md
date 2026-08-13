# Solution Design Document: Module Bundles and Capability Signing

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

Two artifacts replace one. The `arc-agent` wheel ships the nucleus and no modules; every module is delivered as a signed bundle, materialized to a deployment-level root outside every agent's reach, with its tools and skills copied per-agent into the existing untrusted capability directory. Underneath that, the capability trust gate gains the half it has always been missing: a command that actually writes a detached signature. Design is deliberately additive against existing seams — `core/module_discovery.py` keeps folder-presence discovery and only changes which directory it scans; `capabilities/capability_loader.py` keeps its `spec_from_file_location` path loading, its `_UNTRUSTED_ROOTS` set, and its Sign/TOFU gate untouched; `arcrun/backends/_verifier.py` supplies the canonical-JSON + Ed25519 manifest shapes rather than a second envelope; `arcui/routes/trust.py` keeps its operator-role gate and audit emission and only changes what approve *does*. Nothing in `arcagent/core/` grows: bundle verification, install, and materialize live in a new `arcbundle` leaf and in `arccli`, so the ADR-004 core budget is unaffected.

## Architecture

Layering follows `.claude/steering/structure.md#layer-model` and the DAG at `structure.md#dependency-dag`. One new package, `arcbundle`, is a leaf beside `arctrust`: it owns the bundle manifest model, canonical-JSON encoding, signing, verification, and atomic materialize, and imports only `arctrust` + Pydantic. `arcagent` consumes nothing from it at runtime — the agent only ever *reads* an already-materialized directory, which keeps the nucleus ignorant of distribution entirely. `arccli` orchestrates: `arc module {list,install,remove,bundle}` and the reworked `arc trust {approve,disapprove}`. `arcui` calls the same signing function through its existing seam. Three filesystem locations carry three different trust properties and never overlap: `${ARC_CONFIG_DIR:-~/.arc}/modules/<name>/` holds module runtime, mode 0444 in 0555, outside the tool fence, operator-writable only; `<agent_dir>/capabilities/<name>/` holds the per-agent copies of that module's tools and skills and is an existing agent-writable untrusted root; `<workspace>/` holds agent state and working data and never holds module code. Per-agent activation stays where it already lives, the `[modules.NAME]` config entry, so file placement never encodes policy.

## Components

### COMP-001: arcbundle.manifest
**Responsibility:** Pydantic model for a bundle manifest — module name, version, per-file relative path + SHA-256, issuer identifier, format version — plus its canonical-JSON encoding. Single source of truth for the on-disk bundle shape.
**Dependencies:** Pydantic, mirrors the canonical-JSON convention of `arcrun/backends/_verifier.py:canonical_json_payload`
**Inputs:** manifest fields, or raw manifest bytes
**Outputs:** `BundleManifest` instance; `canonical_bytes() -> bytes` producing byte-stable encoding independent of key order. Raises `BundleManifestError` on schema violation.

### COMP-002: arcbundle.signer
**Responsibility:** Produce a detached Ed25519 signature over a manifest's canonical bytes, and stamp the issuer. Used by release CI with the Arc release key and by `--from-source` with a local development key.
**Dependencies:** COMP-001, `arctrust.sign`, `arctrust.Signer`
**Inputs:** `BundleManifest`, private key or `Signer`, issuer id
**Outputs:** detached signature bytes written beside the manifest. Never writes module payload files.

### COMP-003: arcbundle.verifier
**Responsibility:** Verify a bundle before any byte is written: manifest signature against the tier's trusted issuer set, then every declared file hash. Fail-closed on any exception.
**Dependencies:** COMP-001, `arctrust.verify`, tier issuer allowlist
**Inputs:** bundle path, tier, trusted issuer keys
**Outputs:** `VerifiedBundle` on success. Raises `BundleSignatureError` / `BundleContentHashError`. Guarantees: no filesystem mutation on any path, success or failure.

### COMP-004: arcbundle.materializer
**Responsibility:** Atomically write a verified bundle to the deployment module root — stage to a temp sibling, fsync, rename — then apply mode 0444 to files and 0555 to directories. Inverse operation removes the tree.
**Dependencies:** COMP-003
**Inputs:** `VerifiedBundle`, destination root
**Outputs:** materialized module path. Guarantees: never leaves a partial tree; a failure mid-write leaves the destination exactly as it was. `remove(name)` deletes the tree and raises if it does not complete.

### COMP-005: arcbundle.builder
**Responsibility:** Package one module folder from the repository source catalog into a signed bundle file. Runs on the low side for air-gapped staging and in release CI.
**Dependencies:** COMP-001, COMP-002
**Inputs:** source module directory, module name, version, signing key, output path
**Outputs:** one `.arcbundle` file containing manifest, signature, and payload files.

### COMP-006: arcagent.core.module_discovery (modified)
**Responsibility:** Resolve `_MODULES_DIR` to `${ARC_CONFIG_DIR:-~/.arc}/modules/` instead of the installed package directory. Folder-presence discovery, `ModuleStatus`, and `active_modules()` semantics are unchanged.
**Dependencies:** none new, `ARC_CONFIG_DIR` resolution already in `core/config.py:711`
**Inputs:** config with `modules` mapping, optional root override
**Outputs:** unchanged `list[str]` / `dict[str, ModuleStatus]`. Invariant preserved: a module with no folder is not discovered and cannot load.

### COMP-007: arcagent.core.agent_lifecycle (modified)
**Responsibility:** Load each active module's `_runtime` by filesystem path rather than `importlib.import_module("arcagent.modules.<name>._runtime")`, using one loader for every module. The `configure()` signature-introspection kwarg dispatch is untouched.
**Dependencies:** COMP-006, `importlib.util.spec_from_file_location`, matching `capabilities/capability_loader.py`
**Inputs:** agent, workspace, resolved module folder
**Outputs:** configured module runtimes. Invariant: a module whose folder is absent contributes nothing and raises nothing.

### COMP-008: arccli.commands.module
**Responsibility:** Operator command surface: `list`, `install <names...>`, `install --all`, `install --from <bundle>`, `install --from-source <name>`, `remove <name>`, `bundle <names...> -o <file>`. Install writes the config entry and triggers the per-agent capability copy; remove is its exact inverse and exits non-zero on partial completion.
**Dependencies:** COMP-003, COMP-004, COMP-005, COMP-009, COMP-011, `commands/operator.py:resolve_operator_signer`
**Inputs:** argv
**Outputs:** exit 0 on success, non-zero with an explicit message on any failure. `--all` installs every module the deployment manifest permits and skips the rest without failing the command.

### COMP-009: arcbundle.capability_copy
**Responsibility:** Copy a materialized module's tools and skills into `<agent_dir>/capabilities/<name>/` for each agent enabling it, and delete them on remove. Module runtime is never copied.
**Dependencies:** COMP-004
**Inputs:** materialized module path, agent dir
**Outputs:** copied capability path. Copies land in the existing `agent` scan root and are therefore subject to `_UNTRUSTED_ROOTS` adjudication unchanged.

### COMP-010: arcagent.capabilities.signing
**Responsibility:** Write, and revoke, a detached Ed25519 signature over a capability `.py` or a skill's `SKILL.md` using the operator key, and pin that operator public key as the agent's trusted key. This is the half the trust gate has always lacked.
**Dependencies:** `arctrust.sign_artifact` / `verify_artifact`, `capabilities/inventory.py:read_capability_source`, `pin_name_for`
**Inputs:** artifact path, operator `Signer`, agent config path
**Outputs:** detached signature written beside the artifact; trusted key pinned; TOFU pin recorded. `revoke()` removes all three. Verified by re-running the loader's own `_passes_trust_gate`.

### COMP-011: arccli.commands.trust (modified)
**Responsibility:** `arc trust approve` becomes sign + pin-key + pin-hash in one command, replacing the hash-only pin and deleting the note that conceded the pin changes nothing. `disapprove` becomes the exact inverse.
**Dependencies:** COMP-010, existing `_operator_did()`, `list_gated`, `read_capability_source`
**Inputs:** capability name, `--agent`
**Outputs:** exit 0 and the post-approval load verdict re-read through the inventory seam. Refuses any non-operator caller.

### COMP-012: arcui.routes.trust (modified)
**Responsibility:** `POST /api/trust/approve` calls COMP-010 instead of pinning a hash alone. Route table, operator-role gate, roster resolution, audit emission, and the lazy inventory-seam import are unchanged.
**Dependencies:** COMP-010, existing `arcui.audit.emit_mutation_audit`
**Inputs:** authed operator request naming agent + capability
**Outputs:** JSON verdict. Invariant: not routed on `/ws/chat`, not registered on any tool registry.

### COMP-013: arcui web — capability trust panel
**Responsibility:** Render gated capabilities with their source text and an approve action, so the operator reads the artifact before authorizing it. Consumes `GET /api/trust/gated`.
**Dependencies:** COMP-012
**Inputs:** gated-capability rows
**Outputs:** approve/disapprove POSTs. The action is unavailable until the artifact source has been rendered on the row.

### COMP-014: Audit event set
**Responsibility:** Emit one event per outcome across both surfaces: `module.bundle.verified`, `module.signature_invalid`, `module.content_hash_mismatch`, `module.installed`, `module.removed`, `capability.signed`, `capability.signature_revoked`, plus the existing `capability:deny` refusals.
**Dependencies:** `arctrust.audit.emit`, `arcui.audit.emit_mutation_audit`
**Inputs:** outcome + context
**Outputs:** one `AuditEvent` carrying operator DID, artifact or bundle path, and source hash. Single emission point per outcome; sinks fan out unchanged.

### COMP-015: Absent-safe test suite
**Responsibility:** Parametrized over `discover_modules()`: remove one module's materialized tree, start a real agent, run a turn end to end, assert success. Includes the all-absent case.
**Dependencies:** COMP-006, COMP-007
**Inputs:** module name from live discovery
**Outputs:** pass/fail. Parametrization is derived from discovery, so a module added later is covered without editing the test.

### COMP-016: Documentation set
**Responsibility:** `docs/runbooks/signing-capabilities.md` (new operator procedure for CLI and arcui, including reading a denial), `docs/walkthrough/10-security-model.md` (where the signature floor sits relative to the TOFU layer), `README.md` (supply-chain row corrected to describe what the commands do).
**Dependencies:** COMP-008, COMP-011, COMP-012
**Inputs:** shipped command behavior
**Outputs:** three updated documents. Gate: `mkdocs --strict` builds with the runbook linked from the nav.


## Data Model

**BundleManifest** (COMP-001): `format_version: int`, `module: str`, `version: str`, `issuer: str`, `files: list[FileEntry]` where `FileEntry = {path: str (relative, no traversal), sha256: str}`. Canonical JSON = sorted keys, no whitespace, UTF-8, mirroring `arcrun/backends/_verifier.py:canonical_json_payload`. On-disk bundle = `manifest.json` + `manifest.sig` + `files/` payload tree. **ModuleStatus** (COMP-006) is unchanged — `name`, `discovered`, `enabled`; no `origin` field is added because there is exactly one root. **Capability signature** (COMP-010) reuses the existing detached-signature convention the loader already verifies in `_passes_trust_gate`; no new on-disk format. **Config**: `[modules.NAME] enabled` is unchanged and remains the sole activation signal. Deployment manifest of permitted modules is a signed allowlist reusing the `allowed_backends` shape from `arcrun/backends/_manifest.py`.

## External Integrations

No new external services. Ed25519 via PyNaCl through `arctrust` (`.claude/steering/tech.md#core-technologies`). Release signing runs in existing CI with the Arc release key; air-gapped staging is a file carried on approved media, not a network call. `mkdocs --strict` is an existing CI gate. No network access is required by any install path: `--from <bundle>` reads a local file, and `--all` resolves against a local signed manifest.

## Traceability

| Requirement | Components |
|---|---|
| REQ-319 | COMP-010, COMP-011 |
| REQ-320 | COMP-010, COMP-011 |
| REQ-321 | COMP-011, COMP-012 |
| REQ-322 | COMP-012, COMP-013 |
| REQ-323 | COMP-014 |
| REQ-324 | COMP-016 |
| REQ-325 | COMP-001 |
| REQ-326 | COMP-003 |
| REQ-327 | COMP-003, COMP-004 |
| REQ-328 | COMP-005, COMP-008 |
| REQ-329 | COMP-003, COMP-008 |
| REQ-330 | COMP-008 |
| REQ-331 | COMP-002, COMP-005, COMP-008 |
| REQ-332 | COMP-014 |
| REQ-333 | COMP-006 |
| REQ-334 | COMP-007 |
| REQ-335 | COMP-004 |
| REQ-336 | COMP-004, COMP-006 |
| REQ-337 | COMP-009 |
| REQ-338 | COMP-004, COMP-008, COMP-009 |
| REQ-339 | COMP-006, COMP-007, COMP-015 |

## Alternatives Considered

**One distribution per optional module** (D-632) — real absence and correct layering, matching what `arcgateway` already does for platform adapters; rejected on cost: eighteen distributions to version, sign, and release in lockstep with a weekly-changing core. **Post-install prune of site-packages** — no new packages and genuine absence; rejected because it mutates an installed wheel, breaks RECORD hashes, and is undone by the next `uv sync`. **A custom wheel per deployment** (D-635) — one artifact to carry; rejected because federal would then run code never built or tested elsewhere, and the wheel under test stops being the wheel that ships. **Scanning two roots, in-wheel and deployment** (D-633) — handles a source checkout with no install step; rejected because the in-wheel root is empty in every real deployment, so the materialize path would be the one nobody exercises locally. **Symlinking the source catalog for development** (D-641) — fastest inner loop; rejected because it creates an unverified load path whose only guard is a tier check, replaced by signing with a development key through the identical verify path. **Loading module capabilities in place from a trusted `module:<name>` root** (D-648) — no duplication and first-party code keeps its trusted classification; rejected because every module and extension would add a scan root, an unbounded threat surface whose shape is dictated by artifacts that do not exist yet, and because per-agent skill drift is a product goal a shared read-only original cannot serve. **A trusted `module-copy:` root for the copies** (D-649) — no re-signing friction; rejected because a writable directory whose contents are trusted is exactly the hole `_UNTRUSTED_ROOTS` was drawn to close. **A separate `arc trust sign` beside `approve`** (D-651) — each verb does one thing; rejected because an operator would have to know the signature floor and the TOFU layer are distinct gates evaluated in order, which is today's bug rather than a design.

## Risks and Mitigations

**Executable code outside the wheel for the first time.** A directory an agent could write to would be a self-modification path (ASI05/ASI06). Mitigation: COMP-004 places runtime at the deployment root outside the tool fence at 0444/0555, and only operator-run commands write there; COMP-015 does not cover this, so an explicit security test asserts the agent's `write`/`edit`/`bash` tools cannot reach the module root. **Migration breaks a silent assumption.** Anything that treated a module's contribution as guaranteed rather than nullable fails only when the module is gone. Mitigation: COMP-015 lands and passes before the first module leaves the wheel, and each module moves individually. **Development key becomes a production path.** Mitigation: the dev issuer is trusted at personal tier only, so a dev-signed bundle cannot verify on an enterprise or federal box — enforced in COMP-003, not in the CLI. **Rubber-stamped approval.** A button invites clicking without reading. Mitigation: COMP-013 withholds the action until the artifact source is rendered. **Core LOC budget (ADR-004, `.claude/steering/structure.md#module-boundaries`).** Mitigation: only COMP-006 and COMP-007 touch `arcagent/core/`, both are edits rather than additions; all new code lands in `arcbundle` and `arccli`. **Two canonical-JSON implementations drifting.** Mitigation: COMP-001 mirrors `arcrun/backends/_verifier.py` shapes, with a test asserting byte-identical encoding for an equivalent payload.

## Open Questions

_(none)_

## Deviations

### DEV-001

- **Date**: 2026-08-13
- **Spec**: SPEC-066-module-bundles
- **Reason Category**: security-required
- **Original Decision**: REQ-337
- **Status**: approved
- **Approver**: team-lead

**Description**

REQ-337 requires the per-agent capability copy to be 'subject to the existing untrusted-root trust gate'. It was written before T-972 measured what the UNTRUSTED class actually does to module code: UNTRUSTED applies ArcRun-isolated execution plus the agent-authored AST import allowlist, under which 17 of 18 modules register ZERO tools, because @hook / @background_task / @capability only register on an imported object and the stdlib a module legitimately imports is blocked. Adjudicating the copies as UNTRUSTED would therefore satisfy the wording and deliver a fleet with no working modules. The same conflict is what SDD alternative D-648 hit from the other direction. Separately, the copy was never read at all: the loader scanned the shared deployment root, so REQ-337's isolation goal (per-agent skill drift; 'a shared read-only original cannot serve') was undelivered and capability_copy.py's docstring asserted an isolation boundary that did not exist.

**Proposed Change**

Scan the per-agent copy at <agent_dir>/capabilities/modules/<name>/ as a VERIFIED root and DELETE the shared deployment-root scan, so a module has ONE load path rather than two. VERIFIED requires proof of authorship (signature + TOFU) without containment. Because the copy sits in an agent-writable directory and VERIFIED code runs uncontained, a module capability root requires a valid signature at EVERY tier including personal: independent of require_signature (which is tier-derived) and independent of auto_run_agent_code (which admits code the AGENT wrote, whereas a module is a distributed artifact with an issuer). Tampering breaks the signature and the capability is denied. pin_name_for_path derives the same module-qualified TOFU pin name from the copy as from the deployment original, so approvals written by arc module install still resolve. A module's RUNTIME is unchanged: it stays once at the deployment root, read-only, outside the tool fence.

**Impact**

REQ-337's literal 'untrusted-root' wording is not met; its substance (copy per agent, runtime stays at the deployment root, copies pass the load-time trust gate) is met and, for the first time, actually exercised. Security posture is strictly stronger than the shared-root behaviour it replaces: unsigned module code is now denied at personal tier, where before personal tier had no signature floor at all. Blast radius is per agent instead of deployment-wide. arc trust approve and arc trust list now resolve a module capability to the agent's own copy, so one agent's re-sign no longer touches the shared original. --from-source keeps working because arc module install pins the ephemeral development issuer key it just verified the manifest under into the agent's own [security.validators]; arcbundle.verifier still restricts DEV_ISSUER to personal tier (DEV_ISSUER_TIERS unchanged). Verified end to end: install --from-source, boot a real ArcAgent, scheduler's four tools register.
