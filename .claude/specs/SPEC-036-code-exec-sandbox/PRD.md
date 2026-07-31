# PRD — SPEC-036 Real Code-Execution Sandbox

**Format:** EARS. Each requirement carries a governing **pillar** and a pillar-tied acceptance criterion. IDs are monotonic `REQ-NNN`. MoSCoW priority.

---

## Goal & non-goals

**Goal:** sandboxed code execution is the **default and tier-enforced** in arcrun, with a real `isolation="vm"` backend, so agent-generated code never runs on the bare host except when a personal-tier operator explicitly opts down — closing ASI05.

**Non-goals (this spec):** implementing `arcagent/core/os_sandbox.py`'s seccomp backend (separate arcagent concern; noted here only as dead code to reconcile); the `remote` isolation backend; non-Python runtimes; per-agent container lifecycle tuning beyond what `DockerBackend` already provides.

---

## Requirements

### VM isolation backend — *Must*

- **REQ-001** *(Modularity)* arcrun SHALL provide a real `isolation="vm"` backend that implements the existing `ExecutorBackend`/`BackendCapabilities` Protocol (`backends/base.py`) and is resolvable as a built-in (`vm`) by `load_backend`.
  - *Accept:* `isinstance(VmBackend(), ExecutorBackend)` holds; `VmBackend().capabilities.isolation == "vm"`; `load_backend("vm", tier=...)` returns it without a manifest (built-in, trusted like `local`/`docker`).
- **REQ-002** *(Modularity)* The VM backend SHALL be pluggable behind the Protocol so the isolation engine (Firecracker/KVM on Linux, or gVisor/`runsc`) is swappable without changing `execute.py` or the router.
  - *Accept:* swapping the VM engine implementation touches only the backend module; `make_execute_tool` and its callers are unchanged; a documented gVisor/`runsc` alternative satisfies the same Protocol.
- **REQ-003** *(Security)* WHEN the VM backend is constructed or run on a host without the required hypervisor (no `/dev/kvm`, non-Linux), it SHALL fail closed with a typed, explicit error and SHALL NOT execute code by any weaker path.
  - *Accept:* on a host with no KVM, VM execution raises a typed unavailability error; no subprocess or container is silently substituted.
- **REQ-004** *(Simplicity)* The VM backend SHALL reuse the existing hardened container posture as its reference (cap-drop ALL, no-new-privileges, no network, read-only rootfs, non-root, resource limits) and SHALL be very-few-LOC, adding no isolation logic to `execute.py`.
  - *Accept:* the VM backend carries the same deny-by-default network/filesystem/privilege posture as `contained_execute.py`; `execute.py` gains a router call, not isolation code.

### Tier routing — *Must*

- **REQ-010** *(Security)* `make_execute_tool` SHALL route to a backend **by tier** — federal → `vm`, enterprise → `container`, personal → `container` (default) — instead of accepting and ignoring `tier`.
  - *Accept:* the `_ = tier` no-op at `execute.py:43` is gone; a federal tool executes via the VM backend, an enterprise tool via the container backend, a personal tool via the container backend by default (verified by the selected backend's `capabilities.isolation`).
- **REQ-011** *(Security)* The default execution path SHALL NEVER be a bare local subprocess at enterprise or federal tier; IF the tier-required isolation backend is unavailable, execution SHALL fail closed.
  - *Accept:* at enterprise/federal, with the required backend unavailable, an execution attempt returns a typed refusal (not stdout from a host subprocess); no code path yields `isolation="none"` at these tiers.
- **REQ-012** *(Simplicity)* Backend selection SHALL be a single routing function that maps `(tier, config)` → backend name, consumed by `make_execute_tool`; there SHALL be no tier-string branching scattered through execution logic.
  - *Accept:* one function owns the tier→backend decision; `grep` finds no ad-hoc `if tier == "federal"` inside `execute.py`'s run path beyond that function.

### Config relaxation — *Must*

- **REQ-020** *(Security)* A **personal-tier** operator MAY relax isolation — down to and **including fully OFF** (`isolation="none"`, the `local` backend = full host/computer access) — but ONLY via an explicit config setting, never silently and never as an implicit fallback. "Sandbox off" is a **first-class, supported personal mode** (a user's own agent operating on their own machine), not a degraded edge case; it must read cleanly in config (e.g. `sandbox = "off"`).
  - *Accept:* with no relaxation config, personal defaults to `container`; setting the explicit relax option to `off`/`none`/`local` yields `LocalBackend` (`isolation="none"`) and the agent genuinely reaches the host filesystem outside any container; the choice is recorded (REQ-060). Enterprise/federal cannot select this mode (REQ-021).
- **REQ-021** *(Security)* Enterprise and federal tiers SHALL NOT be relaxable below their isolation floor (container for enterprise, VM for federal) by any config value.
  - *Accept:* a relax-to-`local` config at enterprise/federal is rejected/ignored with a typed error; the effective backend remains at or above the tier floor.

### Dev-machine / platform fallback — *Must*

- **REQ-030** *(Simplicity)* WHEN the host cannot run VM isolation (non-Linux/no KVM, e.g. a macOS dev machine), the router SHALL apply an **explicit, tier-gated** guard: non-federal tiers MAY degrade to `container` with an audited notice; **federal** SHALL fail closed.
  - *Accept:* on macOS, a personal/enterprise agent runs via the container backend with an audit notice; a federal agent on macOS refuses to execute (typed error), so a federal deployment only ever runs the VM on Linux.
- **REQ-031** *(Scalability)* The VM backend SHALL declare its cold-start cost via `capabilities.cold_start_budget_ms` so callers can reason about latency without probing.
  - *Accept:* `VmBackend().capabilities.cold_start_budget_ms` is set to a realistic VM boot budget (distinct from container's 800ms and local's 10ms).

### Concern boundary — *Must*

- **REQ-040** *(Modularity)* arcrun SHALL remain execution-only: the router consumes a tier value passed in by the caller and SHALL NOT source tier itself, call ArcLLM, or contain agent logic.
  - *Accept:* the arcrun boundary test still holds; `make_execute_tool` receives `tier` as a parameter (as it already does) and imports nothing from arcagent/arcllm.

### Caller + audit wiring — *Must*

- **REQ-050** *(Modularity)* arcrun's `make_execute_tool` callers (`arccli` `agent/tools.py`, `commands/run.py`) SHALL pass the agent's configured tier so the tool is built with the correct isolation floor.
  - *Accept:* `arc agent tools --with-code-exec` and the `run` agent path construct `make_execute_tool(..., tier=<agent tier>)`; a federal agent's `execute_python` is VM-backed end-to-end.
- **REQ-060** *(Security)* Backend selection for every `execute_python` build, and any tier-permitted downgrade (personal relax, non-Linux container fallback), SHALL emit an audit event via arctrust.
  - *Accept:* building a tool emits a `code_exec.backend.selected` event with tier + isolation; a downgrade emits a distinct audited notice; events are attributable.

### Cleanup — *Should*

- **REQ-070** *(Simplicity)* The superseded "tier accepted but ignored" semantics (`execute.py:43` comment + `_ = tier`) SHALL be removed, and the dead `arcagent/core/os_sandbox.py` seccomp stub SHALL be reconciled (implemented-elsewhere note or removed) so no un-called `NotImplementedError` sandbox masquerades as coverage.
  - *Accept:* no "reserved for future use / local backend used for all tiers" comment remains; `ruff`/`mypy --strict` clean on touched packages; the os_sandbox dead-code status is resolved in the spec's review notes (no silent stub).

---

## MoSCoW

- **Must:** REQ-001…060 (VM backend, tier routing, fail-closed default, config relaxation, dev fallback, boundary, caller+audit wiring).
- **Should:** REQ-070 (cleanup, inline with the edits that supersede each item).
- **Won't (this spec):** arcagent seccomp `os_sandbox` implementation; `remote` backend; non-Python runtimes.

## Traceability

Every REQ → SDD component → PLAN task; tasks scoped to one module.
