# ADR-005: Synchronous Filesystem I/O in Async Tool Functions

**Status**: Accepted
**Date**: 2026-02-15
**Decision Makers**: Josh Schultz
**Relates to**: S003 Phase 1b Review, Performance Review PERF-03

---

## Context

All built-in tools (read, write, grep, find, ls) are `async def` functions that use synchronous `pathlib.Path` methods for filesystem operations:

```python
# Example from read.py
async def read_file(path: str, ...) -> str:
    resolved = resolve_workspace_path(path, workspace)
    content = resolved.read_text(encoding="utf-8")  # Sync I/O
    return content
```

The review flagged this as a potential performance issue: synchronous I/O in async functions blocks the event loop, preventing concurrent task execution during disk operations.

## Decision

**Accept synchronous filesystem I/O as a Phase 1 design choice.** Local filesystem operations on modern SSDs complete in microseconds to low milliseconds. The complexity of wrapping every `Path` call in `asyncio.to_thread()` or `aiofiles` is not justified by the performance characteristics of the workload.

## Alternatives Considered

### 1. Use aiofiles throughout

Replace all `Path.read_text()`, `Path.write_text()`, `Path.iterdir()` with `aiofiles` equivalents. Rejected because:
- Adds a dependency for marginal benefit on local filesystems
- `aiofiles` wraps sync I/O in thread pools internally — same underlying mechanism
- Increases code complexity for every file operation
- Many `pathlib` operations (stat, exists, is_symlink) have no `aiofiles` equivalent

### 2. Use asyncio.to_thread() wrappers

Wrap sync calls in `await asyncio.to_thread(path.read_text)`. Rejected because:
- Thread pool overhead exceeds the I/O time for small local files
- Makes every file operation harder to read and debug
- No benefit when agents are not heavily concurrent on the same event loop

### 3. Use anyio for portability

Adopt `anyio.Path` as an async filesystem abstraction. Rejected because:
- Adds dependency complexity
- Phase 1 targets single-agent execution, not multi-agent concurrency on shared loops

## Rationale

1. **Local filesystem operations are fast.** On NVMe/SSD storage (target deployment environment), `read_text()` on a tool-sized file (<1MB) completes in <1ms. Event loop blocking is negligible.

2. **Agent tools are not highly concurrent.** In Phase 1, one agent runs one tool at a time. There is no concurrent tool execution where blocking would cause visible latency.

3. **Simplicity matters.** The tool code is readable, testable, and straightforward with synchronous `pathlib`. Adding async wrappers doubles the cognitive load for no measurable benefit.

4. **Phase 2 will introduce real concurrency.** When NATS-based multi-agent coordination arrives, we can profile actual contention and add async I/O where it matters.

## When to Revisit

This decision should be revisited when ANY of these conditions are met:

- **Network filesystems**: If tools access NFS, CIFS, or cloud-mounted storage (latency 10-100ms per operation)
- **Multi-agent event loop**: If multiple agents share a single asyncio event loop and file I/O causes measurable task starvation
- **Large file operations**: If tools routinely process files >10MB where read time exceeds 10ms
- **Profiling evidence**: If production profiling shows >5% of event loop time spent blocked on filesystem I/O

## Consequences

### Positive
- Clean, readable tool code using standard `pathlib`
- No additional dependencies (aiofiles, anyio)
- Easy to test with standard `tmp_path` fixtures
- Lower cognitive overhead for tool developers

### Negative
- Event loop blocks during file I/O (microseconds to low milliseconds)
- Cannot serve other async tasks during disk operations
- Requires migration effort if async I/O becomes necessary in Phase 2+

### Mitigations
- Tools enforce file size limits (read: 1MB default) preventing long blocking reads
- Workspace boundary validation prevents network path access
- Performance benchmarks in `tests/performance/` will detect regression if deployment targets change
