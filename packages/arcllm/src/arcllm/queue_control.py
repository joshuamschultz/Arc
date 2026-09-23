"""Injected, provider-scoped call admission and encrypted queue state."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Protocol

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass as validated_dataclass

from arcllm.exceptions import QueueFullError, QueueStateUnavailableError, QueueTimeoutError

QueueState = Literal[
    "queued", "running", "completed", "failed", "cancelled", "timed_out", "outcome_unknown"
]
_TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out", "outcome_unknown"})
_logger = logging.getLogger(__name__)


@validated_dataclass(config=ConfigDict(strict=True, allow_inf_nan=False), frozen=True, slots=True)
class QueueLimits:
    """Bound provider concurrency, waiting time, and stored history."""

    max_concurrent: int = 2
    max_queued: int = 10
    wait_timeout: float = 60.0
    history_limit: int = 1000

    def __post_init__(self) -> None:
        if self.max_concurrent < 1 or self.max_queued < 0:
            raise ValueError("invalid queue capacity")
        if self.wait_timeout <= 0 or self.history_limit < 1:
            raise ValueError("invalid queue timeout or history limit")


@validated_dataclass(config=ConfigDict(strict=True, allow_inf_nan=False), frozen=True, slots=True)
class CallQueueContext:
    """Opaque correlation supplied by a trusted outer run owner.

    These labels do not confer authority. The operator boundary must verify
    identity and scope before presenting jobs or invoking controls.
    """

    tenant_id: str = Field(min_length=1, max_length=256)
    owner_id: str = Field(min_length=1, max_length=256)
    call_id: str | None = Field(default=None, min_length=1, max_length=256)
    agent_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    run_id: str | None = Field(default=None, min_length=1, max_length=256)
    origin: str = Field(default="chat", min_length=1, max_length=64)
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=256)


@validated_dataclass(config=ConfigDict(strict=True, allow_inf_nan=False), frozen=True, slots=True)
class CallJob:
    """Payload-free call snapshot; version fences stale owners."""

    call_id: str
    tenant_id: str
    owner_id: str
    agent_id: str | None
    session_id: str | None
    run_id: str | None
    state: QueueState
    version: int
    created_at: float
    updated_at: float
    provider_scope: str | None = None
    attempt_id: str | None = None


class CallQueueStore(Protocol):
    """Atomic, durable state seam; implementations must fence by version."""

    async def create(self, job: CallJob) -> None: ...
    async def get(self, call_id: str) -> CallJob | None: ...
    async def compare_and_set(
        self,
        call_id: str,
        version: int,
        state: QueueState,
        owner_id: str,
        *,
        provider_scope: str | None = None,
        attempt_id: str | None = None,
    ) -> CallJob | None: ...
    async def list_jobs(
        self, *, tenant_id: str | None = None, offset: int = 0, limit: int = 100
    ) -> list[CallJob]: ...
    async def save_control(
        self, control: dict[str, Any], expected_revision: int
    ) -> int | None: ...
    async def load_control(self) -> dict[str, Any] | None: ...


class MemoryQueueStore:
    """Standalone ephemeral store with the same CAS semantics."""

    def __init__(self, *, history_limit: int = 1000) -> None:
        self._jobs: dict[str, CallJob] = {}
        self._control: dict[str, Any] | None = None
        self._history_limit = history_limit

    async def create(self, job: CallJob) -> None:
        """Create one unique call."""
        if job.call_id in self._jobs:
            raise ValueError("duplicate queue call ID")
        while len(self._jobs) >= self._history_limit:
            terminal = min(
                (existing for existing in self._jobs.values() if existing.state in _TERMINAL),
                key=lambda existing: existing.updated_at,
                default=None,
            )
            if terminal is None:
                raise QueueFullError(len(self._jobs), self._history_limit)
            del self._jobs[terminal.call_id]
        self._jobs[job.call_id] = job

    async def get(self, call_id: str) -> CallJob | None:
        """Return one snapshot."""
        return self._jobs.get(call_id)

    async def compare_and_set(
        self,
        call_id: str,
        version: int,
        state: QueueState,
        owner_id: str,
        *,
        provider_scope: str | None = None,
        attempt_id: str | None = None,
    ) -> CallJob | None:
        """Fence stale transitions."""
        old = self._jobs.get(call_id)
        if (
            old is None
            or old.version != version
            or old.owner_id != owner_id
            or old.state in _TERMINAL
        ):
            return None
        new = replace(
            old,
            state=state,
            version=version + 1,
            updated_at=time.time(),
            provider_scope=provider_scope or old.provider_scope,
            attempt_id=attempt_id or old.attempt_id,
        )
        self._jobs[call_id] = new
        return new

    async def list_jobs(
        self, *, tenant_id: str | None = None, offset: int = 0, limit: int = 100
    ) -> list[CallJob]:
        """Return a bounded page in newest-first order."""
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid queue page")
        jobs = sorted(self._jobs.values(), key=lambda job: job.updated_at, reverse=True)
        if tenant_id is not None:
            jobs = [job for job in jobs if job.tenant_id == tenant_id]
        return jobs[offset : offset + limit]

    async def save_control(self, control: dict[str, Any], expected_revision: int) -> int | None:
        """Save controls only at the current revision."""
        current = self._control["revision"] if self._control is not None else 0
        if current != expected_revision:
            return None
        revision = current + 1
        self._control = {**control, "revision": revision}
        return revision

    async def load_control(self) -> dict[str, Any] | None:
        """Read ephemeral controls."""
        return dict(self._control) if self._control is not None else None


@dataclass(slots=True)
class _Waiter:
    tenant_id: str
    ready: asyncio.Future[None]
    granted: bool = False


class _ProviderPool:
    def __init__(self) -> None:
        self.active = 0
        self.waiters: dict[str, deque[_Waiter]] = {}
        self.order: deque[str] = deque()
        self.last_tenant: str | None = None

    @property
    def waiting(self) -> int:
        return sum(len(queue) for queue in self.waiters.values())

    def enqueue(self, waiter: _Waiter) -> None:
        queue = self.waiters.setdefault(waiter.tenant_id, deque())
        queue.append(waiter)
        if waiter.tenant_id not in self.order:
            self.order.append(waiter.tenant_id)

    def remove(self, waiter: _Waiter) -> None:
        queue = self.waiters.get(waiter.tenant_id)
        if queue is None or waiter not in queue:
            return
        queue.remove(waiter)
        if not queue:
            del self.waiters[waiter.tenant_id]
            self.order.remove(waiter.tenant_id)

    def grant_next(self, limit: int) -> None:
        while self.active < limit and self.order:
            if len(self.order) > 1 and self.order[0] == self.last_tenant:
                self.order.rotate(-1)
            tenant = self.order.popleft()
            queue = self.waiters[tenant]
            waiter = queue.popleft()
            if queue:
                self.order.append(tenant)
            else:
                del self.waiters[tenant]
            if waiter.ready.done():
                continue
            self.last_tenant = tenant
            waiter.granted = True
            self.active += 1
            waiter.ready.set_result(None)


class CallQueueCoordinator:
    """Explicitly injected call owner and fair, provider-scoped scheduler.

    A process restart cannot continue Python callers. ``recover`` therefore
    marks queued calls failed and in-flight calls uncertain; an outer run owner
    must persist encrypted requests/results and explicitly decide resumption.
    """

    def __init__(
        self, *, store: CallQueueStore | None = None, limits: QueueLimits | None = None
    ) -> None:
        self.limits = limits or QueueLimits()
        self.store = store or MemoryQueueStore(history_limit=self.limits.history_limit)
        self._pools: dict[str, _ProviderPool] = {}
        self._live: dict[str, asyncio.Task[Any]] = {}
        self._provider_tasks: dict[str, asyncio.Task[Any]] = {}
        self._streams: dict[str, AsyncIterator[Any]] = {}
        self._execution: dict[str, tuple[float, float | None]] = {}
        self._owned_versions: dict[str, int] = {}
        self._paused = False
        self._control_revision = 0
        self._control_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._initialized = isinstance(self.store, MemoryQueueStore)
        self._current_context: contextvars.ContextVar[CallQueueContext | None] = (
            contextvars.ContextVar(f"arc_queue_context_{id(self)}", default=None)
        )

    @contextmanager
    def bind_context(self, context: CallQueueContext) -> Iterator[None]:
        """Bind verified outer-run correlation on this task and its children."""
        token = self._current_context.set(context)
        try:
            yield
        finally:
            self._current_context.reset(token)

    @property
    def current_context(self) -> CallQueueContext | None:
        """Return the current task's queue correlation, if any."""
        return self._current_context.get()

    async def initialize(self) -> None:
        """Load durable controller settings before accepting calls."""
        async with self._lifecycle_lock:
            if self._live:
                raise RuntimeError("cannot initialize queue with live calls")
            control = await self.store.load_control()
            if control is not None:
                if type(control.get("paused")) is not bool:
                    raise ValueError("invalid queue pause control")
                revision = control.get("revision")
                if type(revision) is not int or revision < 1:
                    raise ValueError("invalid queue control revision")
                self.limits = QueueLimits(**control["limits"])
                self._paused = control["paused"]
                self._control_revision = revision
            self._initialized = True

    async def register(self, context: CallQueueContext) -> CallJob:
        """Accept one caller only after its record is durable."""
        async with self._lifecycle_lock:
            return await self._register_unlocked(context)

    async def _register_unlocked(self, context: CallQueueContext) -> CallJob:
        if not self._initialized:
            raise QueueStateUnavailableError("durable queue not initialized")
        if not context.tenant_id or not context.owner_id:
            raise ValueError("queue tenant and owner are required")
        now = time.time()
        job = CallJob(
            call_id=context.call_id or uuid.uuid4().hex,
            tenant_id=context.tenant_id,
            owner_id=context.owner_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
            run_id=context.run_id,
            state="queued",
            version=0,
            created_at=now,
            updated_at=now,
        )
        await self.store.create(job)
        return job

    async def _transition(
        self,
        job: CallJob,
        state: QueueState,
        *,
        provider_scope: str | None = None,
        attempt_id: str | None = None,
    ) -> CallJob:
        version = self._owned_versions.get(job.call_id)
        if version is None:
            raise RuntimeError("queue owner lost")
        changed = await self.store.compare_and_set(
            job.call_id,
            version,
            state,
            job.owner_id,
            provider_scope=provider_scope,
            attempt_id=attempt_id,
        )
        if changed is None:
            raise RuntimeError("queue owner was fenced")
        self._owned_versions[job.call_id] = changed.version
        return changed

    async def _finish(self, job: CallJob, state: QueueState) -> bool:
        current = await self.store.get(job.call_id)
        if current is None or current.state in _TERMINAL:
            return False
        await self._transition(job, state)
        return True

    async def _finish_after_error(self, job: CallJob, state: QueueState) -> None:
        try:
            await self._finish(job, state)
        except BaseException as exc:
            _logger.warning("queue finalization unavailable: %s", type(exc).__name__)

    @asynccontextmanager
    async def call(
        self, context: CallQueueContext, *, execution_timeout: float = 180.0
    ) -> AsyncIterator[CallJob]:
        """Own a logical call across routed, retried, and fallback attempts."""
        if (
            isinstance(execution_timeout, bool)
            or not math.isfinite(execution_timeout)
            or execution_timeout <= 0
        ):
            raise ValueError("execution_timeout must be positive")
        async with self._lifecycle_lock:
            job = await self._register_unlocked(context)
            self._owned_versions[job.call_id] = job.version
            self._execution[job.call_id] = (execution_timeout, None)
            task = asyncio.current_task()
            if task is not None:
                self._live[job.call_id] = task
        try:
            yield job
        except asyncio.CancelledError:
            await self._finish_after_error(job, "cancelled")
            raise
        except QueueTimeoutError:
            await self._finish_after_error(job, "timed_out")
            raise
        except BaseException:
            await self._finish_after_error(job, "failed")
            raise
        else:
            if not await self._finish(job, "completed"):
                raise asyncio.CancelledError
        finally:
            self._live.pop(job.call_id, None)
            self._provider_tasks.pop(job.call_id, None)
            self._streams.pop(job.call_id, None)
            self._execution.pop(job.call_id, None)
            self._owned_versions.pop(job.call_id, None)

    def remaining_execution(self, job: CallJob) -> float:
        """Return the remaining provider budget, started at first wire admission."""
        timeout, deadline = self._execution[job.call_id]
        if deadline is None:
            raise RuntimeError("provider attempt was not admitted")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueueTimeoutError(timeout)
        return remaining

    def execution_timeout(self, job: CallJob) -> float:
        """Return the configured provider budget for a safe timeout error."""
        return self._execution[job.call_id][0]

    def bind_stream(self, call_id: str, stream: AsyncIterator[Any]) -> None:
        """Register a delegated iterator for cancellation while its caller is yielded."""
        self._streams[call_id] = stream

    @contextmanager
    def provider_task(self, job: CallJob) -> Iterator[None]:
        """Target cancellation at the task currently advancing this call."""
        task = asyncio.current_task()
        if task is not None:
            self._provider_tasks[job.call_id] = task
        try:
            yield
        finally:
            if task is not None and self._provider_tasks.get(job.call_id) is task:
                self._provider_tasks.pop(job.call_id, None)

    @asynccontextmanager
    async def attempt(self, provider_scope: str, job: CallJob) -> AsyncIterator[str]:
        """Admit one actual wire attempt in its provider capacity group."""
        pool = self._pools.setdefault(provider_scope, _ProviderPool())
        if pool.waiting >= self.limits.max_queued and (
            pool.active >= self.limits.max_concurrent or self._paused
        ):
            raise QueueFullError(pool.waiting, self.limits.max_queued)
        waiter = _Waiter(job.tenant_id, asyncio.get_running_loop().create_future())
        pool.enqueue(waiter)
        pool.grant_next(0 if self._paused else self.limits.max_concurrent)
        try:
            try:
                await asyncio.wait_for(waiter.ready, self.limits.wait_timeout)
            except TimeoutError:
                raise QueueTimeoutError(self.limits.wait_timeout) from None
            attempt_id = uuid.uuid4().hex
            await self._transition(
                job, "running", provider_scope=provider_scope, attempt_id=attempt_id
            )
            timeout, deadline = self._execution[job.call_id]
            if deadline is None:
                self._execution[job.call_id] = (timeout, time.monotonic() + timeout)
            yield attempt_id
        finally:
            pool.remove(waiter)
            if waiter.granted:
                pool.active -= 1
                waiter.granted = False
            pool.grant_next(0 if self._paused else self.limits.max_concurrent)

    async def jobs(
        self, *, tenant_id: str | None = None, offset: int = 0, limit: int = 100
    ) -> list[CallJob]:
        """Return a bounded page of payload-free jobs."""
        return await self.store.list_jobs(tenant_id=tenant_id, offset=offset, limit=limit)

    def snapshot(self) -> dict[str, Any]:
        """Return effective controller limits and live provider admission counts."""
        return {
            "max_concurrent": self.limits.max_concurrent,
            "max_queued": self.limits.max_queued,
            "wait_timeout_s": self.limits.wait_timeout,
            "paused": self._paused,
            "active": sum(pool.active for pool in self._pools.values()),
            "waiting": sum(pool.waiting for pool in self._pools.values()),
        }

    async def cancel(self, call_id: str, *, owner_id: str) -> bool:
        """Fence and cancel a live call; outer surface authorizes owner scope."""
        job = await self.store.get(call_id)
        if job is None or job.owner_id != owner_id or job.state in _TERMINAL:
            return False
        task = self._provider_tasks.get(call_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        stream = self._streams.get(call_id)
        close = getattr(stream, "aclose", None)
        if close is None:
            return False
        await close()
        changed = await self.store.compare_and_set(call_id, job.version, "cancelled", owner_id)
        return changed is not None

    async def pause(self) -> None:
        """Stop new provider attempts and persist controller state."""
        async with self._control_lock:
            await self._set_control(True, self.limits)

    async def resume(self) -> None:
        """Resume admission and persist controller state."""
        async with self._control_lock:
            await self._set_control(False, self.limits)

    async def tune(self, limits: QueueLimits) -> None:
        """Persist and apply new bounded limits."""
        async with self._control_lock:
            await self._set_control(self._paused, limits)

    async def _set_control(self, paused: bool, limits: QueueLimits) -> None:
        if not self._initialized:
            raise QueueStateUnavailableError("durable queue not initialized")
        revision = await self.store.save_control(
            {"paused": paused, "limits": asdict(limits)}, self._control_revision
        )
        if revision is None:
            raise QueueStateUnavailableError("stale queue controller revision")
        self._control_revision = revision
        self._paused = paused
        self.limits = limits
        for pool in self._pools.values():
            pool.grant_next(0 if self._paused else self.limits.max_concurrent)

    async def recover(self) -> int:
        """Truthfully close orphan states; never issue a provider request."""
        async with self._lifecycle_lock:
            if self._live:
                raise RuntimeError("cannot recover queue with live calls")
            return await self._recover_unlocked()

    async def _recover_unlocked(self) -> int:
        count = 0
        offset = 0
        while True:
            page = await self.store.list_jobs(offset=offset, limit=100)
            if not page:
                return count
            for job in page:
                if job.state not in {"queued", "running"}:
                    continue
                state: QueueState = "failed" if job.state == "queued" else "outcome_unknown"
                if await self.store.compare_and_set(job.call_id, job.version, state, job.owner_id):
                    count += 1
            offset += len(page)
