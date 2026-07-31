---
title: "Agent Runtime Hardening — Patterns and Anti-Patterns from SPEC-009 Review"
category: security-issues
date: 2026-02-21
tags:
  - immutability
  - hash-chain
  - async-sync-bridge
  - container-sandbox
  - event-system
  - thread-safety
  - cleanup-patterns
  - naming
  - defense-in-depth
module: arcrun
symptom: "Post-implementation review of Phase 4 hardening (events, sandbox, container isolation) surfaced 6 reusable patterns, 6 anti-patterns, and 4 tech debt traps"
root_cause: "N/A — review synthesis, not bug fix"
severity: informational
spec_id: SPEC-009
resolution_verified: true
---

# Agent Runtime Hardening — Patterns and Anti-Patterns

Distilled from the SPEC-009 (ArcRun Phase 4) post-implementation review. These learnings apply to any async agent runtime that needs immutable events, tamper-evident audit trails, container sandboxing, or thread-safe observer patterns.

---

## Patterns That Work

### 1. frozen dataclass + MappingProxyType for Immutable Events

Use `@dataclass(frozen=True)` for event types that must not be modified after creation. For nested dict data, wrap with `types.MappingProxyType` to prevent mutation of the data payload even though Python's frozen dataclass only prevents attribute reassignment.

```python
from dataclasses import dataclass
from types import MappingProxyType

@dataclass(frozen=True)
class Event:
    type: str
    timestamp: float
    data: MappingProxyType  # Prevents data["key"] = "tamper"

    def __post_init__(self) -> None:
        if isinstance(self.data, dict):
            object.__setattr__(self, "data", MappingProxyType(self.data))
```

**Why it works:** Zero runtime cost (no serialization/deserialization), enforced at the Python object level, and `frozen=True` gives you `__hash__` for free so events can be used in sets.

**Known limitation:** MappingProxyType is shallow. Nested dicts or lists inside the data payload remain mutable. If hash chain integrity depends on deep immutability, you must either deep-copy on construction or restrict data values to primitives. (Tracked as tech debt.)

### 2. SHA-256 Hash Chain with Genesis Sentinel

For tamper-evident audit trails, chain each event's hash to its predecessor using a well-known sentinel for the first event.

```python
GENESIS_PREV_HASH = "0" * 64  # Recognizable, deterministic

def _compute_event_hash(prev_hash: str, canonical: bytes) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + canonical).hexdigest()
```

**Key decisions:**
- Deterministic serialization via `json.dumps(sort_keys=True, separators=(",", ":"))` — no whitespace ambiguity.
- Hash chain provides tamper *detection*, not tamper *prevention*. Ed25519 signing is a separate concern (deferred appropriately).
- Verification function walks the full chain: self-hash, prev-hash link, and sequence continuity.

**When to use:** Any system that needs a verifiable audit trail but does not yet have a cryptographic signing infrastructure. The hash chain is a stepping stone that adds value immediately and is compatible with future signing.

### 3. asyncio.to_thread + asyncio.wait_for for Sync-to-Async Bridging

When wrapping a blocking synchronous library (e.g., Docker SDK) for use in an async runtime:

```python
async def _execute(params, ctx):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_run, code),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise SandboxTimeoutError(...) from None
```

**Why this combination:** `to_thread` offloads blocking I/O without blocking the event loop. `wait_for` adds a hard timeout that the sync code cannot violate. Together they provide both non-blocking behavior and resource bounding.

**Known limitation:** `wait_for` cancels the *awaitable*, but the underlying thread continues running until it completes or the process exits. For container operations, this means a timed-out container may be orphaned. Mitigate with external cleanup (garbage-collect containers by label on startup).

### 4. Defense-in-Depth Container Configuration

Layer 9 independent controls so that failure of any single control does not grant the attacker a useful capability:

| Control | Purpose |
|---------|---------|
| `cap_drop=["ALL"]` | No Linux capabilities |
| `security_opt=["no-new-privileges"]` | Cannot escalate via setuid |
| `user="65534:65534"` | Runs as `nobody` |
| `network_disabled=True` | No exfiltration channel |
| `read_only=True` | Cannot write to filesystem |
| `mem_limit`, `cpu_quota`, `pids_limit` | Resource exhaustion prevention |
| `tmpfs` with `noexec,nosuid` | Writable scratch without binary execution |
| Image digest pin warning | Supply chain integrity |
| `MAX_CODE_BYTES` pre-check | Reject oversized payloads before container creation |

**Principle:** Each control addresses a different attack surface. The combination means an attacker must defeat multiple independent barriers, not just one.

### 5. Observer Callback Outside Lock

When an event system has both a lock (for thread safety) and observer callbacks (for extensibility), always invoke callbacks *after* releasing the lock:

```python
def emit(self, event_type, data):
    with self._lock:
        # ... build event, append to list ...
        event = Event(...)
        self._events.append(event)
    # Callback OUTSIDE lock
    if self._on_event is not None:
        try:
            self._on_event(event)
        except Exception:
            logger.warning("Observer callback failed", exc_info=True)
    return event
```

**Why:** If the callback tries to emit another event (or call any method that acquires the same lock), you get a deadlock. This pattern makes the event system safe for recursive/reentrant use. Wrap the callback in try/except so a bad observer cannot break the event pipeline.

### 6. Individual try/except per Cleanup Step

When cleaning up resources (containers, connections, file handles), do NOT bundle cleanup into a single try block. Each step gets its own:

```python
def _cleanup_container(container, client):
    try:
        container.stop(timeout=2)
    except Exception:
        logger.warning("cleanup failed: stop", exc_info=True)
    try:
        container.remove(force=True)
    except Exception:
        logger.warning("cleanup failed: remove", exc_info=True)
    try:
        client.close()
    except Exception:
        logger.warning("cleanup failed: close", exc_info=True)
```

**Why:** If `stop()` fails, you still want to attempt `remove()` and `close()`. A single try/except would skip subsequent steps after the first failure. This pattern ensures maximum resource reclamation even in degraded states.

---

## Anti-Patterns to Avoid

### 1. Cryptic Variable Names in Security Code

**Bad:** `seq`, `ts`, `prev`, `eh` for sequence, timestamp, previous hash, event hash.

**Good:** `sequence`, `timestamp`, `prev_hash`, `event_hash`.

Security-critical code is reviewed more often and by more people than typical code. Saving a few characters actively harms auditability. The readability bar is *higher* for security code, not lower.

### 2. Lambda-in-Loop for Multi-Step Cleanup

**Bad:**
```python
for label, action in [("stop", lambda: c.stop()), ("remove", lambda: c.remove(force=True))]:
    try:
        action()
    except Exception:
        logger.warning(f"cleanup failed: {label}")
```

**Good:** Explicit try/except per step (see Pattern 6 above).

The lambda approach is "clever" but violates the flat/explicit principle. It adds cognitive overhead (reader must understand the loop and lambda captures) for marginal DRY benefit in what is typically a 3-step function.

### 3. Import Alias Inconsistency

**Bad:** `import docker` at module top for type hints, then `import docker as docker_sdk` inside a function for the actual client creation. The two aliases for the same package cause confusion about which is the "real" import.

**Good:** Pick one name and use it consistently. If the import is lazy (inside a function), use the canonical name. If you need a different name to avoid shadowing, document why with a comment.

### 4. Missing WHY Comments on Defensive Code

**Bad:**
```python
stdout_raw, stderr_raw = exec_result.output or (b"", b"")
```

**Good:**
```python
# demux=True returns (stdout, stderr) but either can be None
# when the stream produced no output; the outer `or` guards
# against .output itself being None on some SDK versions
stdout_raw, stderr_raw = exec_result.output or (b"", b"")
```

Defensive code that handles edge cases looks wrong or redundant to readers who do not know the edge case exists. The comment explains *why* the guard is there, preventing future developers from "simplifying" it away.

### 5. Duplicate Security Notices

**Bad:** Same security warning in both module-level docstring and function-level docstring. Readers see it twice; maintainers must update it in two places.

**Good:** Put the security notice at the module level (where it applies broadly). Function docstrings describe the function's behavior, not the module's security posture.

### 6. Methods Doing Too Many Things

**Bad:** `_sync_run` that creates a container, executes code, formats results, and handles cleanup.

**Good:** Extract `_create_container`, `_run_in_container`, and `_cleanup_container` as separate functions. `_sync_run` becomes an orchestrator that calls them in sequence. Each function is testable in isolation, and the orchestrator's control flow is obvious.

---

## Conventions Worth Adopting

### Factory Pattern for Sandbox Tools

Container-based tools follow a `make_*_tool()` factory pattern that returns a `Tool` dataclass. The factory:
1. Validates prerequisites (Docker SDK installed, image digest pinned)
2. Captures configuration in closure scope
3. Returns an immutable Tool with an async `execute` callable

This keeps tool registration declarative and tool implementation encapsulated.

### Error Hierarchy from a Single Base

```
SandboxError (base)
  +-- SandboxUnavailableError  (no runtime found)
  +-- SandboxTimeoutError      (exceeded deadline)
  +-- SandboxOOMError          (exit code 137)
  +-- SandboxRuntimeError      (script failed)
```

Callers can catch `SandboxError` for "any sandbox problem" or specific subclasses for targeted handling. This is standard Python practice but worth calling out because many codebases use generic `RuntimeError` or string-based error types.

### Lazy Import with Availability Check at Factory Time

Check that optional dependencies exist when the factory is called (tool registration), not when the module is imported. This means:
- Importing the module never fails (no import-time side effects)
- Missing dependencies produce a clear error message at configuration time
- The error surfaces before any work is done, not mid-execution

```python
def make_contained_execute_tool(...) -> Tool:
    try:
        import docker as _docker_check  # noqa: F401
    except ImportError:
        raise ImportError("Container support requires docker SDK...") from None
    # ... rest of factory
```

### Defensive Property Returns

When exposing internal collections through properties, return a copy:

```python
@property
def events(self) -> list[Event]:
    with self._lock:
        return list(self._events)
```

The caller gets a snapshot, not a reference to the internal list. Combined with the lock, this prevents both concurrent modification and external mutation.

---

## Tech Debt Patterns to Watch For

These are not bugs but design limitations that erode guarantees over time:

1. **Unbounded collections** — An event list that grows without limit will eventually consume all memory in a long-running agent. Add a `max_events` parameter with a sane default (e.g., 10,000) and rotate oldest events to disk or drop them.

2. **Shallow immutability** — `MappingProxyType` only protects the top-level dict. If event data contains nested dicts or lists, those remain mutable. If hash chain integrity matters, enforce deep immutability at construction time.

3. **Orphaned resources on async cancellation** — When `asyncio.wait_for` times out, the underlying thread keeps running. For container operations, this means containers may be left running. Mitigate with startup garbage collection (find and remove containers with a specific label).

4. **Weak security test assertions** — Tests that verify "the sandboxed code did not crash the host" are not the same as tests that verify "the sandboxed code was actually contained." Prefer assertions on specific containment properties: no network access, no file writes outside tmpfs, resource limits enforced.

---

## Applicability

These patterns and anti-patterns apply broadly to:

- Any async Python runtime that bridges sync libraries
- Event systems that need audit trails or tamper evidence
- Container-based code execution sandboxes
- Thread-safe observer/pub-sub implementations
- Multi-step resource cleanup in any language
- Security-critical code where readability is a safety property
