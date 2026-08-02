"""Scheduler engine — timer loop + execution queue — SPEC-002.

Evaluates cron, interval, and one-time schedules. Executes via
agent_run_fn callback with timeout enforcement and circuit breaker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from croniter import croniter

from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.scheduler.config import SchedulerConfig
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.store import ScheduleStore

if TYPE_CHECKING:
    from arcagent.core.module_bus import ModuleBus

#: How often to repeat the "nothing can fire" warning while unready.
_UNREADY_WARN_SECONDS = 60.0

_logger = logging.getLogger("arcagent.scheduler")

AgentRunFn = Callable[..., Awaitable[Any]]

# Sends a schedule's final output to a channel target string ("telegram:123").
# Injected by the embedded gateway (which owns channels); None standalone.
ChannelDeliverFn = Callable[[str, str], Awaitable[None]]


def _result_text(result: Any) -> str:
    """Extract deliverable text from a run result (``.content`` or ``str``)."""
    if not result:
        return ""
    return str(getattr(result, "content", None) or result)


class SchedulerEngine:
    """Core scheduling engine with timer loop and sequential execution queue."""

    def __init__(
        self,
        store: ScheduleStore,
        config: SchedulerConfig,
        telemetry: AgentTelemetry,
        # ``None`` until the agent binds one. Not a placeholder callable: a
        # noop that "succeeds" marks a reminder run and disables it, losing the
        # reminder silently. Absent is a state the engine reports.
        agent_run_fn: AgentRunFn | None,
        bus: ModuleBus | None = None,
        channel_deliver_fn: ChannelDeliverFn | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._telemetry = telemetry
        self._agent_run_fn: AgentRunFn | None = agent_run_fn
        self._bus = bus
        self._channel_deliver_fn = channel_deliver_fn

        self._in_flight: set[str] = set()
        self._fire_and_forget: set[asyncio.Task[Any]] = set()
        self._timer_task: asyncio.Task[None] | None = None
        self._running = False
        self._timer_consecutive_errors = 0
        self._unready_ticks = 0
        # Which agent this engine belongs to. A fleet runs one engine per
        # agent, so an unlabelled warning names a problem nobody can locate.
        self.label: str = ""
        # Asked for a callback when one is needed and none is bound. This is the
        # whole reason a binding can no longer be missed: the two halves of it
        # do not have to meet in the same asyncio task at the same moment.
        self.run_fn_resolver: Callable[[], AgentRunFn | None] | None = None
        # Test seam: the configured interval is whole seconds, which makes a
        # loop test take whole seconds. Overridden only by tests.
        self._tick_seconds: float = 0.0

    @property
    def running(self) -> bool:
        return self._running

    def set_agent_run_fn(self, fn: AgentRunFn) -> None:
        """Bind or rebind the agent.run() callback.

        Binding is no longer a handshake the loop waits on. The loop asks for a
        callback each time something is due, so a callback bound before the
        engine exists, after it starts, or from a different task all work the
        same — the previous design blocked forever on any of those and said
        nothing.
        """
        self._agent_run_fn = fn

    def set_channel_deliver_fn(self, fn: ChannelDeliverFn | None) -> None:
        """Bind the channel-delivery callback (embedded gateway supplies it)."""
        self._channel_deliver_fn = fn

    # --- Public API ---

    async def start(self) -> None:
        """Start the one loop this engine has."""
        self._running = True
        self._timer_task = asyncio.create_task(self._timer_loop())
        _logger.info("Scheduler engine started")

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop the loop. Nothing to drain — a firing runs inline."""
        del timeout
        self._running = False
        if self._timer_task is not None:
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
        self._in_flight.clear()
        _logger.info("Scheduler engine stopped")

    async def execute(self, entry: ScheduleEntry) -> Any:
        """Execute a single schedule entry via agent_run_fn.

        Handles timeout and updates metadata on success or failure.
        """
        start_time = time.monotonic()
        timeout = entry.timeout_seconds

        try:
            result = await asyncio.wait_for(self._dispatch(entry), timeout=timeout)
            elapsed = time.monotonic() - start_time
            self._on_execution_complete(entry, result, elapsed)
            await self._deliver_to_channel(entry, result)
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
        except Exception as exc:  # reason: fail-open — log + continue
            elapsed = time.monotonic() - start_time
            _logger.error(
                "Schedule %s failed after %.1fs: %s",
                entry.id,
                elapsed,
                exc,
            )
            self.on_execution_failed(entry, exc)
            return None

    async def _dispatch(self, entry: ScheduleEntry) -> Any:
        """Perform the entry's declared action (SPEC-061 COMP-017).

        ``workflow_run`` calls the workflows module's run entry DIRECTLY — the
        decision to start a named workflow is made by the schedule, never by a
        model reading free text and choosing a tool. The import is lazy so the
        scheduler still works on a deployment with no workflows module.

        Everything around this branch is unchanged and action-agnostic: the
        active-hours gate, the ``_in_flight`` dedup (which is why an overlapping
        firing SKIPS), the timeout, and the consecutive-failure circuit breaker
        all apply identically to both actions.
        """
        if entry.action == "workflow_run":
            from arcagent.modules.workflows.run_entry import start_workflow_run

            return await start_workflow_run(str(entry.workflow_id), entry.workflow_input)
        run_fn = self._agent_run_fn
        if run_fn is None:
            # The tick refuses to run a due entry without a callback, so this is
            # only reachable by calling execute() directly. Refuse loudly rather
            # than record a firing that never happened.
            raise RuntimeError("no agent run callback is bound")
        return await run_fn(entry.prompt, session_key=f"scheduler:{entry.id}")

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
                    "schedule_name": entry.label,
                    "error": str(error),
                    "consecutive_failures": new_failures,
                },
            )

        # Return updated entry for caller.
        data = entry.model_dump()
        data.update(updates)
        return ScheduleEntry(**data)

    # --- Private ---

    async def _deliver_to_channel(self, entry: ScheduleEntry, result: Any) -> None:
        """Send the run's output to ``entry.deliver_to`` if delivery is wired.

        Fail-open: a channel send error is logged but never propagates — a
        delivery failure must not fail the schedule execution or trip the
        circuit breaker (the run itself already succeeded).
        """
        if not entry.deliver_to or self._channel_deliver_fn is None:
            return
        text = _result_text(result)
        if not text:
            return
        try:
            await self._channel_deliver_fn(entry.deliver_to, text)
        except Exception:  # reason: fail-open — delivery must not fail the run
            _logger.exception(
                "Schedule %s: channel delivery to %s failed",
                entry.id,
                entry.deliver_to,
            )

    def _emit_bus_event(self, event: str, data: dict[str, Any]) -> None:
        """Fire-and-forget bus event emission with proper task reference tracking.

        Must only be called after a ``self._bus is not None`` guard.
        """
        if self._bus is None:
            raise RuntimeError("_emit_bus_event called without bus")
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

    def _resolve_tz(self) -> tzinfo:
        """The timezone cron/once schedules are evaluated in.

        A configured IANA name ("America/Chicago") is DST-aware; empty keeps the
        original UTC behaviour so "0 8 * * *" without a configured zone is
        unchanged. Set the zone to fire "8am" at 8am local, not 8am UTC.
        """
        name = getattr(self._config, "timezone", "") or ""
        return ZoneInfo(name) if name else UTC

    def _should_fire_cron(self, entry: ScheduleEntry, now: datetime) -> bool:
        if entry.expression is None:
            return False

        tz = self._resolve_tz()
        now_local = now.astimezone(tz)
        if entry.metadata.last_run:
            base = datetime.fromisoformat(entry.metadata.last_run).astimezone(tz)
        else:
            base = now_local - timedelta(days=1)

        cron = croniter(entry.expression, base)
        next_fire = cron.get_next(datetime)

        # croniter yields a naive datetime when the base is naive; anchor it to
        # the evaluation zone so the comparison below is apples-to-apples.
        if next_fire.tzinfo is None:
            next_fire = next_fire.replace(tzinfo=tz)

        return bool(next_fire <= now_local)

    def _should_fire_once(self, entry: ScheduleEntry, now: datetime) -> bool:
        if entry.metadata.run_count > 0:
            return False
        if entry.at is None:
            return False
        target = datetime.fromisoformat(entry.at)
        # A naive "at" is interpreted in the configured zone (a user typing
        # "2026-07-15T08:00" means 8am local), then compared in UTC.
        if target.tzinfo is None:
            target = target.replace(tzinfo=self._resolve_tz())
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
            content = _result_text(result)
            self._emit_bus_event(
                "schedule:completed",
                {
                    "schedule_id": entry.id,
                    "schedule_name": entry.label,
                    "result": content,
                    "elapsed": elapsed,
                },
            )

    async def _timer_loop(self) -> None:
        """The whole engine: every tick, run what is due.

        No queue, no worker, no readiness handshake. Those existed to decouple
        deciding from running, and what they actually produced was a loop that
        could park forever on an event nobody set, with a stored reminder that
        looked pending and could never fire. A tick that finds due work runs it
        inline, one at a time, and asks for the agent callback at that moment —
        so a callback bound late, early, or from another task all work.
        """
        interval = self._tick_seconds or self._config.check_interval_seconds
        while self._running:
            try:
                await self._tick()
                self._timer_consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception:  # reason: one bad tick must not stop the engine
                self._timer_consecutive_errors += 1
                _logger.exception(
                    "Error in scheduler tick (consecutive: %d)",
                    self._timer_consecutive_errors,
                )
                if self._timer_consecutive_errors >= 5:
                    _logger.critical(
                        "Scheduler stopping after %d consecutive tick errors",
                        self._timer_consecutive_errors,
                    )
                    self._running = False
                    return
            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        """Run every schedule that is due, sequentially."""
        due = [
            entry
            for entry in self._store.load()
            if self.should_fire(entry)
            and self.is_within_active_hours(entry)
            and entry.id not in self._in_flight
        ]
        if not due:
            self._unready_ticks = 0
            return
        if self._agent_run_fn is None:
            self._agent_run_fn = self._resolve_run_fn()
        if self._agent_run_fn is None:
            self._warn_unready(len(due))
            return
        self._unready_ticks = 0
        for entry in due:
            self._in_flight.add(entry.id)
            try:
                await self.execute(entry)
            finally:
                self._in_flight.discard(entry.id)

    def _resolve_run_fn(self) -> AgentRunFn | None:
        """Late-bound callback lookup, or None if nothing has bound one yet."""
        if self.run_fn_resolver is None:
            return None
        try:
            return self.run_fn_resolver()
        except Exception:  # reason: a lookup must never break the tick
            _logger.warning("Scheduler run-callback lookup failed", exc_info=True)
            return None

    def _warn_unready(self, pending: int) -> None:
        """Say, repeatedly, that due work cannot run — and consume nothing.

        A schedule that has not fired is still pending, never spent: the row is
        left exactly as it was, so it fires the moment a callback exists.
        """
        self._unready_ticks += 1
        interval = self._tick_seconds or self._config.check_interval_seconds
        every = max(1, int(_UNREADY_WARN_SECONDS / max(interval, 0.001)))
        if self._unready_ticks % every != 1 % every:
            return
        _logger.warning(
            "Scheduler for %s has %d schedule(s) due but no agent run callback — "
            "nothing will fire until one is bound",
            self.label or "an unnamed agent",
            pending,
        )
