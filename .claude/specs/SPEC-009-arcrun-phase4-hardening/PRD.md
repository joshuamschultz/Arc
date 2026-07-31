# PRD: ArcRun Phase 4 — Hardening

**Spec ID**: SPEC-009
**Date**: 2026-02-21
**Source**: `packages/arcrun/.claude/steering/roadmap.md` Phase 4 + 14 build decisions + 5 research agents

## 1. Problem Statement

ArcRun currently executes model-generated code via `make_execute_tool()` as a subprocess on the host machine with no container isolation, no PID limits, and full filesystem access. Events are mutable and unverified — an attacker or buggy observer could tamper with the audit trail. There is no adversarial test suite and no NIST 800-53 compliance documentation. These gaps block FedRAMP authorization for federal deployment.

## 2. Goals

1. **Container sandbox** — Isolate model-generated code execution in ephemeral containers with maximum lockdown defaults
2. **Event integrity** — Tamper-evident audit trail via SHA-256 hash chain on all events
3. **Adversarial testing** — 8-category security test suite covering OWASP LLM + Agentic top 10
4. **Concurrent spawn resilience** — Verify no deadlocks or race conditions under parallel load
5. **NIST documentation** — Full 800-53 control mapping for FedRAMP readiness

## 3. Non-Goals

- Replacing `make_execute_tool()` — existing tool unchanged, container is opt-in
- Network calls from arcrun — arcrun never pulls images or makes network requests
- Production monitoring/alerting infrastructure
- Container orchestration (Kubernetes, Swarm)
- Event authentication (digital signatures) — integrity only, not authentication

## 4. Requirements

### 4.1 Container Sandbox (D1-D4, D12, D14)

| ID | Requirement | Priority |
|----|-------------|----------|
| R1 | New `make_contained_execute_tool()` factory function | P0 |
| R2 | Docker SDK (`docker-py>=7.0`) as optional dependency | P0 |
| R3 | Socket auto-detection: Docker, rootless Podman, rootful Podman | P0 |
| R4 | Caller-specified image (required param), no auto-pull | P0 |
| R5 | Maximum lockdown defaults: no network, read-only root FS, 256MB memory, 50% CPU, drop ALL caps, no-new-privileges, 64 PID limit, tmpfs /tmp (64MB) | P0 |
| R6 | All constraints configurable (caller can relax) | P1 |
| R7 | Code injection via tar (no bind mounts) | P0 |
| R8 | Error hierarchy: `SandboxUnavailableError`, `SandboxTimeoutError`, `SandboxOOMError`, `SandboxRuntimeError` | P0 |
| R9 | Lazy import with helpful error if docker not installed | P0 |
| R10 | Return `{stdout, stderr, exit_code, duration_ms}` matching existing execute tool | P0 |

### 4.2 Event Integrity (D5-D6)

| ID | Requirement | Priority |
|----|-------------|----------|
| R11 | SHA-256 hash chain: each event has `sequence`, `prev_hash`, `event_hash` | P0 |
| R12 | Genesis hash: `"0" * 64` for first event's `prev_hash` | P0 |
| R13 | Canonical bytes: `json.dumps(sort_keys=True, separators=(",", ":"))` | P0 |
| R14 | Event dataclass becomes `frozen=True` | P0 |
| R15 | `Event.data` becomes `MappingProxyType` (immutable view) | P0 |
| R16 | `EventBus.emit()` thread-safe via `threading.Lock` | P0 |
| R17 | Observer callback runs outside lock (prevent deadlock) | P0 |
| R18 | `verify_chain(events)` returns `ChainVerificationResult` | P0 |
| R19 | Three verification invariants: self-hash, chain linkage, sequence order | P0 |
| R20 | `LoopResult.verify_integrity()` method (thin wrapper) | P0 |

### 4.3 Adversarial Testing (D7-D9)

| ID | Requirement | Priority |
|----|-------------|----------|
| R21 | `tests/security/` directory with 8 test files | P0 |
| R22 | Prompt injection tests | P0 |
| R23 | Path traversal tests | P0 |
| R24 | Resource exhaustion tests (fork bomb, memory bomb, infinite loop, disk fill) | P0 |
| R25 | Event chain tampering tests | P0 |
| R26 | Tool parameter injection tests | P0 |
| R27 | Spawn depth bomb tests | P0 |
| R28 | Steering injection tests | P0 |
| R29 | Timing/concurrency tests (10+ parallel spawns, nested parallel, spawn+cancel, spawn+steer) | P0 |
| R30 | All tests runnable independently: `pytest tests/security/` | P1 |

### 4.4 NIST Documentation (D10-D11)

| ID | Requirement | Priority |
|----|-------------|----------|
| R31 | `docs/security/nist-800-53-mapping.md` — all 38+ applicable controls | P0 |
| R32 | `docs/security/threat-model.md` — OWASP LLM + Agentic threats | P1 |
| R33 | `docs/security/adversarial-tests.md` — test catalog with coverage matrix | P1 |
| R34 | Each control entry: ID, title, arcrun feature, code reference, test evidence | P0 |

### 4.5 Integration

| ID | Requirement | Priority |
|----|-------------|----------|
| R35 | `__init__.py` exports: `make_contained_execute_tool`, `verify_chain`, `ChainVerificationResult`, `GENESIS_PREV_HASH` | P0 |
| R36 | Error types exported: `SandboxUnavailableError`, `SandboxTimeoutError`, `SandboxOOMError`, `SandboxRuntimeError` | P0 |
| R37 | `LoopResult.events: list[Event]` (type upgrade from `list[Any]`) | P1 |
| R38 | `pyproject.toml` optional dependency: `[container] = ["docker>=7.0"]` | P0 |
| R39 | Total LOC under 1,400 | P0 |

## 5. Phase 4 Gate Criteria

From roadmap (with D13 revision):

- [ ] Container sandbox available as opt-in option
- [ ] Event checksums prevent tampering (verify_chain passes)
- [ ] Adversarial tests pass (prompt injection, path traversal, resource exhaustion, etc.)
- [ ] Concurrent spawns don't deadlock (10+ parallel, nested parallel)
- [ ] NIST 800-53 control mapping documented (38+ controls)
- [ ] Total LOC under 1,400

## 6. Known Bugs to Fix

From research:

1. **EventBus.emit() doesn't copy data dict** — Observer mutation corrupts event record. Fix: `MappingProxyType(dict(data))` (covered by R15)
2. **execute.py has no seccomp/chroot** — Model code runs with full host access. Fix: `make_contained_execute_tool()` provides opt-in isolation (covered by R1)
3. **No OS-level PID limits** — Fork bomb protection missing. Fix: Container `pids_limit=64` (covered by R5)

## 7. OWASP Coverage Matrix

| OWASP ID | Threat | Test Category | Requirement |
|----------|--------|---------------|-------------|
| LLM01 | Prompt Injection | prompt_injection, steering_injection | R22, R28 |
| ASI02 | Tool Misuse | tool_injection, path_traversal | R23, R26 |
| ASI05 | Unexpected Code Execution | resource_exhaustion, path_traversal | R23, R24 |
| AU-9 | Audit Tampering | event_tampering | R25 |
| ASI08 | Cascading Failures | spawn_depth_bomb, timing_attacks | R27, R29 |

## 8. Success Metrics

| Metric | Target |
|--------|--------|
| Container execution isolated from host | All capabilities dropped, no network, read-only FS |
| Hash chain verification | Detects single-bit modification in any event |
| Adversarial test count | 40+ test cases across 8 categories |
| Concurrent spawn stability | 10+ parallel spawns, zero deadlocks |
| NIST controls mapped | 38+ across 8 families |
| Total LOC | < 1,400 |
| Test coverage | >= 80% |
