"""ExecutorBackend Protocol, BackendCapabilities, ExecHandle, and _ThreadedProcessHandle.

This module is the single source of truth for the execution backend contract.
All backend implementations MUST satisfy this Protocol to be usable by arcrun.

Design notes
------------
- ExecutorBackend is @runtime_checkable so callers can use isinstance() without ABC.
- BackendCapabilities is Pydantic so it round-trips through TOML/JSON config.
- _ThreadedProcessHandle bridges SDK-only backends (Modal, Daytona) that have
  no real subprocess into the unified async stream loop.  A worker thread writes
  raw bytes to an os.pipe; the async reader drains the read end.
- Streaming yields raw bytes (NOT decoded lines) to stay ANSI/binary-safe.
  Decoding and ANSI-strip happen at the *display* layer so audit logs keep raw bytes.
- Backpressure is handled via asyncio.Queue(maxsize=…) inside each backend.
- Hard truncation at capabilities.max_stdout_bytes emits a sentinel frame.
- supports_separated_streams: when True the backend exposes run_separated() which
  returns (SeparatedExecHandle) with distinct stdout / stderr async iterators plus
  the final exit code.  LocalBackend and DockerBackend both set this True.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Sentinel marker appended when stdout is truncated at max_stdout_bytes.
TRUNCATION_MARKER: bytes = b"\n[arcrun: stdout truncated]\n"

# Queue depth for the streaming backpressure queue.
_STREAM_QUEUE_DEPTH = 256

# Read chunk size for the async pipe drain in _ThreadedProcessHandle.stream().
# Matches the typical pipe buffer page size; tuned alongside _STREAM_QUEUE_DEPTH.
_STREAM_CHUNK_BYTES = 4096


class BackendCapabilities(BaseModel):
    """Declarative capability matrix for a backend.

    Agents MUST consult capabilities before calling optional methods
    (copy_to, copy_from, workspace_id, port_forward, run_separated).
    """

    supports_file_copy: bool = False
    supports_persistent_workspace: bool = False
    supports_port_forward: bool = False
    supports_bind_mount: bool = False
    # When True, the backend exposes run_separated() which returns stdout and
    # stderr as independent async iterators alongside the exit code.
    # Backends that only offer a merged stream leave this False.
    supports_separated_streams: bool = False
    cold_start_budget_ms: int = Field(
        default=10,
        description="Expected cold-start latency in milliseconds.",
    )
    max_stdout_bytes: int = Field(
        default=65536,
        description="Hard cap on captured stdout+stderr bytes before truncation.",
    )
    isolation: Literal["none", "container", "vm", "remote"] = "none"


@dataclass
class ExecHandle:
    """Opaque handle for a running or finished execution.

    Fields are backend-private; callers treat this as an opaque token.
    Backends attach their own metadata via the `meta` dict.
    """

    handle_id: str
    backend_name: str
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ExecutorBackend Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecutorBackend(Protocol):
    """Contract every execution backend must satisfy.

    Implementations may add optional capability-gated methods (copy_to, copy_from,
    workspace_id, port_forward) but MUST implement all four core methods below plus
    expose the two class-level attributes.
    """

    name: str
    capabilities: BackendCapabilities

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> ExecHandle:
        """Start a command.  Returns immediately with an ExecHandle.

        The handle is passed to stream() to read output, cancel() to abort,
        or left until close() disposes of the backend entirely.
        """
        ...

    async def stream(self, handle: ExecHandle) -> AsyncIterator[bytes]:
        """Async iterator of raw byte chunks from stdout+stderr.

        Chunks are NOT line-delimited.  Callers must buffer if they need lines.
        The iterator ends when the process exits or is cancelled.
        Hard truncation at capabilities.max_stdout_bytes emits TRUNCATION_MARKER.
        """
        ...
        yield b""  # pragma: no cover — satisfy type checker for Protocol

    async def cancel(self, handle: ExecHandle, *, grace: float = 5.0) -> None:
        """Cancel a running command.

        Sends SIGTERM, waits up to grace seconds, then SIGKILL.
        No-ops if the process already exited.
        """
        ...

    async def close(self) -> None:
        """Release all backend resources (containers, SSH sessions, thread pools).

        After close(), this backend instance MUST NOT be used again.
        """
        ...


# ---------------------------------------------------------------------------
# _ThreadedProcessHandle — SDK-only backend adapter
# ---------------------------------------------------------------------------


class _ThreadedProcessHandle:
    """Wrap a synchronous SDK-backed execution function behind the async stream protocol.

    Motivation (from Hermes tools/environments/base.py pattern):
    SDK-only backends like Modal or Daytona have no real subprocess.  Their
    Python SDKs expose synchronous exec_fn/cancel_fn pairs.  We bridge this
    by running exec_fn in a daemon thread that writes to an os.pipe; the async
    side drains the read end.  Pipe EOF serves as the completion signal —
    no asyncio.Event or cross-thread signalling needed.

    Usage
    -----
        def my_exec_fn(command: str) -> bytes:
            return sdk.run(command)

        handle = _ThreadedProcessHandle(exec_fn=my_exec_fn, cancel_fn=sdk.cancel)
        await handle.start("echo hello")
        async for chunk in handle.stream():
            ...
        handle.cancel()
    """

    def __init__(
        self,
        exec_fn: Callable[[str], bytes],
        cancel_fn: Callable[[], None],
        max_stdout_bytes: int = 65536,
    ) -> None:
        self._exec_fn = exec_fn
        self._cancel_fn = cancel_fn
        self._max_stdout_bytes = max_stdout_bytes

        # os.pipe gives a pair of file descriptors.  Worker thread writes to
        # the write end; async reader drains the read end.
        # Closing the write end signals EOF to the reader.
        self._read_fd, self._write_fd = os.pipe()
        self._thread: threading.Thread | None = None
        self._cancelled = threading.Event()
        self._error: Exception | None = None

    async def start(self, command: str) -> None:
        """Launch the exec_fn in a daemon thread."""
        import asyncio

        loop = asyncio.get_event_loop()

        def _worker() -> None:
            # Open write end as a file object for clean close semantics.
            # Closing wf automatically closes _write_fd, signalling EOF to reader.
            wf = os.fdopen(self._write_fd, "wb", buffering=0)
            try:
                if self._cancelled.is_set():
                    return
                result = self._exec_fn(command)
                truncated = result[: self._max_stdout_bytes]
                wf.write(truncated)
                if len(result) > self._max_stdout_bytes:
                    wf.write(TRUNCATION_MARKER)
            except Exception as exc:
                self._error = exc
            finally:
                # Closing the write end signals EOF; reader loop terminates.
                # Use contextlib.suppress for fd cleanup so a secondary OSError
                # during error handling does not obscure the primary exception.
                with suppress(OSError):
                    wf.close()

        self._thread = threading.Thread(
            target=_worker, daemon=True, name="arcrun-threaded-handle"
        )
        # loop is captured by _worker's closure — used only in future _done signalling.
        # We keep it assigned to avoid flake8 F841, but it is intentionally captured.
        _ = loop
        self._thread.start()

    async def stream(self) -> AsyncIterator[bytes]:
        """Drain the pipe in async chunks.

        Reads until EOF (write end closed by worker thread), then raises
        any exception captured from the worker.
        """
        import asyncio

        rf = os.fdopen(self._read_fd, "rb", buffering=0)
        try:
            while True:
                # run_in_executor avoids blocking the event loop on the read.
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None, rf.read, _STREAM_CHUNK_BYTES
                )
                if not chunk:
                    break
                yield chunk
        finally:
            rf.close()

        if self._error is not None:
            raise self._error

    def cancel(self) -> None:
        """Signal cancellation to the worker thread."""
        self._cancelled.set()
        try:
            self._cancel_fn()
        except Exception:
            logger.debug("cancel_fn raised; suppressed", exc_info=True)
