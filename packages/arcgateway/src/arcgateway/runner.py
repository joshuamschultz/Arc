"""GatewayRunner — long-running asyncio daemon that supervises platform adapters.

Design (SDD §3.1 Process Model):
    GatewayRunner is a single asyncio daemon process. It:
    1. Starts all configured platform adapters inside an asyncio.TaskGroup
       so a crash in one adapter never kills sibling adapters (ASI08 — cascading
       failure containment).
    2. Starts a reconnect watcher task to handle adapter failures with backoff.
    3. Wires each adapter's inbound events to the SessionRouter.
    4. Handles OS signals (SIGINT, SIGTERM) for clean shutdown.
    5. Writes a `.clean_shutdown` marker file on graceful exit (Hermes pattern)
       so operators can distinguish clean stop from crash restart.

T1.13 addition (SPEC-018 §3.4):
    GatewayRunner constructs a ``DeliverySenderImpl`` and exposes it via the
    ``delivery_sender`` property so callers can inject it into the scheduler's
    ``SchedulerEngine.set_cron_runner()`` without the scheduler importing
    arcgateway.

    Adapters are registered with the sender via ``_register_adapters_for_delivery``
    during startup.  Any adapter added before ``run()`` is called automatically
    appears in the sender's routing table.

Clean-shutdown marker:
    On receipt of SIGTERM/SIGINT, GatewayRunner cancels all tasks, waits for
    them to finish, then writes `<runtime_dir>/.clean_shutdown` with the
    stop timestamp. Supervisors (systemd, K8s liveness probes) can check for
    this file to distinguish clean stop from crash.

Usage::

    runner = GatewayRunner(
        adapters=[telegram_adapter, slack_adapter],
        executor=AsyncioExecutor(),
        runtime_dir=Path("/var/run/arcgateway"),
    )
    asyncio.run(runner.run())
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from arcgateway.adapters.base import BasePlatformAdapter, FailedAdapter, reconnect_watcher
from arcgateway.delivery import DeliverySenderImpl
from arcgateway.executor import AsyncioExecutor, Executor, SubprocessExecutor
from arcgateway.session import SessionRouter

if TYPE_CHECKING:
    from arcgateway.config import GatewayConfig

_logger = logging.getLogger("arcgateway.runner")

# Default runtime directory for the clean-shutdown marker file
_DEFAULT_RUNTIME_DIR = Path.home() / ".arc" / "gateway" / "run"

# Reconnect watcher poll interval (seconds)
_RECONNECT_POLL_INTERVAL = 5.0


class GatewayRunner:
    """Supervises platform adapters and routes messages to the SessionRouter.

    Attributes:
        _adapters: List of configured platform adapters.
        _executor: Executor implementation for running agent turns.
        _runtime_dir: Directory for the clean-shutdown marker file.
        _session_router: Routes inbound events to per-session agent tasks.
        _failed_adapters: Tracks adapters that failed and need reconnection.
        _shutdown_event: Set on SIGINT/SIGTERM to trigger clean shutdown.
        _delivery_sender: Satisfies DeliverySender Protocol for cron delivery.
    """

    def __init__(
        self,
        adapters: list[BasePlatformAdapter] | None = None,
        executor: Executor | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        """Initialise GatewayRunner.

        Args:
            adapters: Platform adapter instances to supervise. Defaults to [].
            executor: Executor for running agent turns. Defaults to AsyncioExecutor.
            runtime_dir: Directory for the clean-shutdown marker file.
                Defaults to ~/.arc/gateway/run.
        """
        self._adapters: list[BasePlatformAdapter] = adapters or []
        self._executor: Executor = executor or AsyncioExecutor()
        self._runtime_dir: Path = runtime_dir or _DEFAULT_RUNTIME_DIR
        self._session_router = SessionRouter(executor=self._executor)
        self._failed_adapters: dict[str, FailedAdapter] = {}
        self._adapter_index: dict[str, BasePlatformAdapter] = {
            a.name: a for a in self._adapters
        }
        self._shutdown_event = asyncio.Event()

        # T1.13 — DeliverySenderImpl satisfies arcagent's DeliverySender Protocol.
        # Populated with adapters in _register_adapters_for_delivery().
        self._delivery_sender = DeliverySenderImpl()
        self._register_adapters_for_delivery()

    @property
    def delivery_sender(self) -> DeliverySenderImpl:
        """Expose DeliverySenderImpl for injection into the scheduler.

        Usage (at gateway startup, after add_adapter calls)::

            engine.set_cron_runner(
                cron_runner=CronRunner(telemetry),
                agent_factory=make_agent_factory(agent),
                delivery_sender=runner.delivery_sender,
            )

        Returns:
            The DeliverySenderImpl instance wired with all registered adapters.
        """
        return self._delivery_sender

    @classmethod
    def from_config(cls, config: GatewayConfig) -> GatewayRunner:
        """Build a GatewayRunner from a GatewayConfig.

        Selects the executor based on the configured security tier:
          personal / enterprise → AsyncioExecutor (in-process, shared event loop)
          federal              → SubprocessExecutor (OS-level isolation, resource limits)

        Platform adapters are NOT instantiated here because they require
        credentials that may only be available after vault resolution.
        Callers should add adapters via add_adapter() before calling run().

        Args:
            config: Parsed GatewayConfig from gateway.toml.

        Returns:
            Configured GatewayRunner instance.
        """
        # Lazy import keeps config.py optional (doesn't exist until M1 wiring)
        from arcgateway.config import GatewayConfig  # noqa: F401 (type-only use above)

        tier = config.gateway.tier
        if tier == "federal":
            import sys
            executor: Executor = SubprocessExecutor(
                worker_cmd=[sys.executable, "-m", "arccli.agent_worker"],
            )
            _logger.info("GatewayRunner.from_config: federal tier → SubprocessExecutor")
        else:
            executor = AsyncioExecutor()
            _logger.info(
                "GatewayRunner.from_config: %s tier → AsyncioExecutor",
                tier,
            )

        return cls(
            adapters=[],
            executor=executor,
            runtime_dir=config.gateway.runtime_dir,
        )

    def add_adapter(self, adapter: BasePlatformAdapter) -> None:
        """Register a platform adapter before run() is called.

        Also registers the adapter with DeliverySenderImpl so cron job
        output can be routed to this platform.

        Args:
            adapter: Adapter instance to add.
        """
        self._adapters.append(adapter)
        self._adapter_index[adapter.name] = adapter
        # Register with delivery sender for outbound routing.
        self._delivery_sender.register_adapter(adapter.name, adapter)

    async def run(self) -> None:
        """Start the gateway daemon.

        Runs until SIGINT or SIGTERM is received. On shutdown:
        1. Cancels all adapter tasks and the reconnect watcher.
        2. Waits for all tasks to finish.
        3. Writes the .clean_shutdown marker file.

        This is the main entry point — call via asyncio.run(runner.run()).
        """
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._clean_marker.unlink(missing_ok=True)  # Remove stale marker from prior run

        _logger.info(
            "GatewayRunner starting: %d adapter(s) configured",
            len(self._adapters),
        )

        self._install_signal_handlers()

        try:
            async with asyncio.TaskGroup() as tg:
                # One task per adapter — crash in one does NOT kill others.
                for adapter in self._adapters:
                    tg.create_task(
                        self._run_adapter(adapter),
                        name=f"adapter:{adapter.name}",
                    )

                # Reconnect watcher runs alongside adapters
                tg.create_task(
                    reconnect_watcher(
                        self._failed_adapters,
                        self._adapter_index,
                        poll_interval_seconds=_RECONNECT_POLL_INTERVAL,
                    ),
                    name="reconnect_watcher",
                )

                # Shutdown gate — waits for SIGINT/SIGTERM
                tg.create_task(self._wait_for_shutdown(), name="shutdown_gate")

        except* Exception as eg:
            # Log each exception from the TaskGroup; don't re-raise during shutdown
            for exc in eg.exceptions:
                _logger.exception("Task error during gateway run: %s", exc)
        finally:
            await self._shutdown_adapters()
            self._write_clean_shutdown_marker()

    async def _run_adapter(self, adapter: BasePlatformAdapter) -> None:
        """Connect and supervise a single platform adapter.

        Marks the adapter as failed (for the reconnect watcher) if it
        raises an unhandled exception, rather than letting the exception
        propagate to the TaskGroup (which would cancel sibling adapters).

        Args:
            adapter: Adapter to run.
        """
        _logger.info("Starting adapter: %s", adapter.name)
        try:
            await adapter.connect()
            _logger.info("Adapter %s connected", adapter.name)
            # Block until the adapter's task is cancelled (shutdown) or fails
            await asyncio.sleep(float("inf"))
        except asyncio.CancelledError:
            _logger.info("Adapter %s cancelled (shutdown)", adapter.name)
            raise  # CancelledError must propagate to TaskGroup for clean exit
        except Exception as exc:
            _logger.exception("Adapter %s failed: %s", adapter.name, exc)
            self._failed_adapters[adapter.name] = FailedAdapter(
                name=adapter.name, last_error=exc
            )
            # TODO (M1 integration): emit gateway.adapter.fail audit event

    async def _wait_for_shutdown(self) -> None:
        """Wait until the shutdown event is set (SIGINT or SIGTERM received)."""
        await self._shutdown_event.wait()
        _logger.info("GatewayRunner: shutdown signal received, stopping...")
        # Cancel all other tasks by raising CancelledError in them
        # TaskGroup will cancel remaining tasks when this task returns

    async def _shutdown_adapters(self) -> None:
        """Gracefully disconnect all adapters.

        Called in the finally block of run() to ensure adapters clean up
        their platform connections on any exit path.
        """
        for adapter in self._adapters:
            try:
                await adapter.disconnect()
            except Exception:
                _logger.exception("Error disconnecting adapter %s", adapter.name)

    def _install_signal_handlers(self) -> None:
        """Register SIGINT and SIGTERM handlers.

        Uses the running event loop's add_signal_handler for async-safe
        signal handling. Does not use synchronous signal.signal() as that
        is not safe in asyncio applications.

        Note: signal handlers are not available on Windows. We guard
        with a try/except to allow development on non-POSIX platforms.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_shutdown_signal)
            except (NotImplementedError, OSError):
                # Not available on Windows or in some test environments
                _logger.debug("Signal handler for %s not available on this platform.", sig.name)

    def _handle_shutdown_signal(self) -> None:
        """Asyncio-safe shutdown trigger called from OS signal handler.

        Sets the shutdown event, which wakes the _wait_for_shutdown task.
        """
        _logger.info("GatewayRunner: received shutdown signal")
        self._shutdown_event.set()

    def _write_clean_shutdown_marker(self) -> None:
        """Write .clean_shutdown marker file (Hermes pattern).

        Supervisors check for this file to distinguish clean stop from crash.
        The file contains an ISO 8601 UTC timestamp.
        """
        try:
            marker = self._clean_marker
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                datetime.now(tz=UTC).isoformat() + "\n",
                encoding="utf-8",
            )
            _logger.info("GatewayRunner: wrote clean-shutdown marker at %s", marker)
        except OSError:
            _logger.warning("GatewayRunner: failed to write clean-shutdown marker", exc_info=True)

    def _register_adapters_for_delivery(self) -> None:
        """Register already-added adapters with the DeliverySenderImpl.

        Called once at construction so adapters passed via the constructor
        are immediately available for cron job delivery routing.
        """
        for adapter in self._adapters:
            self._delivery_sender.register_adapter(adapter.name, adapter)

    @property
    def _clean_marker(self) -> Path:
        """Path to the .clean_shutdown marker file."""
        return self._runtime_dir / ".clean_shutdown"

    @property
    def session_router(self) -> SessionRouter:
        """Expose SessionRouter for external wiring (e.g. adapter callbacks)."""
        return self._session_router
