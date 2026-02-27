"""Scheduler engine — timer loop + execution queue — SPEC-002.

Evaluates cron, interval, and one-time schedules. Executes via
agent_run_fn callback with timeout enforcement and circuit breaker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from croniter import croniter

from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.store import ScheduleStore

if TYPE_CHECKING:
    from arcagent.core.module_bus import ModuleBus

_logger = logging.getLogger("arcagent.scheduler")

AgentRunFn = Callable[..., Awaitable[Any]]


class SchedulerEngine:
    """Core scheduling engine with timer loop and sequential execution queue."""

    def __init__(
        self,
        store: ScheduleStore,
        config: SchedulerConfig,
        telemetry: AgentTelemetry,
        agent_run_fn: AgentRunFn,
        bus: ModuleBus | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._telemetry = telemetry
        self._agent_run_fn = agent_run_fn
        self._bus = bus

        self._queue: asyncio.Queue[ScheduleEntry] = asyncio.Queue(maxsize=100)
        self._in_flight: set[str] = set()
        self._fire_and_forget: set[asyncio.Task[Any]] = set()
        self._timer_task: asyncio.Task[None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._running = False
        self._timer_consecutive_errors = 0
        self._ready = asyncio.Event()  # Set when agent_run_fn is bound

    @property
    def running(self) -> bool:
        return self._running

    def set_agent_run_fn(self, fn: AgentRunFn) -> None:
        """Bind or rebind the agent.run() callback."""
        self._agent_run_fn = fn
        self._ready.set()

    # --- Public API ---

    async def start(self) -> None:
        """Start the timer loop and worker task."""
        self._running = True
        self._timer_task = asyncio.create_task(self._timer_loop())
        self._worker_task = asyncio.create_task(self._worker())
        _logger.info("Scheduler engine started")

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop the engine, draining the queue before shutdown."""
        self._running = False
        self._ready.set()  # Unblock timer loop if still waiting for readiness.

        if self._timer_task is not None:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass

        # Drain remaining queue items.
        if not self._queue.empty():
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except TimeoutError:
                _logger.warning("Queue drain timed out after %.1fs", timeout)

        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        self._in_flight.clear()
        _logger.info("Scheduler engine stopped")

    async def enqueue(self, entry: ScheduleEntry) -> None:
        """Put a schedule entry into the execution queue (with dedup)."""
        if entry.id in self._in_flight:
            _logger.debug("Skipping duplicate enqueue for %s", entry.id)
            return
        self._in_flight.add(entry.id)
        await self._queue.put(entry)

    async def execute(self, entry: ScheduleEntry) -> Any:
        """Execute a single schedule entry via agent_run_fn.

        Handles timeout and updates metadata on success or failure.
        """
        start_time = time.monotonic()
        timeout = entry.timeout_seconds

        try:
            result = await asyncio.wait_for(
                self._agent_run_fn(
                    entry.prompt,
                    tool_choice={"type": "any"},
                ),
                timeout=timeout,
            )
            elapsed = time.monotonic() - start_time
            self._on_execution_complete(entry, result, elapsed)
            return result
        except TimeoutError:
            elapsed = time.monotonic() - start_time
            _logger.warning(
                "Schedule %s timed out after %.1fs",
                entry.id,
                elapsed,
            )
            self.on_execution_failed(entry, TimeoutError(f"Timed out after {timeout}s"))
            return None
        except Exception as exc:
            elapsed = time.monotonic() - start_time
            _logger.error(
                "Schedule %s failed after %.1fs: %s",
                entry.id,
                elapsed,
                exc,
            )
            self.on_execution_failed(entry, exc)
            return None

    # --- Evaluation ---

    def should_fire(self, entry: ScheduleEntry) -> bool:
        """Determine if a schedule should fire right now."""
        if not entry.enabled:
            return False

        now = datetime.now(tz=UTC)

        if entry.type == "interval":
            return self._should_fire_interval(entry, now)
        if entry.type == "cron":
            return self._should_fire_cron(entry, now)
        if entry.type == "once":
            return self._should_fire_once(entry, now)
        return False

    def is_within_active_hours(self, entry: ScheduleEntry) -> bool:
        """Check if current time is within the entry's active hours.

        Supports overnight windows (e.g. 22:00-06:00).
        """
        if entry.active_hours is None:
            return True

        tz = ZoneInfo(entry.active_hours.timezone)
        now_local = datetime.now(tz=UTC).astimezone(tz)

        start_h, start_m = map(int, entry.active_hours.start.split(":"))
        end_h, end_m = map(int, entry.active_hours.end.split(":"))

        current_minutes = now_local.hour * 60 + now_local.minute
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m

        # Handle overnight windows (e.g. 22:00-06:00).
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes

    # --- Circuit breaker ---

    def on_execution_failed(
        self,
        entry: ScheduleEntry,
        error: BaseException,
    ) -> ScheduleEntry:
        """Handle execution failure. Returns updated entry.

        Persists consecutive_failures in metadata. After
        circuit_breaker_threshold consecutive failures, disables the schedule.
        """
        # Re-read from store for latest failure count (entry may be stale).
        stored = self._store.get(entry.id)
        if isinstance(stored, ScheduleEntry):
            base_failures = stored.metadata.consecutive_failures
        else:
            base_failures = entry.metadata.consecutive_failures
        new_failures = base_failures + 1
        threshold = self._config.circuit_breaker_threshold

        updates = self._build_metadata_update(
            entry,
            last_result="error",
            consecutive_failures=new_failures,
        )

        if new_failures >= threshold:
            updates["enabled"] = False
            _logger.warning(
                "Circuit breaker tripped for %s after %d failures",
                entry.id,
                new_failures,
            )

        try:
            self._store.update(entry.id, updates)
        except KeyError:
            pass

        # Emit bus event so other modules (e.g. Telegram) can notify user.
        if self._bus is not None:
            self._emit_bus_event(
                "schedule:failed",
                {
                    "schedule_id": entry.id,
                    "schedule_name": entry.prompt[:80],
                    "error": str(error),
                    "consecutive_failures": new_failures,
                },
            )

        # Return updated entry for caller.
        data = entry.model_dump()
        data.update(updates)
        return ScheduleEntry(**data)

    # --- Private ---

    def _emit_bus_event(self, event: str, data: dict[str, Any]) -> None:
        """Fire-and-forget bus event emission with proper task reference tracking."""
        task = asyncio.ensure_future(self._bus.emit(event, data))
        self._fire_and_forget.add(task)
        task.add_done_callback(self._fire_and_forget.discard)

    def _build_metadata_update(
        self,
        entry: ScheduleEntry,
        *,
        last_result: str = "ok",
        run_count_increment: int = 0,
        elapsed: float | None = None,
        consecutive_failures: int = 0,
    ) -> dict[str, Any]:
        """Build a metadata update dict — single source for metadata mutations."""
        meta_data = entry.metadata.model_dump()
        meta_data["last_run"] = datetime.now(tz=UTC).isoformat()
        meta_data["last_result"] = last_result
        meta_data["consecutive_failures"] = consecutive_failures
        if run_count_increment:
            meta_data["run_count"] = entry.metadata.run_count + run_count_increment
        if elapsed is not None:
            meta_data["last_duration_seconds"] = round(elapsed, 3)
        return {"metadata": meta_data}

    def _should_fire_interval(
        self,
        entry: ScheduleEntry,
        now: datetime,
    ) -> bool:
        if not entry.metadata.last_run:
            return True
        last = datetime.fromisoformat(entry.metadata.last_run)
        return last + timedelta(seconds=entry.every_seconds or 0) <= now

    def _should_fire_cron(self, entry: ScheduleEntry, now: datetime) -> bool:
        if entry.expression is None:
            return False

        if entry.metadata.last_run:
            base = datetime.fromisoformat(entry.metadata.last_run)
        else:
            base = now - timedelta(days=1)

        cron = croniter(entry.expression, base)
        next_fire = cron.get_next(datetime)

        # Ensure next_fire is timezone-aware.
        if next_fire.tzinfo is None:
            next_fire = next_fire.replace(tzinfo=UTC)

        return next_fire <= now

    def _should_fire_once(self, entry: ScheduleEntry, now: datetime) -> bool:
        if entry.metadata.run_count > 0:
            return False
        if entry.at is None:
            return False
        target = datetime.fromisoformat(entry.at)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return target <= now

    def _on_execution_complete(
        self,
        entry: ScheduleEntry,
        result: Any,
        elapsed: float,
    ) -> None:
        """Update metadata and emit bus event after successful execution."""
        updates = self._build_metadata_update(
            entry,
            last_result="ok",
            run_count_increment=1,
            elapsed=elapsed,
            consecutive_failures=0,
        )

        # Auto-disable once-schedules after successful execution.
        if entry.type == "once":
            updates["enabled"] = False

        try:
            self._store.update(entry.id, updates)
        except KeyError:
            _logger.warning("Schedule %s disappeared during execution", entry.id)

        # Emit bus event so other modules (e.g. Telegram) can deliver results.
        if self._bus is not None:
            content = (getattr(result, "content", None) or str(result)) if result else ""
            self._emit_bus_event(
                "schedule:completed",
                {
                    "schedule_id": entry.id,
                    "schedule_name": entry.prompt[:80],
                    "result": content,
                    "elapsed": elapsed,
                },
            )

    async def _timer_loop(self) -> None:
        """Periodically evaluate all schedules and enqueue those that fire."""
        # Wait until agent_run_fn is bound (via agent:ready event).
        await self._ready.wait()
        interval = self._config.check_interval_seconds
        while self._running:
            try:
                entries = self._store.load()
                for entry in entries:
                    if self.should_fire(entry) and self.is_within_active_hours(entry):
                        await self.enqueue(entry)
                self._timer_consecutive_errors = 0
            except Exception:
                self._timer_consecutive_errors += 1
                _logger.exception(
                    "Error in timer loop (consecutive: %d)",
                    self._timer_consecutive_errors,
                )
                if self._timer_consecutive_errors >= 5:
                    _logger.critical(
                        "Timer loop hit %d consecutive errors, stopping engine",
                        self._timer_consecutive_errors,
                    )
                    self._running = False
                    return
            await asyncio.sleep(interval)

    async def _worker(self) -> None:
        """Sequential execution worker — drains queue one item at a time."""
        while True:
            try:
                entry = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                if not self._running and self._queue.empty():
                    break
                continue
            except asyncio.CancelledError:
                break

            try:
                await self.execute(entry)
            finally:
                self._in_flight.discard(entry.id)
                self._queue.task_done()
