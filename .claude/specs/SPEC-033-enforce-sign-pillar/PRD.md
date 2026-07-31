# PRD — SPEC-033 Enforce the Sign Pillar

**Format:** EARS. Each requirement carries a governing **pillar** and a pillar-tied acceptance criterion. IDs are monotonic `REQ-NNN`. MoSCoW priority. Every REQ is tagged **[WIRE]** (wire existing, tested code — very-few-LOC) or **[NEW]** (net-new code).

**Cross-cutting constraints (apply to every REQ):** *Simplicity* — prefer wiring existing built code; smallest correct change. *Modularity* — arctrust owns verify/sign primitives + WORM chain; arcagent wires the load/execute path; arcskill owns Sigstore/Rekor. *Security* — fail-closed; no unsigned exec above personal. *Scalability* — per-agent shared-nothing; O(1) hash lookup; verify-on-change.

**Depends on:** SPEC-036 (execution sandbox + personal-off contract) for REQ-040.

---

## Goal & non-goals

**Goal:** every agent-loadable code artifact is signature/hash-verified and runtime-restricted before it executes, at every tier; agent-authored artifacts are signed on write; relaxation is possible only at personal tier via explicit config.

**Non-goals (this spec):** building the execution sandbox (SPEC-036); Sigstore keyless issuance flow; re-signing first-party `builtins`/`global` artifacts (release-signed upstream — this spec only *verifies* them at load).

---

## Requirements

### Harden the capability load path — *Must*

- **REQ-001** *(Security)* **[WIRE]** WHEN the loader loads a `workspace`-root `.py` capability, it SHALL execute it through the existing `DynamicToolLoader` (RESTRICTED_BUILTINS + restricted `__import__`), NOT plain `exec(code, module.__dict__)`.
  - *Accept:* `capability_loader._load_module`'s bare `exec` (`capability_loader.py:443`) no longer runs workspace source; the compiled namespace uses `RESTRICTED_BUILTINS` and the wrapped `__import__`; the restricted-builtins security suite passes against the live load path (not just the loader in isolation).
- **REQ-002** *(Security)* **[WIRE]** WHERE a capability comes from a non-`workspace` root (`builtins`/`global`/`agent`), the loader SHALL require a valid signature at load rather than silently skipping validation.
  - *Accept:* the `_UNTRUSTED_ROOTS`-only validation gap (`capability_loader.py:65,196-198`) is closed — every root either passes `DynamicToolLoader` (workspace) or load-time signature verify (first-party roots); no root loads with neither.

### Verify signatures at load, not just install — *Must*

- **REQ-010** *(Security)* **[WIRE]** WHEN evaluating any capability source for load, the loader SHALL call `TofuLayer.evaluate` and act on the per-tier `Decision` (ALLOW / DENY / NEW_SIGHTING) before registering or executing it, fail-closed on any error.
  - *Accept:* the zero-caller gap on `TofuLayer` (`core/tofu_layer.py:56`) is closed; an unsigned source at federal returns `DENY` and is not registered; an exception in evaluation denies (never loads).
- **REQ-011** *(Security)* **[NEW]** WHEN a signed skill/tool is (re)loaded, the system SHALL re-verify its signature/content-hash against the artifact bytes at LOAD time, independent of the install-time check.
  - *Accept:* arcskill exposes a load-time verify entry point (today `verify_bundle` runs only from `installer.py:345`); a skill whose bytes changed after install fails load-time verify and does not register; federal-grade verify (`require_signature`) is the floor at every tier (personal may relax per REQ-050).

### Sign agent-authored artifacts on write — *Must*

- **REQ-020** *(Security)* **[NEW]** WHEN `create_tool` or `create_skill` writes an agent-authored artifact, it SHALL sign the written bytes with `arctrust.keypair` (agent identity), and the load path SHALL verify that signature on (re)load.
  - *Accept:* `create_tool.py:52` / `create_skill.py:88` writes are accompanied by a detached signature/manifest; a tampered-after-write artifact fails load; unsigned agent-authored artifacts do not execute above personal.
- **REQ-021** *(Security)* **[NEW]** WHEN the skill-improver `apply_result` writes a mutated skill, it SHALL sign the mutation with `arctrust.keypair` and the load path SHALL verify it on reload.
  - *Accept:* `skill_improver/engine.py:222` write produces a signature over the new text; a mutated-but-unsigned skill fails load; verify covers improver output identically to `create_skill` output.
- **REQ-022** *(Audit)* **[NEW]** The skill-improver audit trail SHALL be emitted through the signed WORM chain (`arctrust.audit.emit` + `SignedChainSink`), not the plaintext `audit.jsonl`.
  - *Accept:* `candidate_store.append_audit` (`candidate_store.py:136-141`) routes through `arctrust.audit.emit`; the plaintext `audit.jsonl` writer is removed (no dual sink); the chain is tamper-evident.

### TOFU approval gate — *Must*

- **REQ-030** *(Security)* **[NEW]** WHEN a `workspace` `.py` capability is seen for the first time, the system SHALL require explicit approval (consulting `ValidatorsConfig`) before it executes, and SHALL audit the approval decision.
  - *Accept:* a never-seen workspace tool yields `Decision.NEW_SIGHTING` and does not execute until approved; approval records the source's `name`+`hash` into `ValidatorsConfig.approved` (`core/config.py:267`); a later byte change re-triggers `NEW_SIGHTING`; each decision emits an audit event.

### Sandboxed execution — *Must*

- **REQ-040** *(Security)* **[WIRE]** *(depends SPEC-036)* WHEN agent-authored code executes, it SHALL run through the SPEC-036 tier-routed sandbox backend, never bare `exec`/host subprocess; personal tier MAY run unsigned/unsandboxed only when the operator has explicitly disabled signing AND the SPEC-036 sandbox.
  - *Accept:* workspace-tool execution dispatches to the SPEC-036 backend router; federal/enterprise never bare-exec and never silently degrade; a personal operator with both toggles off may run unsigned workspace tools on their own machine; `os_sandbox.make_sandbox` (`os_sandbox.py:80`) is either wired or explicitly ceded to SPEC-036 (no dead second sandbox).

### Config-relaxable & pluggable — *Must / Should*

- **REQ-050** *(Modularity)* **[NEW]** *(Must)* Signing/verification SHALL be relaxable only at personal tier via explicit config (mirroring SPEC-036 personal-off); enterprise and federal SHALL ignore/reject any relaxation.
  - *Accept:* a personal config toggle disables sign+verify (with an audit WARN); the same toggle at enterprise/federal is rejected (or has no effect) and verification still runs; default everywhere is verify-on.
- **REQ-051** *(Modularity)* **[NEW]** *(Should)* Verification/trust SHALL be a pluggable layer behind one interface, supporting Sigstore keyless OR an org keypair.
  - *Accept:* swapping the trust backend (Sigstore ↔ org keypair) requires no change to `capability_loader`; both satisfy one verify Protocol; the active backend is config-selected.

### Cleanup — *Should*

- **REQ-060** *(Simplicity)* **[NEW]** *(Should)* Superseded paths SHALL be removed in the same edits that replace them: the plain-`exec` workspace load, the plaintext improver `audit.jsonl` writer, any validation-skipping root branch.
  - *Accept:* `grep` finds no `exec(code, module.__dict__)` on the workspace path and no plaintext improver audit writer; no compat shims; `ruff` 0, `mypy --strict` 0 on touched packages.

---

## MoSCoW

- **Must:** REQ-001, 002, 010, 011, 020, 021, 022, 030, 040, 050 (harden load, verify-at-load, sign agent artifacts, WORM audit, TOFU gate, sandboxed exec, personal-only relaxation).
- **Should:** REQ-051 (pluggable trust backend), REQ-060 (cleanup, inline with the edits that supersede each path).
- **Won't (this spec):** the SPEC-036 sandbox itself; Sigstore keyless issuance UX; re-signing shipped first-party artifacts.

## Traceability

Every REQ → SDD component → PLAN task; each task scoped to one module and marked WIRE or NEW.
