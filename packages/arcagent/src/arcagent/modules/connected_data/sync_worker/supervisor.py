"""Start, watch and restart the sync worker; hand the main process a channel to it.

One supervisor per process. ``arc ui start`` starts it with the dashboard and
stops it on shutdown; any other process that hosts an agent starts it on its
first store write. It is never a second systemd unit and needs no configuration.

* **Spawn.** The child is ``python -m arcagent.modules.connected_data.sync_worker``
  from the same interpreter (the same runtime venv). A fresh 32-byte secret is
  made for every spawn and written to the child's stdin with the socket paths,
  never to argv or the environment. Both sockets live in a private 0700
  directory.
* **No orphans.** The child exits when its stdin reaches EOF (the parent closed
  it or died), when its parent pid changes, and on Linux by the parent-death
  signal. ``stop`` closes stdin, sends SIGTERM and, after a grace period, SIGKILL.
* **Watchdog.** A ping every ``heartbeat_interval``; ``heartbeat_misses`` missed
  in a row means the worker is hung: it is killed and restarted.
* **Restart.** Capped exponential backoff with jitter; the backoff resets once
  a worker has stayed up for ``stable_after`` seconds.
* **While it is down.** Every write fails fast with ``SyncWorkerUnavailableError``
  (a sync run defers and keeps its cursor), and reads keep working: they never
  needed the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from arctrust.audit import AuditSink

from arcagent.modules.connected_data.sync_worker.host import HostService
from arcagent.modules.connected_data.sync_worker.protocol import (
    HOST_REQUEST,
    HOST_RESPONSE,
    WORKER_REQUEST,
    WORKER_RESPONSE,
)
from arcagent.modules.connected_data.sync_worker.remote_port import replay_audit
from arcagent.modules.connected_data.sync_worker.rpc import (
    RemoteError,
    RpcClient,
    RpcConnectError,
    RpcServer,
    RpcUnavailableError,
)
from arcagent.modules.connected_data.sync_worker.specs import (
    StoreSpec,
    SyncWorkerUnavailableError,
    raise_from_wire,
)

_logger = logging.getLogger("arcagent.modules.connected_data.sync_worker.supervisor")

WorkerState = Literal["up", "restarting", "down"]

#: The child the supervisor runs: this package, under this interpreter.
DEFAULT_COMMAND: tuple[str, ...] = (
    sys.executable,
    "-m",
    "arcagent.modules.connected_data.sync_worker",
)
#: macOS caps a Unix socket path at 104 bytes; Linux at 108.
_MAX_SOCKET_PATH = 100


@dataclass(frozen=True)
class SyncWorkerStatus:
    """What the dashboard shows: up, restarting (with why) or down."""

    state: WorkerState
    pid: int | None = None
    restarts: int = 0
    detail: str = ""
    #: Seconds until the next start attempt, while restarting.
    retry_in_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "pid": self.pid,
            "restarts": self.restarts,
            "detail": self.detail,
            "retry_in_seconds": self.retry_in_seconds,
        }


def _runtime_dir() -> Path:
    """A private directory for the two sockets, short enough for a socket path."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    if len(base) > _MAX_SOCKET_PATH - 40 or not Path(base).is_dir():
        base = "/tmp"  # noqa: S108 — reason: only when the per-user dir is too long for AF_UNIX; mkdtemp makes it 0700
    return Path(tempfile.mkdtemp(prefix="arc-sync-", dir=base))


class SyncWorkerSupervisor:
    """Own one sync worker child: spawn it, watch it, restart it, stop it."""

    def __init__(
        self,
        host: HostService,
        *,
        command: Sequence[str] = DEFAULT_COMMAND,
        ready_timeout: float = 60.0,
        heartbeat_interval: float = 5.0,
        heartbeat_misses: int = 4,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
        stable_after: float = 60.0,
        stop_grace: float = 5.0,
        write_timeout: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = lambda: 0.9 + secrets.randbelow(201) / 1000,
    ) -> None:
        self.host = host
        self._command = tuple(command)
        self._ready_timeout = ready_timeout
        self._heartbeat = heartbeat_interval
        self._misses = heartbeat_misses
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._stable_after = stable_after
        self._stop_grace = stop_grace
        self._write_timeout = write_timeout
        self._clock = clock
        self._jitter = jitter
        self._dir: Path | None = None
        self._host_server: RpcServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._secret: bytes | None = None
        self._client: RpcClient | None = None
        self._ready = asyncio.Event()
        self._stopping = False
        self._state: WorkerState = "down"
        self._detail = "not started"
        self._restarts = 0
        self._next_attempt: float | None = None
        self._start_lock = asyncio.Lock()
        #: A worker has answered since ``start``: later writes fail fast while it restarts.
        self._ever_up = False
        #: Each served agent's embed settings; every new worker loads them before "up".
        self._warm_set: set[tuple[str, tuple[str, str, str]]] = set()

    # -- what the rest of the process sees ---------------------------------

    def status(self) -> SyncWorkerStatus:
        retry = None
        if self._state == "restarting" and self._next_attempt is not None:
            retry = max(0.0, self._next_attempt - self._clock())
        pid = self._proc.pid if self._proc is not None and self._state == "up" else None
        return SyncWorkerStatus(self._state, pid, self._restarts, self._detail, retry)

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop this supervisor was started on, while it supervises."""
        return self._task.get_loop() if self._task is not None else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def channel(self) -> SupervisedChannel:
        return SupervisedChannel(self)

    def client(self) -> RpcClient:
        """The live worker's client, or ``SyncWorkerUnavailableError`` while it is not up."""
        client = self._client
        if client is None or self._state != "up":
            raise SyncWorkerUnavailableError(
                f"sync worker is {self._state}: {self._detail}", retry_after=self.retry_after()
            )
        return client

    def retry_after(self) -> float:
        status = self.status()
        if status.retry_in_seconds is not None:
            return status.retry_in_seconds + self._heartbeat
        return self._heartbeat

    @property
    def write_timeout(self) -> float:
        return self._write_timeout

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start supervising (idempotent); returns once the first spawn was attempted."""
        async with self._start_lock:
            if self.running:
                return
            self._stopping = False
            self._ever_up = False
            self._dir = _runtime_dir()
            self._host_server = RpcServer(
                path=self._dir / "host.sock",
                secret=lambda: self._secret,
                request_direction=HOST_REQUEST,
                response_direction=HOST_RESPONSE,
                handler=self.host.handle,
                expected_pid=lambda: self._proc.pid if self._proc is not None else -1,
                on_refused=self._host_refused,
            )
            await self._host_server.start()
            self._state, self._detail = "restarting", "starting"
            self._task = asyncio.create_task(self._supervise(), name="sync-worker-supervisor")

    async def ensure_started(self) -> None:
        """Start on first use and wait (bounded) for that first worker to answer.

        Only the first start is waited for. Once a worker has been up, a write
        while it restarts fails fast instead: the sync run defers and keeps its
        cursor rather than holding a page open through the backoff.
        """
        if not self.running:
            await self.start()
        if self._ever_up:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), self._ready_timeout)

    async def await_responsive(self) -> bool:
        """Wait (bounded by the ready timeout) until the live worker answers a ping.

        For a caller whose connect failed before anything was sent: the worker may
        be warming a model, or this process's own loop was starved at startup and
        the connect timer fired late. ``False`` when the worker is not up or never
        answered; the caller then defers, typed and retryable.
        """
        deadline = self._clock() + self._ready_timeout
        while self._clock() < deadline:
            client = self._client
            if client is None or self._state != "up":
                return False
            try:
                await client.call("ping", {}, timeout=self._heartbeat)
                return True
            except (RpcUnavailableError, RemoteError):
                await asyncio.sleep(0.05)
        return False

    async def wait_ready(self, timeout: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout)
        return self._state == "up"

    async def warm(self, agent_did: str, embed: tuple[str, str, str], *, timeout: float) -> None:
        """Have the worker load ``agent_did``'s embedding model before its first write.

        Remembered, so every worker spawned later loads it again before it reports
        up. Bounded: a load that outlasts ``timeout`` goes on in the worker, and the
        first write waits for it there.
        """
        self._warm_set.add((agent_did, embed))
        client = self._client
        if client is not None and self._state == "up":
            await _warm_one(client, agent_did, embed, timeout)

    async def stop(self) -> None:
        """Stop the worker and supervision; the worker is never left running."""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._terminate()
        if self._host_server is not None:
            await self._host_server.close()
            self._host_server = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._state, self._detail = "down", "stopped"

    def discard(self) -> None:
        """Kill the child and remove the sockets without an event loop.

        For a process whose loop is already gone (an interpreter exit, a test's
        teardown): ``stop`` is the orderly path whenever a loop is running.
        """
        self._stopping = True
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, signal.SIGKILL)
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._task = None
        self._state, self._detail = "down", "stopped"

    async def kill_worker(self) -> None:
        """Kill the current child outright (operator restart, or a test of recovery)."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    # -- supervision -------------------------------------------------------

    async def _supervise(self) -> None:
        delay = self._backoff_initial
        while not self._stopping:
            began = self._clock()
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reason: supervision must outlive one failed spawn
                _logger.exception("sync worker spawn failed")
                self._detail = f"spawn failed: {type(exc).__name__}"
            self._client = None
            self._ready.clear()
            await self._terminate()
            if self._stopping:
                return
            if self._clock() - began >= self._stable_after:
                delay = self._backoff_initial
            wait = delay * self._jitter()
            self._state = "restarting"
            self._next_attempt = self._clock() + wait
            _logger.warning("sync worker %s; restarting in %.1fs", self._detail, wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, self._backoff_max)
            self._restarts += 1

    async def _run_once(self) -> None:
        if self._dir is None:
            raise RuntimeError("supervisor is not started")
        secret = secrets.token_bytes(32)
        worker_socket = self._dir / "worker.sock"
        with contextlib.suppress(FileNotFoundError):
            worker_socket.unlink()
        proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
        )
        self._proc, self._secret = proc, secret
        bootstrap = {
            "secret": secret.hex(),
            "socket": str(worker_socket),
            "host_socket": str(self._dir / "host.sock"),
            "parent_pid": os.getpid(),
        }
        if proc.stdin is None:
            raise RuntimeError("sync worker has no stdin")
        proc.stdin.write(json.dumps(bootstrap).encode("utf-8") + b"\n")
        await proc.stdin.drain()
        client = RpcClient(
            path=worker_socket,
            secret=secret,
            request_direction=WORKER_REQUEST,
            response_direction=WORKER_RESPONSE,
            expected_pid=proc.pid,
        )
        if not await self._await_ready(client, proc):
            return
        # A new worker holds no model: load what the served agents embed with
        # before it is "up", so a run's first page never pays a cold load.
        for agent_did, embed in sorted(self._warm_set):
            await _warm_one(client, agent_did, embed, self._ready_timeout)
        self._client = client
        self._state, self._detail, self._next_attempt = "up", "", None
        self._ever_up = True
        self._ready.set()
        _logger.info("sync worker up (pid %s)", proc.pid)
        await self._watch(client, proc)

    async def _await_ready(self, client: RpcClient, proc: asyncio.subprocess.Process) -> bool:
        deadline = self._clock() + self._ready_timeout
        while self._clock() < deadline:
            if proc.returncode is not None:
                self._detail = f"exited with code {proc.returncode} while starting"
                return False
            try:
                await client.call("ping", {}, timeout=self._heartbeat)
                return True
            except (RpcUnavailableError, RemoteError):
                await asyncio.sleep(0.05)
        self._detail = "did not start in time"
        return False

    async def _watch(self, client: RpcClient, proc: asyncio.subprocess.Process) -> None:
        misses = 0
        exited = asyncio.ensure_future(proc.wait())
        try:
            while True:
                done, _ = await asyncio.wait({exited}, timeout=self._heartbeat)
                if done:
                    self._detail = f"exited with code {proc.returncode}"
                    return
                try:
                    await client.call("ping", {}, timeout=self._heartbeat)
                    misses = 0
                except (RpcUnavailableError, RemoteError):
                    misses += 1
                    if misses >= self._misses:
                        self._detail = "hung (missed heartbeats); killed"
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        await exited
                        return
        finally:
            if not exited.done():
                exited.cancel()

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        self._secret = None
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), self._stop_grace)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    def _host_refused(self, reason: str) -> None:
        _logger.warning("sync worker host channel refused a request: %s", reason)


async def _warm_one(
    client: RpcClient, agent_did: str, embed: tuple[str, str, str], timeout: float
) -> None:
    """One warm request; a failure only costs the first write a cold load."""
    try:
        await client.call("warm", {"agent_did": agent_did, "embed": list(embed)}, timeout=timeout)
    except (RpcUnavailableError, RemoteError) as exc:
        _logger.warning("sync worker did not warm an embedder: %s", exc)


class SupervisedChannel:
    """Send one store write to whichever worker is live now."""

    def __init__(self, supervisor: SyncWorkerSupervisor) -> None:
        self._supervisor = supervisor

    async def write(
        self,
        store: StoreSpec,
        method: str,
        args: dict[str, Any],
        body: bytes = b"",
        *,
        audit: AuditSink | None = None,
    ) -> tuple[Any, bytes]:
        supervisor = self._supervisor
        await supervisor.ensure_started()
        request = {"store": store.model_dump(mode="json"), "method": method, "args": args}
        try:
            result, out = await self._call_once_ready(request, body)
        except RpcUnavailableError as exc:
            raise SyncWorkerUnavailableError(
                f"sync worker did not answer: {exc}", retry_after=supervisor.retry_after()
            ) from exc
        except RemoteError as exc:
            replay_audit(exc.error.get("audit"), audit)
            raise_from_wire(exc)
        replay_audit(result.get("audit") if isinstance(result, dict) else None, audit)
        return (result.get("value") if isinstance(result, dict) else None), out

    async def _call_once_ready(self, request: dict[str, Any], body: bytes) -> tuple[Any, bytes]:
        """Send the write; if the connect itself failed, wait for the worker, then send once more.

        A connect failure sent nothing, so the retry cannot double-write. Any
        failure after the request left (no answer) is never retried here: the
        write may have landed, and the coordinator's own cursor decides.
        """
        supervisor = self._supervisor
        try:
            return await supervisor.client().call(
                "write", request, body, timeout=supervisor.write_timeout
            )
        except RpcConnectError:
            if not await supervisor.await_responsive():
                raise
        return await supervisor.client().call(
            "write", request, body, timeout=supervisor.write_timeout
        )


# -- the one supervisor of this process --------------------------------------

#: One per process by design: the worker writes every store this process's agents
#: hold, and two supervisors would mean two writers. Not configuration; the slot
#: the process's own lifecycle fills and clears.
_PROCESS_SUPERVISOR: SyncWorkerSupervisor | None = None


def process_supervisor() -> SyncWorkerSupervisor:
    """This process's sync worker supervisor (made on first use, started on first write)."""
    global _PROCESS_SUPERVISOR
    if _PROCESS_SUPERVISOR is None:
        _PROCESS_SUPERVISOR = SyncWorkerSupervisor(
            HostService(arcstore_opener=None, audit_sink=None)
        )
    return _PROCESS_SUPERVISOR


async def shutdown_process_supervisor() -> None:
    """Stop and forget this process's supervisor (process shutdown, or between tests)."""
    global _PROCESS_SUPERVISOR
    supervisor, _PROCESS_SUPERVISOR = _PROCESS_SUPERVISOR, None
    if supervisor is not None:
        await supervisor.stop()


def discard_process_supervisor() -> None:
    """Stop and forget this process's supervisor from code that is not in a coroutine.

    Stops it in order on the loop it was started on when that loop is still open
    and idle; otherwise (the loop is gone) kills the child outright.
    """
    global _PROCESS_SUPERVISOR
    supervisor, _PROCESS_SUPERVISOR = _PROCESS_SUPERVISOR, None
    if supervisor is None:
        return
    loop = supervisor.loop
    if loop is not None and not loop.is_closed() and not loop.is_running():
        loop.run_until_complete(supervisor.stop())
        return
    supervisor.discard()


__all__ = [
    "DEFAULT_COMMAND",
    "SupervisedChannel",
    "SyncWorkerStatus",
    "SyncWorkerSupervisor",
    "WorkerState",
    "discard_process_supervisor",
    "process_supervisor",
    "shutdown_process_supervisor",
]
