# SPEC-036 — Real Code-Execution Sandbox

**Feature:** Make sandboxed code execution the **default and tier-enforced** in arcrun, and add a real `isolation="vm"` backend — closing the ASI05 "Firecracker microVM isolation" gap that CLAUDE.md claims but the code does not deliver.
**Status:** PENDING
**Branch:** `feat/SPEC-036-code-exec-sandbox`
**Type:** Generic (execution backend + tier routing + config + dev-machine fallback)
**Confidence:** High — problem confirmed at file:line; the container path already exists and is reused; the backend Protocol already declares `"vm"`.

---

## One-liner

Route `execute_python` **by tier** to an isolation backend behind arcrun's existing `ExecutorBackend` Protocol — **federal → VM (hard), enterprise → container, personal → container (relaxable only by explicit config)** — add a real `isolation="vm"` backend (Firecracker on Linux/KVM; gVisor/`runsc` documented as the alternative), and **fail closed** when the required isolation is unavailable. No more bare host subprocess by default.

## Why (the problem)

Arc's default code execution is an **unsandboxed host subprocess**, and the security posture claimed in CLAUDE.md (ASI05: *"Firecracker microVM isolation"*) is absent. Verified against code:

- **`execute_python` runs `isolation="none"`.** `make_execute_tool` (`packages/arcrun/src/arcrun/builtins/execute.py:25-63`) spawns `sys.executable` directly via `asyncio.create_subprocess_exec` on the host. The `tier` parameter is **accepted but ignored** — `execute.py:43` literally does `_ = tier` with a "reserved for future use" comment. This is the tool arcrun hands agents (`arccli/.../commands/agent/tools.py:18-20`, `commands/run.py:122,259` — all call `make_execute_tool()` with no tier).
- **The seccomp sandbox is a stub with zero call sites.** `packages/arcagent/src/arcagent/core/os_sandbox.py` — `SeccompSandbox.run` raises `NotImplementedError` (`os_sandbox.py:180-190`); `make_sandbox` exists but nothing in the codebase calls it (grep: zero non-test references).
- **The Protocol already promises VM, but no VM backend exists.** `BackendCapabilities.isolation` is a `Literal["none", "container", "vm", "remote"]` (`backends/base.py:70`). Built-ins resolve only `local` and `docker` (`backends/loader.py:183-193`); there is no `vm` backend to load.
- **A genuine container path already exists — but it's opt-in.** `make_contained_execute_tool` (`builtins/contained_execute.py:89-201`) is container-grade (`cap_drop=["ALL"]`, `no-new-privileges`, `network_disabled`, `read_only`, non-root `65534:65534`, pid/mem/cpu limits). `DockerBackend` (`backends/docker.py:51-89`) is the same posture as a Protocol-native backend (`isolation="container"`). Neither is the default; agents get the bare subprocess.

Net: the strongest isolation Arc ships is never on by default, and "VM isolation" is a claim with no implementation. This spec makes isolation the default, enforces it by tier, and supplies the missing VM backend.

## Decision (approved with Josh)

**`execute_python` becomes tier-routed over the `ExecutorBackend` Protocol; the VM backend is a new pluggable backend behind that same Protocol.**

- **Tier is the router, not a hint.** `make_execute_tool` selects a backend by tier instead of ignoring it: **federal → VM (hard floor), enterprise → container, personal → container default**. Local host subprocess is reachable **only** at personal tier and **only** via explicit config relaxation.
- **Fail-closed.** At enterprise/federal, if the required isolation backend is unavailable (no KVM, no container runtime, wrong platform), execution **refuses** — it never silently degrades to a weaker isolation.
- **Reuse, don't rebuild.** The container backend already exists (`DockerBackend` / `contained_execute` hardening). The VM backend is small and sits behind the existing Protocol next to `LocalBackend`/`DockerBackend`. arcrun stays execution-only.

Rationale: one routing decision keyed on tier is simpler than scattered isolation flags (Simplicity); a new backend behind the Protocol keeps VM isolation pluggable and swappable for gVisor (Modularity); default-deny-weak-isolation is the ASI05 mitigation the codebase claims (Security); backends are shared-nothing per execution and cold-start-budgeted in `capabilities` (Scalability).

## Scope (this spec)

1. **VM backend** — a real `isolation="vm"` `ExecutorBackend` (Firecracker/KVM on Linux; gVisor/`runsc` documented as the drop-in alternative), loadable as a built-in.
2. **Tier routing** — `make_execute_tool` routes by tier to VM / container / local; the ignored `tier` param becomes authoritative.
3. **Fail-closed default** — enterprise/federal never fall back to bare subprocess; unavailable required isolation → refuse.
4. **Config relaxation** — personal tier MAY opt down to container/local, only via explicit config, always audited.
5. **Dev-machine story** — non-Linux (macOS) has no KVM; explicit, tier-gated container fallback so dev degrades loudly while Linux federal deployments get the VM.
6. **Caller + audit wiring** — CLI callers pass the agent's tier; backend selection and any downgrade emit an audit event.

**Out of scope:** the arcagent `os_sandbox.py` seccomp implementation (a separate arcagent concern; this spec routes arcrun execution and notes the stub as dead code to reconcile); `remote` backend; language runtimes other than Python.

## Principled-coder pillars

1. **Simplicity** — one tier→backend routing function; reuse the existing container path; the VM backend is very-few-LOC behind the Protocol.
2. **Modularity** — VM isolation is a new `ExecutorBackend` implementation, swappable (Firecracker ↔ gVisor) with no change to `execute.py`; arcrun stays execution-only (no LLM/agent logic).
3. **Security** — sandboxed by default, tier-enforced, **fail-closed** on unavailable isolation; downgrades only via explicit config, always audited. Direct ASI05 mitigation.
4. **Scalability** — shared-nothing per execution; `capabilities.cold_start_budget_ms` makes VM cold-start explicit; timeouts/limits on every backend.

## Key facts (from investigation)

- `make_execute_tool` ignores tier and runs bare subprocess: `execute.py:43` (`_ = tier`), `:55-63` (`create_subprocess_exec` of `sys.executable`, `isolation="none"`).
- Protocol already declares VM: `backends/base.py:70` (`isolation: Literal["none","container","vm","remote"]`).
- Container path exists twice: Protocol-native `DockerBackend` (`backends/docker.py:51-89`, `isolation="container"`, cap-drop/no-new-priv/read-only/network none) and the opt-in tool `make_contained_execute_tool` (`builtins/contained_execute.py:89-201`, non-root `65534`, pid/mem/cpu limits).
- Loader is tier-aware but has no VM built-in: `backends/loader.py:75-193`, `_try_builtin` resolves only `local`/`docker` (`:183-193`).
- Seccomp sandbox is a stub with no callers: `arcagent/core/os_sandbox.py:180-190` (`NotImplementedError`); grep finds zero non-test call sites of `make_sandbox`.
- Tier vocabulary already exists: `arcagent/core/tier.py` (`Tier` StrEnum: `FEDERAL`/`ENTERPRISE`/`PERSONAL`).
- Callers pass no tier today: `arccli/.../commands/agent/tools.py:18-20`, `commands/run.py:122,259`.

## Files

- `PRD.md` — requirements (EARS, pillar-tagged, MoSCoW)
- `SDD.md` — module boundaries + component design (VM backend, tier router, fail-closed guard, dev fallback) grounded in file:line
- `PLAN.md` — phased TDD tasks, one module per task

## Learnings

_(captured during /deepen, /implement, /review)_
