# arcrun-phase-4 — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-174–D-187 (14 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Feature: ArcRun Phase 4 — Hardening

**Date**: 2026-02-21
**Source**: `packages/arcrun/.claude/steering/roadmap.md` (Phase 4)
**Goal**: Container sandbox, event integrity, adversarial testing, concurrent spawn performance, NIST 800-53 documentation

> **Research Enhancement Summary** (2026-02-21): Enriched with 5 parallel research agents covering Docker SDK patterns, SHA-256 hash chain implementation, adversarial testing techniques, NIST 800-53 control mapping, and codebase integration analysis. Key findings: 3 real bugs discovered (mutable Event data, no seccomp on execute, no PID limits), 38 NIST controls mapped (21 fully implemented, 14 partial, 3 planned), complete implementation patterns for container factory and hash chain. Ready for `/specify`.

#### Decisions

| # | Category | Decision | Choice | Rationale |
|---|----------|----------|--------|-----------|

#### Key Design Principles

- **Container sandbox is opt-in**: Existing code unchanged. New factory for container-isolated code execution.
- **Event integrity is always-on**: Hash chain computed on every emit. Verification optional but chain is always built.
- **Adversarial tests are comprehensive**: 8 attack categories covering OWASP LLM + Agentic top 10 threats.
- **NIST documentation is thorough**: Full control mapping for FedRAMP readiness.
- **No network calls from arcrun. Ever.**: Container images pre-staged by operator. No auto-pull.

#### Components to Build

1. **`arcrun/builtins/contained_execute.py`** — Container-isolated code execution factory (~80-100 LOC)
2. **`arcrun/events.py`** — Add hash chain to EventBus (~20-30 LOC)
3. **`arcrun/types.py`** — Add `sequence`, `prev_hash`, `event_hash` to Event; `verify_integrity()` to LoopResult (~20 LOC)
4. **`tests/security/`** — 8 adversarial test files
5. **`docs/security/`** — NIST mapping, threat model, adversarial test catalog

#### Architecture Diagram

```
make_contained_execute_tool(image="python:3.11-slim", ...)
    |
    v
Tool.execute(params, ctx)
    |
    +-- docker.from_env() or docker.DockerClient(base_url=socket)
    +-- client.containers.run(
    |       image=image,
    |       command=["python", "/tmp/script.py"],
    |       network_disabled=True,
    |       read_only=True,
    |       mem_limit="256m",
    |       pids_limit=64,
    |       cap_drop=["ALL"],
    |       tmpfs={"/tmp": "size=64m"},
    |       auto_remove=True,
    |   )
    +-- return {"stdout": ..., "stderr": ..., "exit_code": ...}

EventBus.emit()
    |
    +-- sequence = len(self._events)
    +-- prev_hash = self._events[-1].event_hash if events else "genesis"
    +-- event_hash = sha256(prev_hash + canonical(event_data))
    +-- Event(type, timestamp, run_id, data, sequence, prev_hash, event_hash)
```

#### Research Insights (via /deepen)

**Enriched**: 2026-02-21 | **Sources**: 5 parallel research agents (Docker SDK patterns, SHA-256 hash chain, adversarial testing, NIST 800-53 mapping, codebase integration analysis)

##### D1/D3 — Container Sandbox: Docker SDK Implementation Patterns

**Factory pattern** — `make_contained_execute_tool()` follows the same factory signature as `make_execute_tool()`:

```python
def make_contained_execute_tool(
    *,
    image: str,                          # Required — caller specifies, no auto-pull
    timeout_seconds: float = 30,
    max_output_bytes: int = 65536,
    socket: str | None = None,           # Auto-detect: Docker or Podman
    mem_limit: str = "256m",
    cpu_period: int = 100_000,
    cpu_quota: int = 50_000,             # 50% of one core
    pids_limit: int = 64,
    tmpfs_size: str = "64m",
    network_disabled: bool = True,
    read_only: bool = True,
) -> Tool:
```

**Socket auto-detection** (Docker vs Podman):
```python
def _detect_socket() -> str:
    candidates = [
        os.environ.get("DOCKER_HOST", ""),
        f"unix:///run/user/{os.getuid()}/podman/podman.sock",  # Rootless Podman
        "unix:///var/run/docker.sock",                          # Docker default
        "unix:///var/run/podman/podman.sock",                   # Rootful Podman
    ]
    for sock in candidates:
        if sock and Path(sock.replace("unix://", "")).exists():
            return sock
    raise SandboxUnavailableError("No container runtime socket found")
```

**Code injection via tar** (avoids bind mounts — more secure):
```python
import tarfile, io
def _inject_code_via_tar(container, code: str) -> None:
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        data = code.encode("utf-8")
        info = tarfile.TarInfo(name="script.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive("/tmp", tar_stream)
```

**Seccomp profile** — Restrict syscalls to compute-only operations. Block: `mount`, `umount`, `ptrace`, `keyctl`, `pivot_root`, `reboot`, `kexec_load`, `unshare`, `setns`, `clone` (with CLONE_NEWUSER). Allow: `read`, `write`, `open`, `close`, `mmap`, `brk`, `stat`, `fstat`, `exit_group`, and other standard compute syscalls.

**Error hierarchy**:
- `SandboxUnavailableError` — No runtime found (Docker not installed, socket not accessible)
- `SandboxTimeoutError` — Container exceeded timeout
- `SandboxOOMError` — Container killed by OOM (exit code 137)
- `SandboxRuntimeError` — Script execution failed (non-zero exit)

**Podman compatibility notes**:
- Podman implements Docker's API — `docker-py` works via socket config
- Podman rootless runs as non-root user (preferred for fed/enterprise)
- Podman native SELinux support via `:Z` volume label (automatic in rootless)
- Podman supports FIPS mode when host kernel has FIPS enabled

**Air-gap image management**:
```bash
# Online machine: save approved image
docker save python:3.11-slim -o python-3.11-slim.tar
# Transfer to air-gapped environment
docker load -i python-3.11-slim.tar
# Podman equivalent
podman save/load same syntax
```

**Warm pool pattern** (optional optimization for repeated executions):
```python
# Create container once, reuse for multiple executions
container = client.containers.create(image=image, **constraints)
container.start()
# On shutdown: container.stop() + container.remove()
```
Note: Warm pool trades isolation (shared container) for performance. Default should be ephemeral (new container per execution).

##### D5/D6 — Hash Chain: Production-Ready Implementation

**Event immutability** — Current Event is a mutable `@dataclass`. Must change to `frozen=True` and wrap data dict:

```python
from types import MappingProxyType

@dataclass(frozen=True)
class Event:
    type: str
    timestamp: float
    run_id: str
    data: MappingProxyType               # Immutable view of data dict
    sequence: int = 0
    prev_hash: str = ""
    event_hash: str = ""
```

**MappingProxyType** prevents observer mutation of data dict. Construction:
```python
event = Event(
    type=event_type,
    timestamp=time.time(),
    run_id=self._run_id,
    data=MappingProxyType(dict(data)),    # Deep copy + freeze
    sequence=seq,
    prev_hash=prev,
    event_hash=computed_hash,
)
```

**Canonical bytes** for hash computation — deterministic serialization:
```python
def _canonical_bytes(event_type: str, timestamp: float, run_id: str,
                     data: Mapping, sequence: int) -> bytes:
    payload = json.dumps(
        {"type": event_type, "timestamp": timestamp, "run_id": run_id,
         "data": dict(data), "sequence": sequence},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return payload
```

**Hash computation**:
```python
GENESIS_PREV_HASH = "0" * 64  # 64 hex chars = 256 bits

def _compute_event_hash(prev_hash: str, canonical: bytes) -> str:
    return hashlib.sha256(
        prev_hash.encode("ascii") + canonical
    ).hexdigest()
```

**Thread safety** — EventBus.emit() must be thread-safe for observer callbacks:
```python
import threading

class EventBus:
    def __init__(self, run_id: str, ...):
        self._lock = threading.Lock()
        self._events: list[Event] = []

    def emit(self, event_type: str, data: dict | None = None) -> Event:
        with self._lock:
            seq = len(self._events)
            prev = self._events[-1].event_hash if self._events else GENESIS_PREV_HASH
            canonical = _canonical_bytes(event_type, time.time(), self._run_id,
                                        data or {}, seq)
            event_hash = _compute_event_hash(prev, canonical)
            event = Event(type=event_type, timestamp=..., run_id=self._run_id,
                         data=MappingProxyType(dict(data or {})),
                         sequence=seq, prev_hash=prev, event_hash=event_hash)
            self._events.append(event)
        # Observer callback OUTSIDE lock to prevent deadlock
        if self._on_event is not None:
            try: self._on_event(event)
            except Exception: pass
        return event
```

**Chain verification** — Three invariants:
```python
@dataclass
class ChainVerificationResult:
    valid: bool
    event_count: int
    first_broken_index: int | None = None
    error: str | None = None

def verify_chain(events: list[Event]) -> ChainVerificationResult:
    for i, event in enumerate(events):
        # 1. Self-hash: recompute and compare
        canonical = _canonical_bytes(event.type, event.timestamp, event.run_id,
                                    event.data, event.sequence)
        expected = _compute_event_hash(event.prev_hash, canonical)
        if expected != event.event_hash:
            return ChainVerificationResult(False, len(events), i, "self-hash mismatch")

        # 2. Chain linkage: prev_hash matches previous event's hash
        expected_prev = events[i-1].event_hash if i > 0 else GENESIS_PREV_HASH
        if event.prev_hash != expected_prev:
            return ChainVerificationResult(False, len(events), i, "chain break")

        # 3. Sequence order: must be monotonically increasing
        if event.sequence != i:
            return ChainVerificationResult(False, len(events), i, "sequence gap")

    return ChainVerificationResult(True, len(events))
```

**LoopResult.verify_integrity()** — Thin wrapper:
```python
def verify_integrity(self) -> ChainVerificationResult:
    return verify_chain(self.events)
```

**NIST compliance mapping**: AU-9 (Protection of Audit Information) — hash chain detects tampering. AU-10 (Non-repudiation) — event hashes prove chronological ordering.

##### D7/D8 — Adversarial Testing: 8 Attack Categories with Findings

**Real bugs discovered during research**:

1. **EventBus.emit() doesn't copy data dict** — Observer callbacks receive the same dict object. A malicious observer could mutate data, affecting subsequent observers and corrupting the event record. **Fix**: `MappingProxyType(dict(data))` in hash chain implementation resolves this.

2. **execute.py has no seccomp/chroot** — Current `make_execute_tool()` runs model code as a subprocess on the host with full filesystem access. **Fix**: `make_contained_execute_tool()` resolves this for opt-in users.

3. **No OS-level PID limits for fork bomb protection** — Current subprocess execution has no PID limits. A malicious `os.fork()` loop in model-generated code could exhaust host PIDs. **Fix**: Container `pids_limit=64` resolves this for contained execution.

**8 test categories with example payloads**:

| Category | File | Key Test Cases |
|----------|------|----------------|
| Prompt injection | `test_prompt_injection.py` | System prompt extraction, tool call injection via task text, instruction override in spawned child |
| Path traversal | `test_path_traversal.py` | `../../etc/passwd` in code execution, symlink escape from tmpdir, container mount escape |
| Resource exhaustion | `test_resource_exhaustion.py` | Fork bomb (`while True: os.fork()`), memory bomb (`"A" * 10**10`), infinite loop, disk fill via /tmp |
| Event chain tampering | `test_event_tampering.py` | Modify event data after emit, insert/delete events, reorder events, replay events from different run |
| Tool parameter injection | `test_tool_injection.py` | SQL injection via tool params, command injection in execute args, oversized parameters |
| Spawn depth bomb | `test_spawn_depth_bomb.py` | Recursive spawn to max_depth, parallel spawn flood, spawn with manipulated depth field |
| Steering injection | `test_steering_injection.py` | Inject instructions via tool result, manipulate system prompt through child spawn, context poisoning |
| Timing attacks | `test_timing_attacks.py` | Concurrent spawn race conditions, emit during verification, spawn+cancel race, spawn+steer race |

**OWASP coverage matrix**:

| OWASP ID | Threat | Test Category |
|----------|--------|---------------|
| LLM01 | Prompt Injection | prompt_injection, steering_injection |
| ASI02 | Tool Misuse | tool_injection, path_traversal |
| ASI05 | Unexpected Code Execution | resource_exhaustion, path_traversal |
| AU-9 | Audit Tampering | event_tampering |
| ASI08 | Cascading Failures | spawn_depth_bomb, timing_attacks |

##### D9 — Concurrent Spawn Stress Tests

**Key stress test scenarios**:

1. **10+ parallel spawns** — `asyncio.gather(*[run(...) for _ in range(10)])` with mock models that have controlled delays. Verify: no event interleaving between runs, all run_ids unique, all events have correct parent_run_id.

2. **Nested parallel spawns** — Parent spawns 3 children, each child spawns 2 grandchildren. Verify: event bubbling correct at all levels, `child.{run_id}.` prefix nesting is accurate.

3. **Spawn + cancel race** — Start a spawn, then cancel the parent mid-execution. Verify: child tasks are properly cancelled, no zombie asyncio tasks, events up to cancellation point are valid.

4. **Spawn + steer race** — Concurrent `run()` calls where one changes strategy mid-loop. Verify: strategy changes don't affect sibling runs.

**Test pattern** (from existing test suite): MockModel with predetermined responses:
```python
class MockModel:
    def __init__(self, responses: list[LLMResponse]):
        self._responses = iter(responses)
    async def invoke(self, messages, tools=None, **kwargs):
        return next(self._responses)
```

##### D10/D11 — NIST 800-53: Control Mapping Summary

**38 controls mapped across 8 families**:

| Family | Controls | Fully Impl. | Partial | Planned |
|--------|----------|-------------|---------|---------|
| AC (Access Control) | AC-3, AC-4, AC-6, AC-17, AC-25 | 3 | 1 | 1 |
| AU (Audit) | AU-2, AU-3, AU-6, AU-8, AU-9, AU-10, AU-12 | 4 | 2 | 1 |
| CM (Config Mgmt) | CM-2, CM-3, CM-5, CM-7, CM-8 | 3 | 2 | 0 |
| IA (Identification) | IA-2, IA-3, IA-4, IA-5 | 2 | 2 | 0 |
| SC (System/Comms) | SC-2, SC-3, SC-4, SC-7, SC-8, SC-13, SC-28, SC-39 | 4 | 3 | 1 |
| SI (System Integrity) | SI-2, SI-3, SI-4, SI-7, SI-10 | 3 | 2 | 0 |
| SA (System Acq.) | SA-4, SA-8, SA-10, SA-11 | 1 | 3 | 0 |
| CA (Assessment) | CA-2, CA-7, CA-8 | 1 | 1 | 1 |
| **Total** | **38** | **21** | **14** | **3** |

**Key controls enabled by Phase 4**:

| Control | Title | Phase 4 Feature |
|---------|-------|-----------------|
| AU-9 | Protection of Audit Information | SHA-256 hash chain (tamper-evident events) |
| AU-10 | Non-repudiation | Hash chain proves chronological ordering |
| SC-4 | Information in Shared Resources | Container isolation prevents data leakage between executions |
| SC-7 | Boundary Protection | Container network disabled, read-only FS |
| SC-39 | Process Isolation | Container per-execution with PID/mem/CPU limits |
| AC-25 | Reference Monitor | Sandbox check in executor.py (TOCTOU-safe in asyncio) |
| SA-8 | Security Engineering Principles | Least privilege defaults, fail-secure |
| CA-8 | Penetration Testing | 8-category adversarial test suite |

**Priority gaps for ATO**:
- AU-6 (Audit Review): Need automated analysis/alerting on events (partially addressed by observer pattern)
- SA-11 (Developer Testing): Need formal test plan document cross-referencing NIST controls
- CA-2 (Control Assessments): Need periodic assessment procedure documentation

##### Codebase Integration: Critical Implementation Details

**Event dataclass migration** (`events.py`):
- Current Event is `@dataclass` (mutable). Changing to `frozen=True` is a **breaking change** for any code that mutates events after creation.
- EventBus.emit() returns Event — callers may be storing references and mutating `.data`. Search for: `event.data["key"] = value` patterns.
- `data: dict[str, Any]` → `data: MappingProxyType` changes type signature. Callers doing `isinstance(event.data, dict)` will break. `MappingProxyType` supports `Mapping` protocol but not `MutableMapping`.

**EventBus thread safety** (`events.py`):
- Current `self._events.append(event)` is NOT thread-safe if observers run in threads.
- `threading.Lock` around emit body prevents concurrent sequence number conflicts.
- Observer callback MUST run outside the lock to prevent deadlock if observer calls emit.

**LoopResult.events type** (`types.py`):
- Currently `events: list[Any]`. Should become `events: list[Event]` for type safety.
- `_build_result()` in `react.py:188-204` constructs LoopResult. Events come from `state.event_bus.events` (shallow copy via `list()`). Hash chain integrity is preserved because Event is now frozen.

**Public API exports** (`__init__.py`):
- Currently 12 exports. Add: `make_contained_execute_tool`, `verify_chain`, `ChainVerificationResult`, `GENESIS_PREV_HASH`.
- `SandboxUnavailableError`, `SandboxTimeoutError`, `SandboxOOMError`, `SandboxRuntimeError` for error handling.

**Optional dependency** (`pyproject.toml`):
```toml
[project.optional-dependencies]
container = ["docker>=7.0"]
```
No optional deps section currently exists — must create it.

**Lazy import pattern** for docker:
```python
def make_contained_execute_tool(...) -> Tool:
    try:
        import docker
    except ImportError:
        raise ImportError(
            "Container support requires docker SDK. "
            "Install with: pip install arcrun[container]"
        )
```

#### New Risks Discovered

1. **Event mutation by observers** — Current EventBus passes raw mutable data dict to observers. A malicious or buggy observer could corrupt the event record. Fixed by `MappingProxyType` + `frozen=True`.

2. **No thread safety on EventBus** — `list.append()` is thread-safe in CPython due to GIL, but sequence number computation (`len(self._events)`) and hash chain linkage are not atomic. Fixed by `threading.Lock`.

3. **Type signature change** — `Event.data: dict → MappingProxyType` is a breaking change. Must audit all callers.

4. **Container runtime availability** — `make_contained_execute_tool()` silently fails if Docker/Podman not installed. Must provide clear error with install instructions.

5. **Seccomp profile portability** — Custom seccomp profiles may not work on all kernel versions. Need fallback to default Docker seccomp profile.

---

---
