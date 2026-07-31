# SDD — SPEC-033 Enforce the Sign Pillar

## Module boundaries (hard contracts)

| Module | Owns | Contract | MUST NOT |
|--------|------|----------|----------|
| **arctrust** | Sign/verify primitives + tamper-evident audit | `keypair.sign`/verify, content-hash verify, `SignedChainSink` (WORM), `audit.emit` | import arcagent/arcskill; know about capability roots or TOFU policy |
| **arcskill** | Sigstore / Rekor verification | expose a **load-time** verify entry point (not only install-time `verify_bundle`) | own the capability load loop; decide tier policy |
| **arcagent** | Wire the load/execute path | route workspace `.py` → `DynamicToolLoader`; call `TofuLayer`; call load-time verify; sign `create_tool`/`create_skill`/improver writes; route exec → SPEC-036 | reimplement crypto (use arctrust); reimplement Sigstore (use arcskill); run bare `exec` above personal |
| **arcrun** *(via SPEC-036)* | Tier-routed execution sandbox | SPEC-036 `ExecutorBackend` router + personal-off | verify signatures; own capability policy |

**Dependency edges:** `arcagent → arctrust` (exists), `arcagent → arcskill` (exists), `arcagent → arcrun` (exists, execution). No new package. arctrust stays a leaf.

**Ownership note:** `TofuLayer` and `ValidatorsConfig` live in `arcagent/core` today (per-agent tier policy is an arcagent concern); the *crypto they consult* (sign/verify/hash) belongs to arctrust. This split is preserved — arcagent decides *whether* to allow; arctrust decides *whether the bytes are authentic*.

---

## Current state (from investigation — file:line)

- **Plain-`exec` load, workspace-only AST.** `capability_loader._load_module` does `code = compile(...)` then `exec(code, module.__dict__)` with full builtins (`capabilities/capability_loader.py:434-443`). AST validation is gated on `_UNTRUSTED_ROOTS = frozenset({"workspace"})` (`:65`) at `_register_python_file` (`:196-198`); `builtins`/`global`/`agent` roots skip it and **no** root gets runtime builtin restriction.
- **Hardened loader exists, unwired.** `tools/_dynamic_loader.py:485-710` — `DynamicToolLoader.load(source, *, name)` (`:523`) compiles into a namespace built from `RESTRICTED_BUILTINS` (`:465`) with a wrapped `__import__` (`_make_restricted_import`, `:657`; injected `:606`); AST layer rejects blocked imports/attrs (CVE-2023-37271 / -2024-47532 / -2025-22153 class). Grep: only its own definition + `__all__` export — **zero production callers**.
- **`TofuLayer` exists, unwired.** `core/tofu_layer.py:56-96` — `evaluate(target: CapabilitySource) -> Decision`: FEDERAL → `ALLOW` iff `target.signed` else `DENY`; ENTERPRISE → approved-hash match `ALLOW` / mismatch `DENY` / unknown `NEW_SIGHTING`; PERSONAL → `ValidatorsConfig.auto_run_agent_code` toggle. Grep: definition + `__all__` only — **zero production call sites**.
- **`ValidatorsConfig`** at `core/config.py:267` — carries `approved` (name→hash entries) and `auto_run_agent_code`; the exact inputs `TofuLayer` and the TOFU gate need.
- **Sigstore verify is install-time, tier-skippable.** `arcskill/hub/verify.py` (775 LOC) — `verify_bundle` (`:181`) does full Fulcio + Rekor; tier gate at `:411` (`config.is_federal or config.policy.require_signature`); non-federal skips when `sigstore` absent (`:57-63`). Only caller is `installer.py:345` — **no load-time re-verify**.
- **Agent-authored artifacts unsigned.** `builtins/capabilities/create_skill.py:69,88` writes `SKILL.md` (no signature); `builtins/capabilities/create_tool.py:31,49,52` AST-validates then `write_text` (no signature).
- **Improver mutations unsigned; audit plaintext.** `modules/skill_improver/engine.py:207` `apply_result` → `atomic_write_text` (`:222`) then `append_audit` (`:244`); `candidate_store.py:136-141` `append_audit` writes plaintext `audit.jsonl` — not the WORM/`SignedChainSink`.
- **`os_sandbox` unwired; SPEC-036 owns execution.** `core/os_sandbox.py` — `OsSandbox` Protocol (`:68`), `make_sandbox` (`:80`); zero production callers. SPEC-036 routes `execute_python` by tier (federal→VM, enterprise→container, personal→container, personal-off relaxable).
- **arctrust primitives available:** `keypair.py:34 KeyPair`, `keypair.py:80 sign`, `identity.py:197 AgentIdentity.sign`, `audit.py:484 emit`, `JsonlSink`/`SignedChainSink` (`audit.py`).

---

## Component design

### C1 · Hardened workspace load — *arcagent* `capability_loader.py` (REQ-001, REQ-060) — **WIRE**
- Replace the workspace branch of `_load_module` (`capability_loader.py:434-443`) so workspace `.py` source is compiled/executed by `DynamicToolLoader.load(source, name=…)` (`tools/_dynamic_loader.py:523`) — RESTRICTED_BUILTINS + wrapped `__import__` — instead of bare `exec`. Non-workspace roots keep the trusted import path **plus** load-time verify (C2).
- Delete the bare `exec` workspace path in the same edit (REQ-060); no dual path.
- **Scope correction (research-driven):** `DynamicToolLoader` is **defense-in-depth / a fast-fail linter, NOT the security boundary.** AST-gate + RESTRICTED_BUILTINS is defeatable by object-graph traversal (`().__class__.__bases__[0].__subclasses__()`, frame walking) — a denylist over Python's non-encapsulated object graph, disclaimed as "not a sandbox" by RestrictedPython's own maintainers. The **actual** trust boundary for executing agent-authored code is the SPEC-036 OS/microVM sandbox (C7), which becomes a **hard** dependency, not a soft one. C1 cheaply rejects obvious/sloppy code and gives good errors; it must never be relied on as containment.
- **Pillar:** Security (fast-fail hygiene, boundary deferred to C7) + Simplicity (wire existing, very-few-LOC; do NOT grow a bespoke in-process security model — that complexity belongs in the sandbox).

### C2 · TofuLayer + load-time verify wiring — *arcagent* `capability_loader.py`, *arcskill* verify (REQ-002, REQ-010, REQ-011) — **WIRE + NEW**
- Build a `CapabilitySource` per discovered artifact (name, source bytes, `signed` flag from C4/C5 signature presence) and call `TofuLayer.evaluate` (`core/tofu_layer.py`) before register/execute; fail-closed (any exception → deny). **[WIRE]**
- `signed` is resolved by **re-verifying at load**: for Sigstore-signed skills, call a new arcskill load-time verify (factor the verify core out of `installer.py:345`'s one-shot into a reusable `verify_artifact_at_load`); for arctrust-signed agent artifacts (C4/C5), verify the detached signature/content-hash via arctrust. **[NEW]**
- Non-`workspace` first-party roots (REQ-002) require a valid signature here rather than skipping validation. **[WIRE]**
- Federal-grade verify is the floor (`require_signature` default true); personal may relax (C6).
- **Pillar:** Security (verify at use, not just install) + Modularity (arcskill owns Sigstore, arctrust owns hash/sig, arcagent orchestrates).

### C3 · TOFU approval gate — *arcagent* `capability_loader.py` + `core/config.py` (REQ-030) — **NEW**
- On `Decision.NEW_SIGHTING` (first sight of a workspace `.py`, enterprise), refuse execution and surface an approval request; approval writes the source's `name`+`sha256` into `ValidatorsConfig.approved` (`core/config.py:267`) so the next `TofuLayer.evaluate` returns `ALLOW`. A byte change re-triggers `NEW_SIGHTING` (hash mismatch). Every decision (`NEW_SIGHTING`/approve/deny) emits `arctrust.audit.emit`.
- **Pillar:** Security (trust-on-first-use, human-gated) + Scalability (O(1) approved-hash lookup, already `TofuLayer`'s shape).

### C4 · Sign agent-authored writes — *arcagent* `create_tool.py`/`create_skill.py` (REQ-020) — **NEW**
- After `create_tool` (`create_tool.py:52`) and `create_skill` (`create_skill.py:88`) write the artifact, sign the bytes with the agent's `arctrust.keypair`/`AgentIdentity.sign` (`arctrust/identity.py:197`) into a detached signature/manifest beside the artifact. The load path (C2) verifies it on (re)load.
- **Signer = the agent's own DID key** (research-confirmed): Sigstore-keyless (Fulcio+OIDC) has no analog for an artifact the agent writes locally with no human/OIDC in the loop; DID-scoped Ed25519 directly reuses the identity primitive ADR-019 already mandates. Optionally carry an **in-toto-shaped provenance payload** (subject = artifact hash, builder = agent DID, invocation = task/model context) *inside* the Ed25519-signed envelope for structured attribution.
- **Honest signature semantics (must be stated in the spec, not implied away):** a valid self-signature proves "unmodified since this agent wrote it" + attribution/non-repudiation — it does **NOT** prove the content is safe. A compromised/manipulated agent signs malicious code with a perfectly valid key. The safety judgment therefore stays with (a) the TOFU baseline+drift gate (C3) and (b) the execution sandbox (C7) + `PolicyPipeline`. Signature is necessary, explicitly insufficient alone.
- **Pillar:** Security (tamper-since-write detection + attribution, not a safety verdict) + Modularity (arctrust signs, arcagent orchestrates).

### C5 · Sign + WORM-audit improver mutations — *arcagent* `skill_improver/engine.py` + `candidate_store.py` (REQ-021, REQ-022) — **NEW**
- `apply_result` signs the mutated skill text (same primitive as C4) at write (`engine.py:222`); load path verifies on reload.
- `candidate_store.append_audit` (`candidate_store.py:136-141`) routes mutation events through `arctrust.audit.emit` + `SignedChainSink` (WORM) instead of the plaintext `audit.jsonl`; remove the plaintext writer (REQ-060).
- **Chain design (research-confirmed):** each entry references the prior entry's hash (RFC 9162 / Certificate-Transparency / Rekor pattern), signed with an Ed25519 key; this single structure gives tamper-evidence + non-repudiation. Two enforcement details the log alone does **not** cover, to be honored here: (1) **AU-9(2)** — the `SignedChainSink` store must be logically separate from the mutable skill-code store (one compromise must not defeat both); (2) **SI-7(7)** — chain-verification failure must wire into `PolicyPipeline`/alerting (incident response), not just sit as an unread log line.
- **Pillar:** Security + Audit (tamper-evident chain; satisfies NIST AU-9(3), AU-10, SI-7, SR-4, SA-10 — see Research Insights mapping).

### C6 · Config-relaxable + pluggable trust — *arcagent* `core/config.py`, verify interface (REQ-050, REQ-051) — **NEW**
- Personal-only relaxation: a config toggle (aligned with SPEC-036 personal-off) disables sign+verify with an audit WARN; at enterprise/federal the toggle is rejected/ignored and verify still runs (`ValidatorsConfig`/tier check). **[NEW, Must]**
- Pluggable trust: a `TrustBackend` verify Protocol with two implementations — Sigstore keyless (arcskill) and org/DID keypair (arctrust) — config-selected; `capability_loader` depends only on the Protocol. **[NEW, Should]**
- **Source-aware selection (research-driven):** Sigstore for **install-time hub skills** (real external provenance exists there); Ed25519/DID for **agent-authored artifacts and air-gapped/federal** deployments (network-free, and a private Rekor is "just an org keypair with extra infra"). Revocation is a DID-Document / VC-status-list action (operator kill-switch), not a signature-scheme change.
- **Pillar:** Modularity (swap trust without touching the loader) + Simplicity (one Protocol; no offline transparency-log infra where a local DID root suffices).

### C7 · Sandboxed execution — *arcagent* → *arcrun/SPEC-036* (REQ-040) — **WIRE (HARD depends SPEC-036)**
- Route agent-authored code execution through SPEC-036's tier-routed `ExecutorBackend`; never bare `exec`/host subprocess. Honor SPEC-036 personal-off (unsigned + unsandboxed allowed only when a personal operator has explicitly disabled both). Cede/retire `os_sandbox.make_sandbox` (`os_sandbox.py:80`) to SPEC-036 so there is not a second dead sandbox.
- **Dependency hardened (research-driven):** SPEC-036 is the **actual security boundary**, not a nice-to-have. Every production system that runs untrusted Python (openedx/codejail, E2B, Modal, Daytona) treats language-level restriction as inert unless wrapped in OS/microVM isolation; OWASP ASI05 (already in Arc's threat table) mandates it. Firecracker-class cold-start (~125ms) fits Arc's <500ms budget, so there is no scalability excuse to skip it. Above personal, agent-authored code executes **only** inside the SPEC-036 sandbox — C1's `DynamicToolLoader` is the fast-fail linter *in front of* it, never a substitute.
- **Pillar:** Security (isolation is SPEC-036's job and is the real boundary; this spec ensures *nothing unsigned reaches it* above personal).

---

## Enforcement matrix (tier × control)

| Control | Personal | Enterprise | Federal |
|---------|----------|------------|---------|
| Workspace `.py` via `DynamicToolLoader` (C1) | yes (relaxable) | yes | yes |
| Load-time signature verify (C2) | yes (relaxable) | yes | yes (floor) |
| TOFU approval on first sight (C3) | toggle (`auto_run_agent_code`) | NEW_SIGHTING gate | signed-only |
| Agent-authored artifacts signed (C4/C5) | yes (relaxable) | yes | yes |
| Improver audit → WORM chain (C5) | yes | yes | yes |
| Execution sandbox (C7, SPEC-036) | container (relaxable off) | container | VM |
| Relaxation allowed | yes (explicit config) | no | no |

## Security posture
Fail-closed everywhere: evaluation errors deny; missing/invalid signature above personal blocks load; verify runs at load (not just install); first-sight workspace code is human-gated; agent-authored + improver output is signed and re-verified; improver audit is tamper-evident (`SignedChainSink`). Personal relaxation is explicit, audited, and impossible to reach at enterprise/federal.

## Scalability posture
Per-agent, shared-nothing verification; TOFU is an O(1) approved-hash map (`TofuLayer` already this shape); load-time verify runs on discovery/reload (edge-triggered), not a polling loop; signing is a single Ed25519 op per write.

---

## Research Insights (from /deepen)

Three parallel `web-search-researcher` threads (2024-2026 sources), each filtered through the four pillars. Verdicts below are per-component: **scalability ceiling / security posture / module boundary**.

### C1 + C7 · Restricted-exec is NOT a boundary — sandbox is (design change)
- **Finding.** AST-gate + RESTRICTED_BUILTINS + wrapped `__import__` is defeated by object-graph traversal (`().__class__.__bases__[0].__subclasses__()` reaches `subprocess`/`os` with no `import` statement), frame/`__globals__` walking, and format-string reaches. It is a **denylist over Python's non-encapsulated object graph** — new dunders/indirection keep reopening it, and RestrictedPython has shipped repeated escape CVEs (CVE-2023-37271, -2024-47532, **CVE-2025-22153**, patched by *removing* the language feature). RestrictedPython's own docs: *"not a sandbox system or a secured environment."* Checkmarx "Glass Sandbox": *"Don't rely on Python's scope restrictions — use real isolation boundaries."* Every production runner of untrusted Python (openedx/codejail = AppArmor + unprivileged user + rlimits; E2B/Modal/Daytona 2025-26 = Firecracker microVM / gVisor) layers restriction *inside* OS/microVM isolation, never as the sole control. Firecracker ~125ms cold-start fits Arc's <500ms budget.
- **Cited:** Checkmarx "Glass Sandbox"; RestrictedPython upstream docs + CVE-2025-22153 (Wiz/SentinelOne); HackTricks "Bypass Python sandboxes"; openedx/codejail; Modal/Spheron/Manveer C. AI-agent sandbox guides (2026).
- **Verdict.** *Scalability ceiling:* none added — microVM boot is within budget, so isolation is free of a scalability excuse. *Security posture:* restricted-exec alone is **inadequate as a boundary**; treat `DynamicToolLoader` as defense-in-depth / fast-fail linter only. *Module boundary:* clean — static validation in `DynamicToolLoader` (arcagent), containment in SPEC-036 (arcrun); never blur. **Design change applied:** C1 re-scoped to linter; **C7/SPEC-036 promoted to a HARD dependency** — nothing agent-authored executes above personal outside the sandbox. (OWASP ASI05, already in Arc's threat table, mandates this.)

### C2 · Verify-at-load, not just install (confirmed)
- **Finding.** Re-verifying every load is standard: the Linux kernel checks module signatures **at load time** against compiled-in keys (`sig_enforce` rejects regardless of install state); Java `jarsigner` re-verifies against JAR contents on load. Rationale is uniform: install-time and load-time are different trust boundaries — a signed artifact can be tampered on disk in between. Ed25519 verify is microseconds and needs no network when the key/cert is cached locally.
- **Cited:** kernel.org module-signing docs + systemshardening.com; Oracle `jarsigner` tutorial.
- **Verdict.** *Scalability ceiling:* negligible — per-load Ed25519 verify is O(1), no network. *Security posture:* strong; matches kernel/JVM precedent and Arc Pillar 2. *Module boundary:* arcskill owns Sigstore load-time verify, arctrust owns hash/detached-sig verify, arcagent orchestrates — as specified. No change.

### C3 · TOFU baseline + drift (confirmed, complements the signature)
- **Finding.** SSH `known_hosts` model generalizes: hash-pin on first approval, silently verify on subsequent loads, hard-stop on drift (rug-pull detection). OWASP MCP guidance recommends exactly this — *"pinning tool definitions using cryptographic hashes and alerting on any changes"* + explicit user confirmation for sensitive ops. TOFU governs *provenance/integrity*, not runtime containment — it does not replace the sandbox (C7).
- **Cited:** Wikipedia "Trust on first use"; OWASP MCP Security Cheat Sheet + MCP03:2025 Tool Poisoning.
- **Verdict.** *Scalability ceiling:* O(1) approved-hash map — already `TofuLayer`'s shape. *Security posture:* closes the residual "compromised-agent-signs-its-own-code" gap that a bare valid signature cannot (baseline+drift). *Module boundary:* per-agent tier policy in arcagent, crypto in arctrust — as specified. No change; adds explicit "drift = hard stop" semantics.

### C4 + C5 (sign) · DID-scoped Ed25519, honest semantics (confirmed choice + clarification)
- **Finding.** Sigstore-keyless (Fulcio+Rekor) binds a short-lived cert to an **OIDC identity** asserted by a CI provider — npm (GA Jul 2025) and PyPI (GA Nov 2024) provenance follow this. An agent writing code locally has **no OIDC issuer, no CI job, no human** — keyless's core value (externally-attestable "who signed") has no analog. DID-scoped Ed25519 (`arctrust.keypair`, already ADR-019-mandated) is the smaller, correct mechanism. in-toto's builder/subject/predicate schema maps cleanly onto "agent = builder" and is worth adopting as the *payload shape* inside the signed envelope — but the 2026 TanStack attack (malicious package with **valid** SLSA provenance) proves signing ≠ trust. GitHub agentic-security research: malicious skill behavior *"only appears once the skill runs after scanning has passed."* Self-signature = tamper-since-write + attribution; it does **not** detect a compromised agent signing its own malicious output.
- **Cited:** Sigstore docs + npm/PyPI provenance GA blogs; in-toto/SLSA provenance (Legit Security, sbomify); Cloudsmith 2026 supply-chain guide (TanStack); GitHub Secure Code Game; arXiv 2509.18415 (NHI context lineage).
- **Verdict.** *Scalability ceiling:* one Ed25519 op per write — trivial. *Security posture:* correct **only if scoped honestly** — signature proves "unmodified since write," not "safe"; pair with TOFU (C3) + sandbox/policy (C7). *Module boundary:* arctrust signs with agent DID, arcagent orchestrates — as specified. **Refinement applied:** C4 now states signature semantics explicitly and offers in-toto payload shape; Sigstore-keyless explicitly rejected for self-authored artifacts.

### C5 (audit) · Signed hash-chain WORM + NIST mapping (confirmed, gaps flagged)
- **Finding.** Signed, hash-linked, append-only log is the RFC 9162 / Certificate-Transparency / Rekor pattern — tamper-evidence + non-repudiation in one structure; plaintext JSONL can be silently truncated/reordered/edited. Maps to NIST 800-53r5 as below. No current agentic-AI framework (CSA Agentic NIST AI RMF Profile 2026, NIST AI RMF, OMB M-25-21) specifies tamper-evident/self-modification audit — Arc is **ahead of** guidance and must derive satisfaction from 800-53 directly.

  | Control | Requirement | Arc mechanism |
  |---|---|---|
  | **SI-7 / 7(1)/7(7)** | Detect unauthorized software changes at config-change events; wire to IR | Each create/update/mutation = a signed chain entry (the integrity-check event); chain-verify failure → `PolicyPipeline`/alerting |
  | **AU-9(2)/(3)** | Protect audit info; separate storage; crypto (signed hash) | `SignedChainSink`: Ed25519-signed, hash-linked; store **separate** from mutable skill code |
  | **AU-10** | Non-repudiation (signatures, chain of custody) | Entry signed by agent DID + hash-chain preserves order/custody |
  | **SA-10 / SR-4** | Developer config mgmt; provenance across lifecycle | Diff + rationale + prior-hash per entry = full artifact provenance |
  | **CM-3 / CM-5 / CM-14** | Pre-change approval; authorized changers; signed components | Chain = the *document/audit* leg; **pre-change gate = `PolicyPipeline` deny-by-default**; load-time sig check = CM-14 |
- **Cited:** RFC 9162; Sigstore Rekor / Chainguard; NIST SP 800-53r5 (CSF Tools SI-7/AU-9/AU-10/SA-10/SR-4/CM-3/5/14); FedRAMP AI + ConMon Playbook v1.0 (Nov 2025); CSA Agentic NIST AI RMF Profile; OMB M-25-21.
- **Verdict.** *Scalability ceiling:* append-only, no lock contention, O(1) verify per entry. *Security posture:* satisfies AU-9/AU-10/SR-4/SA-10 and the evidentiary half of SI-7/CM-3 fully; **gaps that must live elsewhere** — CM-3 *pre*-change gate (PolicyPipeline, not the log), CM-14 load-time sig check on the mutated artifact, AU-9(2) separate store, SI-7(7) IR wire. *Module boundary:* rides the existing `arctrust.audit.emit` single emission point — no caller changes. **Refinement applied:** C5 now names the four non-log enforcement points; PRD/PLAN already cover PolicyPipeline gating + load-verify.

### C6 · Pluggable trust — Sigstore for hub-install, Ed25519/DID for self-authored (refinement)
- **Finding.** For **air-gapped/federal**, Sigstore degrades: Rekor needs network; workarounds are stapled inclusion proofs or a **private** Fulcio/Rekor — which is "materially just an org keypair with extra infrastructure" (Chainguard "Busting 5 Sigstore Myths"). Local Ed25519 verify against a locally-distributed DID-document/public-key root needs no network at any tier. Agent key custody/revocation is a DID-Document/VC-status-list action (operator kill-switch), matching Arc's per-agent DID design.
- **Cited:** Chainguard "Busting 5 Sigstore Myths"; Strata agentic-identity 2026; Keyfactor Zero-Trust for Agentic AI.
- **Verdict.** *Scalability ceiling:* Ed25519 path is network-free and air-gap-native; Sigstore adds infra with no gain offline. *Security posture:* keep **both** behind the `TrustBackend` Protocol — Sigstore for **install-time hub skills** (arcskill already does this, real external provenance there), Ed25519/DID for **agent-authored + air-gapped** artifacts. *Module boundary:* `capability_loader` depends only on the Protocol — as specified. **Refinement applied:** C6 backend selection is tier/source-aware (hub→Sigstore, self-authored/air-gap→Ed25519); revocation = DID-Document/status-list, not a scheme change.
