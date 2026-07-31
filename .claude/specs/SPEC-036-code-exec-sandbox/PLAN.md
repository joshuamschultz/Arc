# PLAN — SPEC-036 Real Code-Execution Sandbox

**Status:** PENDING
**Method:** TDD (failing test → implement → verify). Each task scoped to **one module** (`[pkg]` tag). Cross-module use via SDD contracts only. Smallest correct change; reuse the existing container path; delete superseded code in the same edit (no compat shims).

**Progress:** 0 / 12 complete

## Enrichment summary (from /deepen)

Three parallel research streams confirmed the core design and tightened five build details (full detail + citations in SDD → Research Insights). Adopt these when implementing:
- **A1/A2 — Firecracker stays the default `vm` engine, gVisor is the documented alternative** (field consensus + CVE record: hardware-KVM boundary beats syscall interception). The engine MUST launch via the **jailer** + **seccomp level 2**, never bare `firecracker` — the one 2026 Firecracker CVE was a jailer bug. Set `cold_start_budget_ms ≈ 200` (cold Firecracker ~125-200ms is *below* container's 800ms; pooled-snapshot ~10-30ms is the future fleet optimization, out of scope).
- **A2 — federal on no-KVM = refuse, not silent gVisor swap.** gVisor is not hardware-VM class; it doesn't satisfy the federal floor. KVM-capable Linux is a federal deployment **prerequisite**. (Verify gVisor aarch64 before offering an ARM gVisor fallback.)
- **B1 — router is a PURE function:** `platform_supports_vm` is an **injected** fact (the backend's own KVM check is defence-in-depth, not the routing decision); below-floor `relax` at enterprise/federal **raises immediately**; refusal is a **distinct type**, never a "none" backend value.
- **C1/D1 — audit on every resolution, not only downgrades** (a successful federal→vm is an AU-2 event). Event carries `caller_did`, tier, requested/resolved backend, `relax` **+ reason**, `platform_supports_vm`, outcome (AU-3). Router does not emit; the caller does (single emission point).
- **B2/C2 — dev fallback pattern (OpenCode prior art):** pick most-restrictive-available, **throw** on unavailable-explicit backend (never silent-degrade-to-none), agent config may only *escalate*. macOS container fallback protects the host but is a genuine downgrade (container-in-a-VM ≠ microVM) — label + audit it; federal path never reaches the container branch (config-time literal deny). NIST mapping: SC-39(1)/SC-7/AC-6/SI-3/SC-3 + AU-2/3/12.

## Design anchors (from SDD)
- **Router is a pure function** `resolve_execution_backend(tier, relax, platform_supports_vm) -> backend name`; the single owner of the tier→isolation decision. `make_execute_tool` delegates to a backend, never inlines subprocess.
- **VM backend behind the Protocol** (`ExecutorBackend`, `isolation="vm"`), Firecracker/KVM default, gVisor/`runsc` documented alt; **fail-closed** on no-KVM/non-Linux — never a weaker substitute.
- **Reuse, don't rebuild** — the container floor is `DockerBackend`/`contained_execute` (cap-drop ALL, no-new-priv, no-net, read-only, non-root, limits); VM reuses that posture.
- **Tier flooring:** federal→vm (hard), enterprise→container, personal→container (relax to container/local via explicit config only). Downgrades audited.
- **arcrun stays execution-only** — receives `tier`/`relax`, never sources them.

---

## Phase A — VM backend `[arcrun]` (foundation)
- [ ] **A1** `[arcrun]` `VmBackend` in `backends/vm.py`: implements `ExecutorBackend` (`run/stream/cancel/close`, `name="vm"`, `capabilities.isolation="vm"`, realistic `cold_start_budget_ms`); Firecracker/KVM engine behind an internal seam; guest posture reuses the container deny-by-default surface — REQ-001, REQ-004, REQ-031
- [ ] **A2** `[arcrun]` fail-closed availability probe (`/dev/kvm` + Linux) → typed `VmUnavailableError`; document gVisor/`runsc` as the Protocol-satisfying alternative engine — REQ-002, REQ-003
- [ ] **A3** `[arcrun]` register `vm` as a built-in in `load_backend._try_builtin` (`loader.py:183-193`); trusted at all tiers, no manifest — REQ-001

## Phase B — Tier router + `execute_python` rewire `[arcrun]`
- [ ] **B1** `[arcrun]` `resolve_execution_backend(tier, relax, platform_supports_vm)` — single routing fn: federal→vm (refuse if unsupported), enterprise→container, personal→container/relax; enterprise/federal reject below-floor relax — REQ-010, REQ-011, REQ-012, REQ-020, REQ-021
- [ ] **B2** `[arcrun]` non-Linux/no-KVM guard inside the router: non-federal → container (downgrade notice), federal → refuse — REQ-030
- [ ] **B3** `[arcrun]` `make_execute_tool` calls the router + `load_backend`, delegates execution to the backend (`run_separated`-shaped result), removes `_ = tier` and the inline host subprocess (`execute.py:43,50-88`); public tool surface unchanged — REQ-010, REQ-040, REQ-070

## Phase C — Config relaxation + caller wiring `[arccli]`/`[arcagent]`
- [ ] **C1** `[arcagent]`/`[arccli]` explicit `execution.relax_isolation` config field (default unset) resolved from agent config; passed as `relax` to `make_execute_tool` — REQ-020
- [ ] **C2** `[arccli]` `agent/tools.py:18-20` and `commands/run.py:122,259` build `make_execute_tool(tier=<agent tier>, relax=<relax>)`; ad-hoc `arc run exec` defaults to personal — REQ-050
- [ ] **C3** `[arcrun]` boundary test: `make_execute_tool`/router import nothing from arcagent/arcllm; tier arrives as a parameter (arcrun stays execution-only) — REQ-040

## Phase D — Audit + cleanup
- [ ] **D1** `[arcrun]` emit `code_exec.backend.selected` on every build and `code_exec.isolation.downgraded` on any tier-permitted downgrade, via the existing `_audit.py`/`load_backend(audit_sink=…)` path — REQ-060
- [ ] **D2** `[arcagent]` reconcile the dead `core/os_sandbox.py` seccomp stub (delete un-called `NotImplementedError` path per no-legacy rule, or single explicit "arcrun backends are the ASI05 surface" note); remove the superseded "tier ignored" comment — REQ-070

## Phase E — Acceptance & gates
- [ ] **E1** Integration test: a **federal** agent's `execute_python` runs via the VM backend on Linux/KVM and **refuses** on non-Linux; **enterprise** runs container and never bare subprocess; **personal** defaults container, relaxes to local only with explicit config; each selection/downgrade audited — REQ-003, REQ-010, REQ-011, REQ-020, REQ-030, REQ-060
- [ ] **E2** Gates: `ruff` 0, `mypy --strict` 0 (arcrun + touched arccli/arcagent), suites green; no code path yields `isolation="none"` at enterprise/federal; LOC checked (reuse keeps net small) — REQ-070

---

## Sequencing
- **A before B** — the router refuses to route to a `vm` backend that doesn't exist; build + register it first.
- **B before C** — callers can't pass tier/relax into a router that isn't there.
- **C before D/E** — audit + acceptance exercise the end-to-end wired path.
- Cleanup (REQ-070) rides inline with B3 (remove the ignored-tier no-op in the same edit that replaces it) and D2 (os_sandbox reconciliation).

## Definition of Done
Sandboxed execution is the default and tier-enforced; a real `isolation="vm"` backend exists behind the Protocol (Firecracker/KVM, gVisor documented alt); enterprise/federal never fall back to bare subprocess and fail closed when isolation is unavailable; personal may opt down only via explicit, audited config; macOS dev degrades to container (federal refuses); every selection/downgrade is audited; `mypy --strict` + `ruff` green; ASI05 gap closed.
