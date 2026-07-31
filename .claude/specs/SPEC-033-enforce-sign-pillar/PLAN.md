# PLAN — SPEC-033 Enforce the Sign Pillar

## Enrichment summary (from /deepen)

Three parallel `web-search-researcher` threads (2024-2026, four-pillar filtered) — full analysis in SDD → Research Insights. Net effect on the plan:

- **Design change — restricted-exec is NOT a security boundary.** AST-gate + RESTRICTED_BUILTINS is defeated by object-graph traversal / frame walking; RestrictedPython's own docs disclaim it as "not a sandbox," with recurring escape CVEs (incl. CVE-2025-22153). Every production untrusted-Python runner (codejail, E2B, Modal, Daytona) layers it inside OS/microVM isolation. **B1's `DynamicToolLoader` is re-scoped to a fast-fail linter; E1/SPEC-036 is promoted to a HARD dependency** — nothing agent-authored executes above personal outside the sandbox. This raises the stakes on Phase E: it is not optional hardening, it is *the* boundary.
- **Confirmed — signing model.** Ed25519 with the **agent's own DID key** (reuse `arctrust.keypair`, ADR-019) is correct for self-authored artifacts; Sigstore-keyless (OIDC/Fulcio) has no analog with no human/CI in the loop and is rejected for self-authored code (kept for install-time hub skills only). D2/D3 must record **honest signature semantics**: valid sig = "unmodified since write" + attribution, **not** "safe" — safety stays with TOFU (D1) + sandbox (E1). Optional: in-toto-shaped provenance payload inside the signed envelope.
- **Confirmed — verify-at-load** (C1/C2): kernel-module + `jarsigner` precedent; per-load Ed25519 verify is O(1), network-free. **Confirmed — TOFU** (D1): SSH `known_hosts` hash-pin + drift = hard-stop; OWASP MCP endorses it.
- **Confirmed + gaps flagged — WORM audit** (D4): signed hash-chain = RFC 9162 / Rekor pattern. Maps to NIST **SI-7, AU-9(3), AU-10, SA-10, SR-4, CM-14** (table in SDD). Four enforcement points the log alone does not cover, now called out: CM-3 pre-change gate = `PolicyPipeline` (not the log), CM-14 load-time sig check, **AU-9(2) audit store separate from skill-code store**, **SI-7(7) chain-verify failure wired to alerting/IR**. Add these as acceptance checks under G1.
- **Refinement — pluggable trust** (F2): make `TrustBackend` selection source/tier-aware (hub→Sigstore, self-authored/air-gap→Ed25519/DID); revocation = DID-Document / status-list.

No new tasks required; scope of E1 hardened (blocking, not optional), and G1 gains AU-9(2)/SI-7(7)/CM-14 assertions.

---

**Status:** PENDING
**Method:** TDD. Each task scoped to **one module** (`[pkg]` tag) and marked **WIRE** (wire existing, tested code — very-few-LOC) or **NEW** (net-new). Cross-module use via SDD contracts only.

**Progress:** 0 / 14 complete

**Depends on:** SPEC-036 (execution sandbox + personal-off) — Phase E is blocked on it.

## Wiring vs net-new (at a glance)
- **WIRE (reuse built+tested code):** B1 (`DynamicToolLoader`), B2/C1 (`TofuLayer`), C2 first-party-root verify, E1 (SPEC-036 exec routing).
- **NEW:** C3 (load-time re-verify factoring), D1 (TOFU gate), D2/D3 (sign agent artifacts + improver), D4 (WORM improver audit), F1/F2 (config relaxation + pluggable trust), cleanup rides inline.

---

## Phase A — arctrust verify/sign primitives `[arctrust]` (foundation)
- [ ] **A1** `[arctrust]` **NEW** — expose a reusable **content-hash + detached-signature verify** helper (over `keypair`/`AgentIdentity.sign`) that arcagent can call at load; confirm `SignedChainSink` is emit-ready for improver audit — REQ-011, REQ-021, REQ-022

## Phase B — harden the workspace load path `[arcagent]`
- [ ] **B1** `[arcagent]` **WIRE** — route `workspace`-root `.py` through `DynamicToolLoader.load` (RESTRICTED_BUILTINS + restricted `__import__`); delete the bare `exec(code, module.__dict__)` workspace path (`capability_loader.py:443`) in the same edit — REQ-001, REQ-060
- [ ] **B2** `[arcagent]` **WIRE** — call `TofuLayer.evaluate(CapabilitySource)` before register/execute for every discovered artifact; fail-closed on error (any exception → deny) — REQ-010

## Phase C — verify at load, not just install `[arcskill]`/`[arcagent]`
- [ ] **C1** `[arcskill]` **NEW** — factor the install-time verify core (`verify.py:181` / `installer.py:345`) into a reusable `verify_artifact_at_load`; keep federal-grade verify as the floor — REQ-011
- [ ] **C2** `[arcagent]` **WIRE** — on (re)load, re-verify Sigstore-signed skills (via C1) and arctrust-signed agent artifacts (via A1); require a valid signature for non-`workspace` first-party roots instead of skipping validation (`capability_loader.py:65,196-198`) — REQ-002, REQ-011

## Phase D — sign agent-authored artifacts + TOFU gate `[arcagent]`
- [ ] **D1** `[arcagent]` **NEW** — TOFU approval gate: on `Decision.NEW_SIGHTING`, refuse exec + request approval; approval writes `name`+`sha256` into `ValidatorsConfig.approved` (`core/config.py:267`); byte change re-triggers NEW_SIGHTING; each decision audited — REQ-030
- [ ] **D2** `[arcagent]` **NEW** — `create_tool`/`create_skill` sign written bytes with `arctrust` (agent identity) into a detached signature/manifest (`create_tool.py:52`, `create_skill.py:88`); load path (C2) verifies — REQ-020
- [ ] **D3** `[arcagent]` **NEW** — skill-improver `apply_result` signs the mutated skill on write (`engine.py:222`); reload verifies — REQ-021
- [ ] **D4** `[arcagent]` **NEW** — `candidate_store.append_audit` (`candidate_store.py:136-141`) routes through `arctrust.audit.emit` + `SignedChainSink`; remove the plaintext `audit.jsonl` writer — REQ-022, REQ-060

## Phase E — sandboxed execution `[arcagent]`/`[arcrun]` (blocked on SPEC-036)
- [ ] **E1** `[arcagent]` **WIRE** — route agent-authored code execution through the SPEC-036 tier-routed backend; never bare exec; honor personal-off (unsigned+unsandboxed only when a personal operator disables both); cede `os_sandbox.make_sandbox` to SPEC-036 (no second dead sandbox) — REQ-040

## Phase F — config relaxation + pluggable trust `[arcagent]`
- [ ] **F1** `[arcagent]` **NEW** — personal-only relaxation toggle (aligned with SPEC-036 personal-off): disables sign+verify with an audit WARN; rejected/ignored at enterprise+federal (verify still runs) — REQ-050
- [ ] **F2** `[arcagent]` **NEW (Should)** — `TrustBackend` verify Protocol with Sigstore-keyless + org-keypair implementations, config-selected; `capability_loader` depends only on the Protocol — REQ-051

## Phase G — acceptance & gates
- [ ] **G1** Security test: an unsigned/tampered workspace tool fails to load at enterprise+federal; a first-sight tool is NEW_SIGHTING-gated then runs after approval (and a later byte-change re-triggers NEW_SIGHTING — TOFU drift = hard stop); improver mutation is signed + WORM-audited; agent-authored code above personal executes only inside the SPEC-036 sandbox (never bare exec); personal-off allows unsigned on own machine. **NIST assertions (research-driven):** the `SignedChainSink` store is separate from the mutable skill-code store (AU-9(2)); a chain-verify failure raises a `PolicyPipeline`/alert event, not just a log line (SI-7(7)); a mutated skill fails load without a valid signature (CM-14) — REQ-001,010,011,020,021,022,030,040,050
- [ ] **G2** Gates: `ruff` 0, `mypy --strict` 0 (all touched pkgs), suites green (incl. the pre-existing `DynamicToolLoader`/`TofuLayer`/restricted-builtins suites now exercising the live path); `grep` finds no bare `exec(code, module.__dict__)` on the workspace path and no plaintext improver audit writer; LOC checked — REQ-060

---

## Sequencing
- **A before B/C** (arcagent needs the verify helper). **B1 before B2** (load path exists before TofuLayer gates it). **C1 before C2** (arcskill exposes load-time verify before arcagent calls it). **D2/D3 before C2 can fully verify agent artifacts** (sign-on-write must exist before load-verify has something to check) — implement sign (D2/D3) and verify (C2) together, test end-to-end. **E1 blocked on SPEC-036.** F1 is Must; F2 is Should.
- Cleanup (REQ-060) rides inline with B1 and D4 — delete the superseded `exec` path and plaintext audit writer in the same edits.

## Definition of Done
No unsigned/unrestricted agent-authored code path executes above personal: workspace `.py` loads through `DynamicToolLoader`; signatures are re-verified at load; agent-authored + improver artifacts are signed on write and verified on reload; first-sight workspace code is TOFU-gated and audited; improver audit is on the signed WORM chain; execution routes through the SPEC-036 sandbox; relaxation is possible only at personal via explicit config; `ruff` + `mypy --strict` green; no dual paths, no shims.
