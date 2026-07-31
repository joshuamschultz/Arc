# SDD: ArcRun Phase 4 — Hardening

**Spec ID**: SPEC-009
**Date**: 2026-02-21

## 1. Component Overview

Phase 4 adds 3 production components and 3 documentation deliverables:

| Component | File | Estimated LOC | Type |
|-----------|------|---------------|------|
| Container Execute | `src/arcrun/builtins/contained_execute.py` | ~80-100 | New file |
| Event Integrity | `src/arcrun/events.py` | ~30-40 net new | Modification |
| Chain Verification | `src/arcrun/types.py` | ~20-25 net new | Modification |
| Adversarial Tests | `tests/security/*.py` (8 files) | ~400-500 | New directory |
| NIST Mapping | `docs/security/nist-800-53-mapping.md` | N/A (docs) | New file |
| Threat Model | `docs/security/threat-model.md` | N/A (docs) | New file |
| Test Catalog | `docs/security/adversarial-tests.md` | N/A (docs) | New file |

**Net production LOC increase**: ~130-165 (within 1,400 budget)

## 2. Container Sandbox (`builtins/contained_execute.py`)

### 2.1 Factory Function

```python
def make_contained_execute_tool(
    *,
    image: str,                          # Required — no auto-pull
    timeout_seconds: float = 30,
    max_output_bytes: int = 65536,
    socket: str | None = None,           # Auto-detect if None
    mem_limit: str = "256m",
    cpu_period: int = 100_000,
    cpu_quota: int = 50_000,             # 50% of one core
    pids_limit: int = 64,
    tmpfs_size: str = "64m",
    network_disabled: bool = True,
    read_only: bool = True,
) -> Tool:
```

Follows the same factory pattern as `make_execute_tool()`. Returns a `Tool` with the same `{stdout, stderr, exit_code, duration_ms}` output format.

### 2.2 Socket Auto-Detection

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

Priority: `DOCKER_HOST` env > rootless Podman > Docker > rootful Podman.

### 2.3 Code Injection via Tar

No bind mounts. Code injected via tar stream into container's `/tmp`:

```python
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

### 2.4 Container Constraints

Default configuration (maximum lockdown):

```python
container_config = {
    "image": image,
    "command": ["python", "/tmp/script.py"],
    "network_disabled": True,
    "read_only": True,
    "mem_limit": "256m",
    "pids_limit": 64,
    "cpu_period": 100_000,
    "cpu_quota": 50_000,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges"],
    "tmpfs": {"/tmp": f"size={tmpfs_size}"},
    "auto_remove": True,
}
```

### 2.5 Execution Flow

```
make_contained_execute_tool(image="python:3.11-slim")
    |
    v
Tool.execute(params, ctx)
    |
    +-- Lazy import docker SDK
    +-- docker.DockerClient(base_url=socket or _detect_socket())
    +-- client.containers.create(**container_config)
    +-- container.start()
    +-- _inject_code_via_tar(container, params["code"])
    +-- container.exec_run(["python", "/tmp/script.py"])
    +-- Wait with timeout
    +-- Collect stdout/stderr
    +-- container.stop() + container.remove()
    +-- Return {stdout, stderr, exit_code, duration_ms}
```

**Alternative flow** (simpler, using `containers.run()`):

```
client.containers.run(
    image=image,
    command=["python", "-c", code],  # For short code
    **container_config,
)
```

Preferred approach: `containers.create()` + tar injection for security (avoids shell interpretation of code).

### 2.6 Error Hierarchy

All errors in `builtins/contained_execute.py`:

```python
class SandboxError(Exception):
    """Base for all sandbox errors."""

class SandboxUnavailableError(SandboxError):
    """No container runtime found (Docker not installed, socket not accessible)."""

class SandboxTimeoutError(SandboxError):
    """Container exceeded timeout."""

class SandboxOOMError(SandboxError):
    """Container killed by OOM (exit code 137)."""

class SandboxRuntimeError(SandboxError):
    """Script execution failed (non-zero exit)."""
```

### 2.7 Lazy Import Pattern

```python
def make_contained_execute_tool(...) -> Tool:
    try:
        import docker
    except ImportError:
        raise ImportError(
            "Container support requires docker SDK. "
            "Install with: pip install arcrun[container]"
        )
    # ... rest of factory
```

## 3. Event Integrity (`events.py` modifications)

### 3.1 Event Immutability

Current → New:

```python
# Current (mutable)
@dataclass
class Event:
    type: str
    timestamp: float
    run_id: str
    data: dict[str, Any]

# New (frozen + hash chain)
@dataclass(frozen=True)
class Event:
    type: str
    timestamp: float
    run_id: str
    data: MappingProxyType                # Immutable view
    sequence: int = 0
    prev_hash: str = ""
    event_hash: str = ""
```

### 3.2 Hash Chain Computation

```python
import hashlib
import json
from types import MappingProxyType

GENESIS_PREV_HASH = "0" * 64

def _canonical_bytes(event_type: str, timestamp: float, run_id: str,
                     data: Mapping, sequence: int) -> bytes:
    return json.dumps(
        {"type": event_type, "timestamp": timestamp, "run_id": run_id,
         "data": dict(data), "sequence": sequence},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")

def _compute_event_hash(prev_hash: str, canonical: bytes) -> str:
    return hashlib.sha256(
        prev_hash.encode("ascii") + canonical
    ).hexdigest()
```

### 3.3 Thread-Safe EventBus.emit()

```python
import threading

class EventBus:
    def __init__(self, run_id: str, on_event=None):
        self._run_id = run_id
        self._on_event = on_event
        self._events: list[Event] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        with self._lock:
            seq = len(self._events)
            prev = self._events[-1].event_hash if self._events else GENESIS_PREV_HASH
            ts = time.time()
            frozen_data = MappingProxyType(dict(data or {}))
            canonical = _canonical_bytes(event_type, ts, self._run_id, frozen_data, seq)
            event_hash = _compute_event_hash(prev, canonical)
            event = Event(
                type=event_type, timestamp=ts, run_id=self._run_id,
                data=frozen_data, sequence=seq,
                prev_hash=prev, event_hash=event_hash,
            )
            self._events.append(event)
        # Observer callback OUTSIDE lock
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass
        return event
```

### 3.4 Chain Verification

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
        canonical = _canonical_bytes(
            event.type, event.timestamp, event.run_id, event.data, event.sequence
        )
        expected = _compute_event_hash(event.prev_hash, canonical)
        if expected != event.event_hash:
            return ChainVerificationResult(False, len(events), i, "self-hash mismatch")

        # 2. Chain linkage
        expected_prev = events[i - 1].event_hash if i > 0 else GENESIS_PREV_HASH
        if event.prev_hash != expected_prev:
            return ChainVerificationResult(False, len(events), i, "chain break")

        # 3. Sequence order
        if event.sequence != i:
            return ChainVerificationResult(False, len(events), i, "sequence gap")

    return ChainVerificationResult(True, len(events))
```

Location: `verify_chain()` and `ChainVerificationResult` live in `events.py` (co-located with Event and EventBus). Exported via `__init__.py`.

## 4. LoopResult Integration (`types.py` modifications)

### 4.1 verify_integrity() Method

```python
@dataclass
class LoopResult:
    content: str | None
    turns: int
    tool_calls_made: int
    tokens_used: dict[str, Any]
    strategy_used: str
    cost_usd: float
    events: list[Event] = field(default_factory=list)  # Type upgrade: Any -> Event

    def verify_integrity(self) -> ChainVerificationResult:
        """Verify tamper-evidence of the event chain."""
        from arcrun.events import verify_chain
        return verify_chain(self.events)
```

### 4.2 Breaking Change: `events: list[Any]` -> `events: list[Event]`

The type annotation change from `list[Any]` to `list[Event]` is backward compatible at runtime (Python doesn't enforce generic types). It improves type checking for callers using mypy.

## 5. Public API Changes (`__init__.py`)

### 5.1 New Exports

```python
from arcrun.builtins import make_contained_execute_tool, make_execute_tool, make_spawn_tool
from arcrun.events import (
    GENESIS_PREV_HASH,
    ChainVerificationResult,
    Event,
    EventBus,
    verify_chain,
)
from arcrun.builtins.contained_execute import (
    SandboxError,
    SandboxOOMError,
    SandboxRuntimeError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
```

### 5.2 pyproject.toml

```toml
[project.optional-dependencies]
container = ["docker>=7.0"]
```

## 6. Adversarial Test Design (`tests/security/`)

### 6.1 Directory Structure

```
tests/security/
├── __init__.py
├── conftest.py                    # Shared fixtures (MockModel, event helpers)
├── test_prompt_injection.py       # LLM01
├── test_path_traversal.py         # ASI02, ASI05
├── test_resource_exhaustion.py    # ASI05
├── test_event_tampering.py        # AU-9
├── test_tool_injection.py         # ASI02
├── test_spawn_depth_bomb.py       # ASI08
├── test_steering_injection.py     # LLM01
└── test_timing_attacks.py         # ASI08 + concurrent spawns
```

### 6.2 Test Approach

All tests use `MockModel` — no real LLM calls. Tests verify that arcrun's sandbox, event system, and spawn controls correctly prevent or detect each attack category.

**Key test patterns**:
- **Prompt injection**: Craft task text containing tool call instructions. Verify sandbox blocks unauthorized tool calls.
- **Path traversal**: Use `../../etc/passwd` in code execution params. Verify sandbox denies and container isolation prevents access.
- **Resource exhaustion**: Submit fork bomb, memory bomb, infinite loop. Verify container kills at limits.
- **Event tampering**: Modify event after emit, insert/delete/reorder events. Verify `verify_chain()` detects all mutations.
- **Tool injection**: Submit oversized params, SQL injection strings, command injection. Verify parameter validation and sandbox.
- **Spawn depth bomb**: Recursive spawn to max_depth. Verify depth limit enforcement.
- **Steering injection**: Inject instructions via tool result content. Verify system prompt isolation.
- **Timing attacks**: 10+ parallel spawns via `asyncio.gather`. Verify unique run_ids, no event interleaving, no deadlocks.

## 7. NIST Documentation (`docs/security/`)

### 7.1 `nist-800-53-mapping.md`

38 controls across 8 families. Each entry:

```markdown
### SC-39: Process Isolation
**Status**: Fully Implemented (Phase 4)
**Feature**: Container-per-execution via `make_contained_execute_tool()`
**Code Reference**: `src/arcrun/builtins/contained_execute.py`
**Test Evidence**: `tests/security/test_resource_exhaustion.py`
**Details**: Each code execution runs in an ephemeral container with PID namespace isolation (pids_limit=64), memory limits (256MB), CPU quota (50%), dropped capabilities, and no network access.
```

### 7.2 `threat-model.md`

OWASP LLM Top 10 (2025) + Agentic Top 10 (2026) mapped to arcrun mitigations.

### 7.3 `adversarial-tests.md`

Test catalog with OWASP coverage matrix, example payloads, and expected behaviors.

## 8. Data Flow

### 8.1 Container Execute Flow

```
Caller
    |
    v
make_contained_execute_tool(image="python:3.11-slim")
    |
    v
Tool(name="contained_execute_python", execute=_execute)
    |
    v
Strategy calls tool.execute(params={"code": "..."}, ctx)
    |
    +-- docker.DockerClient(base_url=detected_socket)
    +-- container = client.containers.create(image, **lockdown_config)
    +-- container.start()
    +-- _inject_code_via_tar(container, code)
    +-- result = container.exec_run(["python", "/tmp/script.py"])
    +-- Wait with timeout -> SandboxTimeoutError
    +-- Check exit code 137 -> SandboxOOMError
    +-- container.stop() + container.remove()
    +-- Return JSON {stdout, stderr, exit_code, duration_ms}
```

### 8.2 Event Integrity Flow

```
EventBus.emit("tool.call", {"name": "execute_python"})
    |
    +-- Lock acquired
    +-- seq = len(events)
    +-- prev_hash = events[-1].event_hash or GENESIS
    +-- canonical = json.dumps(sorted, compact)
    +-- event_hash = sha256(prev_hash + canonical)
    +-- Event(frozen=True, data=MappingProxyType)
    +-- events.append(event)
    +-- Lock released
    +-- Observer callback (outside lock)
    |
    v
LoopResult.verify_integrity()
    |
    +-- verify_chain(events)
    +-- For each event: check self-hash, chain linkage, sequence
    +-- Return ChainVerificationResult(valid=True/False)
```

## 9. Dependency Diagram

```
Caller Code
    |
    v
arcrun.__init__ (public API)
    |
    +-- builtins/contained_execute.py  [NEW]
    |       +-- docker SDK (optional, lazy import)
    |
    +-- builtins/execute.py            [UNCHANGED]
    +-- builtins/spawn.py              [UNCHANGED]
    |
    +-- events.py                      [MODIFIED: hash chain, frozen, lock]
    +-- types.py                       [MODIFIED: verify_integrity(), Event typing]
    +-- loop.py                        [UNCHANGED]
    +-- state.py                       [UNCHANGED]
    +-- registry.py                    [UNCHANGED]
    +-- sandbox.py                     [UNCHANGED]
    +-- strategies/*                   [UNCHANGED]
```
