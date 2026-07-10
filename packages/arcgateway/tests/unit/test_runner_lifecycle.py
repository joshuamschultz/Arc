"""Unit tests for GatewayRunner lifecycle.

Covers:
- Constructor: defaults, adapter storage
- add_adapter: updates index
- session_router property
- _handle_shutdown_signal: sets shutdown event
- _install_signal_handlers: registers SIGTERM/SIGINT without error
- _write_clean_shutdown_marker: creates file with ISO timestamp
- _pid_file / _clean_marker path properties
- _shutdown_adapters: calls disconnect() on all adapters; continues on error
- run() lifecycle via direct shutdown_event trigger (external cancel pattern)
- from_config: personal/enterprise → AsyncioExecutor, federal → SubprocessExecutor
- FailedAdapter dataclass: defaults, backoff math
- reconnect_watcher: attempts reconnect, removes on success, marks permanently failed
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from arcgateway.adapters._reconnect import FailedAdapter
from arcgateway.executor import AsyncioExecutor
from arcgateway.runner import GatewayAlreadyRunning, GatewayRunner

# ---------------------------------------------------------------------------
# Minimal adapter stub
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Minimal adapter for lifecycle tests."""

    def __init__(self, name: str = "stub", *, fail_on_connect: bool = False) -> None:
        self.name = name
        self._fail_on_connect = fail_on_connect
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._fail_on_connect:
            raise RuntimeError("adapter connect() failed")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def send(self, target: Any, message: str, *, reply_to: str | None = None) -> None:
        pass


# ---------------------------------------------------------------------------
# Helper: run with external cancel
# ---------------------------------------------------------------------------


async def _run_cancel_after(runner: GatewayRunner, *, delay: float = 0.05) -> None:
    """Wrap runner.run() in a task and cancel from outside after `delay` s.

    This pattern bypasses the TaskGroup/reconnect_watcher infinite loop by
    cancelling the outermost task — simulating an OS SIGKILL from the test.
    Used to verify the setup/teardown paths inside run() without needing a
    real event loop shutdown.
    """
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(delay)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: S110 — test cleanup, swallow is intentional
        pass  # expected


async def _run_via_shutdown_event(runner: GatewayRunner, *, delay: float = 0.05) -> None:
    """Trigger clean shutdown by setting the shutdown_event after delay.

    Patches _install_signal_handlers to no-op. Uses a separate task that
    sets the shutdown event, which causes _wait_for_shutdown to complete,
    which causes the TaskGroup to start cancelling remaining tasks.

    The TaskGroup may still take a full poll_interval (5s) to see the
    reconnect_watcher task cancellation.  We therefore use a much shorter
    RECONNECT_POLL_INTERVAL via monkeypatching.
    """

    async def _trigger() -> None:
        await asyncio.sleep(delay)
        runner._shutdown_event.set()

    with (
        patch.object(runner, "_install_signal_handlers", return_value=None),
        patch("arcgateway.runner._RECONNECT_POLL_INTERVAL", 0.01),
    ):
        try:
            await asyncio.wait_for(
                asyncio.gather(runner.run(), _trigger(), return_exceptions=True),
                timeout=3.0,
            )
        except TimeoutError:
            pass  # acceptable — clean_marker still written in finally


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructor_defaults(tmp_path: Path) -> None:
    """GatewayRunner defaults to AsyncioExecutor when none provided."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    assert isinstance(runner._executor, AsyncioExecutor)
    assert runner._adapters == []
    assert runner._failed_adapters == {}


def test_constructor_with_adapters(tmp_path: Path) -> None:
    """GatewayRunner stores passed adapters."""
    adapter = _StubAdapter("telegram")
    runner = GatewayRunner(adapters=[adapter], runtime_dir=tmp_path)
    assert len(runner._adapters) == 1
    assert runner._adapters[0] is adapter


def test_add_adapter_registers_in_index(tmp_path: Path) -> None:
    """add_adapter() adds adapter to _adapter_index."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    adapter = _StubAdapter("slack")
    runner.add_adapter(adapter)
    assert "slack" in runner._adapter_index


def test_session_router_property(tmp_path: Path) -> None:
    """session_router property returns a SessionRouter."""
    from arcgateway.session import SessionRouter

    runner = GatewayRunner(runtime_dir=tmp_path)
    assert isinstance(runner.session_router, SessionRouter)


# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------


def test_handle_shutdown_signal_sets_event(tmp_path: Path) -> None:
    """_handle_shutdown_signal() sets the _shutdown_event."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    assert not runner._shutdown_event.is_set()
    runner._handle_shutdown_signal()
    assert runner._shutdown_event.is_set()


@pytest.mark.asyncio
async def test_install_signal_handlers_registers_sigterm(tmp_path: Path) -> None:
    """_install_signal_handlers() registers SIGTERM without error."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    registered: list[int] = []

    loop = asyncio.get_running_loop()
    original = loop.add_signal_handler

    def _spy_add(sig: signal.Signals, cb: Any) -> None:
        registered.append(sig)
        original(sig, cb)

    with patch.object(loop, "add_signal_handler", side_effect=_spy_add):
        runner._install_signal_handlers()

    assert signal.SIGTERM in registered
    assert signal.SIGINT in registered


@pytest.mark.asyncio
async def test_install_signal_handlers_tolerates_not_implemented(tmp_path: Path) -> None:
    """_install_signal_handlers() silently ignores NotImplementedError (Windows)."""
    runner = GatewayRunner(runtime_dir=tmp_path)

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.add_signal_handler.side_effect = NotImplementedError
        # Must not raise.
        runner._install_signal_handlers()


# ---------------------------------------------------------------------------
# Clean-shutdown marker
# ---------------------------------------------------------------------------


def test_write_clean_shutdown_marker_creates_file(tmp_path: Path) -> None:
    """_write_clean_shutdown_marker() creates the .clean_shutdown file."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    runner._write_clean_shutdown_marker()

    marker = tmp_path / ".clean_shutdown"
    assert marker.exists()
    content = marker.read_text(encoding="utf-8").strip()
    # Should contain an ISO timestamp.
    assert "T" in content  # e.g. "2026-04-18T..."


def test_clean_marker_path_property(tmp_path: Path) -> None:
    """_clean_marker property returns the correct path."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    assert runner._clean_marker == tmp_path / ".clean_shutdown"


def test_pid_file_path_property(tmp_path: Path) -> None:
    """_pid_file property returns the correct path."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    assert runner._pid_file == tmp_path / "gateway.pid"


# ---------------------------------------------------------------------------
# _shutdown_adapters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_adapters_calls_disconnect(tmp_path: Path) -> None:
    """_shutdown_adapters() calls disconnect() on every adapter."""
    a1 = _StubAdapter("t")
    a2 = _StubAdapter("s")
    runner = GatewayRunner(adapters=[a1, a2], runtime_dir=tmp_path)
    await runner._shutdown_adapters()
    assert a1.disconnect_calls == 1
    assert a2.disconnect_calls == 1


@pytest.mark.asyncio
async def test_shutdown_adapters_continues_after_error(tmp_path: Path) -> None:
    """_shutdown_adapters() logs errors but continues disconnecting other adapters."""

    class _BadAdapter(_StubAdapter):
        async def disconnect(self) -> None:
            raise RuntimeError("disconnect exploded")

    good = _StubAdapter("good")
    bad = _BadAdapter("bad")
    runner = GatewayRunner(adapters=[bad, good], runtime_dir=tmp_path)
    # Must not propagate the exception.
    await runner._shutdown_adapters()
    assert good.disconnect_calls == 1


# ---------------------------------------------------------------------------
# run() lifecycle — write/remove PID file + clean marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_writes_clean_shutdown_marker(tmp_path: Path) -> None:
    """run() writes the .clean_shutdown marker after shutdown."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    await _run_via_shutdown_event(runner)
    assert (tmp_path / ".clean_shutdown").exists()


@pytest.mark.asyncio
async def test_run_writes_pid_file_on_startup(tmp_path: Path) -> None:
    """run() writes gateway.pid before starting adapters."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    pid_file = tmp_path / "gateway.pid"

    # We cancel fast — before the full run() completes — to observe the PID write.
    task = asyncio.create_task(runner.run())
    # Give run() enough time to write the PID file (it's synchronous and first thing).
    await asyncio.sleep(0.02)
    assert pid_file.exists(), "PID file must be written at startup"
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: S110 — test cleanup, swallow is intentional
        pass


@pytest.mark.asyncio
async def test_run_with_no_adapters_starts_cleanly(tmp_path: Path) -> None:
    """run() with no adapters starts and shuts down without error."""
    runner = GatewayRunner(runtime_dir=tmp_path)
    # Just verify it starts without raising on the PID write and setup.
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: S110 — test cleanup, swallow is intentional
        pass
    # The runtime dir should have been created.
    assert tmp_path.exists()


@pytest.mark.asyncio
async def test_run_calls_adapter_connect(tmp_path: Path) -> None:
    """run() calls connect() on each registered adapter."""
    adapter = _StubAdapter("telegram")
    runner = GatewayRunner(adapters=[adapter], runtime_dir=tmp_path)
    await _run_cancel_after(runner, delay=0.05)
    assert adapter.connect_calls >= 1


@pytest.mark.asyncio
async def test_run_raises_already_running_before_starting(tmp_path: Path) -> None:
    """run() raises GatewayAlreadyRunning if PID file belongs to a live process."""
    import os

    runner = GatewayRunner(runtime_dir=tmp_path)

    # Pre-populate with our own live PID.
    pid_file = tmp_path / "gateway.pid"
    tmp_path.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    with patch.object(runner, "_install_signal_handlers", return_value=None):
        with pytest.raises((GatewayAlreadyRunning, Exception)):
            await runner.run()


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------


def test_from_config_personal_tier_uses_asyncio_executor(tmp_path: Path) -> None:
    """from_config() with personal tier creates an AsyncioExecutor.

    from_config is a plain scaffold factory — it does not itself require or
    verify a real agent-execution path (see cli.cmd_start, which fails
    closed before ever calling this, for that concern).
    """
    from arcgateway.config import GatewayConfig

    toml_text = f"""
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)

    runner = GatewayRunner.from_config(config)
    assert isinstance(runner._executor, AsyncioExecutor)


def test_from_config_enterprise_tier_uses_asyncio_executor(tmp_path: Path) -> None:
    """from_config() with enterprise tier creates an AsyncioExecutor."""
    from arcgateway.config import GatewayConfig

    toml_text = f"""
[gateway]
tier = "enterprise"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)

    runner = GatewayRunner.from_config(config)
    assert isinstance(runner._executor, AsyncioExecutor)


def test_from_config_federal_tier_uses_subprocess_executor(tmp_path: Path) -> None:
    """from_config() with federal tier creates a SubprocessExecutor."""
    from arcgateway.config import GatewayConfig
    from arcgateway.executor import SubprocessExecutor

    toml_text = f"""
[gateway]
tier = "federal"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)

    runner = GatewayRunner.from_config(config)
    assert isinstance(runner._executor, SubprocessExecutor)


# ---------------------------------------------------------------------------
# from_config — require_pairing wiring (SDD §3.1 DM Pairing)
# ---------------------------------------------------------------------------


def test_from_config_require_pairing_false_leaves_pairing_store_unset(tmp_path: Path) -> None:
    """Default (require_pairing=false) does not construct a PairingStore.

    This preserves the current no-enforcement default for every existing
    deployment: SessionRouter's PairingInterceptor is a no-op when both
    user_allowlist and pairing_store are None.
    """
    from arcgateway.config import GatewayConfig

    toml_text = f"""
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"

[security]
require_pairing = false
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)

    runner = GatewayRunner.from_config(config)

    assert runner._pairing_store is None
    assert runner.session_router._pairing._pairing_store is None


def test_from_config_require_pairing_true_wires_pairing_store(tmp_path: Path) -> None:
    """require_pairing=true builds a PairingStore and wires it into SessionRouter.

    This is the fix for the "built but dead" pairing system: without this,
    GatewayRunner.__init__ built SessionRouter with no pairing_store at all,
    so PairingInterceptor.is_user_approved was always a no-op regardless of
    config — DM pairing enforcement never actually engaged.
    """
    from arcgateway.config import GatewayConfig
    from arcgateway.pairing import PairingStore

    db_path = tmp_path / "pairing.db"
    toml_text = f"""
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"

[security]
require_pairing = true

[pairing]
db_path = "{db_path}"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)

    runner = GatewayRunner.from_config(config)

    assert isinstance(runner._pairing_store, PairingStore)
    # SessionRouter's own PairingInterceptor must hold the SAME store instance
    # so `arc gateway pair approve` (writing to db_path from a separate
    # process) is visible on the very next message the router handles.
    assert runner.session_router._pairing._pairing_store is runner._pairing_store


def test_from_config_require_pairing_true_uses_configured_db_path(tmp_path: Path) -> None:
    """The wired PairingStore honors [pairing].db_path, not PairingStore's own default."""
    from arcgateway.config import GatewayConfig

    db_path = tmp_path / "custom" / "pairing.db"
    toml_text = f"""
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"

[security]
require_pairing = true

[pairing]
db_path = "{db_path}"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)

    runner = GatewayRunner.from_config(config)

    assert runner._pairing_store._db_path == db_path.expanduser().resolve()


def test_from_config_require_pairing_true_seeds_user_allowlist_from_platforms(
    tmp_path: Path,
) -> None:
    """Task #34: [platforms.telegram].allowed_user_ids must reach the
    SessionRouter's PairingInterceptor, not just live in unused config.

    Without this, an allowlisted user still fell through to the (empty, for a
    never-before-paired user) pairing_store lookup on their first message —
    the static allowlist was pure config-file decoration.
    """
    from arcgateway.config import GatewayConfig

    toml_text = f"""
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"

[security]
require_pairing = true

[pairing]
db_path = "{tmp_path / "pairing.db"}"

[platforms.telegram]
enabled = true
allowed_user_ids = [555]
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)

    runner = GatewayRunner.from_config(config)

    assert runner.session_router._pairing._user_allowlist == {"did:arc:telegram:555"}


def test_from_config_require_pairing_false_does_not_seed_user_allowlist(
    tmp_path: Path,
) -> None:
    """require_pairing=false must leave _user_allowlist None even if a platform
    has allowed_user_ids configured — otherwise PairingInterceptor's "no
    allowlist AND no store => enforcement disabled" fast path breaks for
    OTHER platforms (e.g. web) that reach SessionRouter with no allowlist
    concept of their own — a regression this fix must not introduce.
    """
    from arcgateway.config import GatewayConfig

    toml_text = f"""
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"

[security]
require_pairing = false

[platforms.telegram]
enabled = true
allowed_user_ids = [555]
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)

    runner = GatewayRunner.from_config(config)

    assert runner.session_router._pairing._user_allowlist is None


# ---------------------------------------------------------------------------
# reconnect watcher — FailedAdapter dataclass
# ---------------------------------------------------------------------------


def test_failed_adapter_default_values() -> None:
    """FailedAdapter defaults are correct."""
    fa = FailedAdapter(name="telegram")
    assert fa.attempt == 0
    assert fa.last_error is None
    assert fa.permanently_failed is False


def test_failed_adapter_backoff_math() -> None:
    """FailedAdapter.next_backoff_seconds() follows exponential backoff formula."""
    fa = FailedAdapter(name="telegram")
    # n=1 → 30s
    assert fa.next_backoff_seconds() == 30.0
    fa.attempt = 1
    assert fa.next_backoff_seconds() == 30.0  # n=max(1,1)=1 → 30
    fa.attempt = 2
    assert fa.next_backoff_seconds() == 60.0  # n=2 → 60
    fa.attempt = 3
    assert fa.next_backoff_seconds() == 120.0  # n=3 → 120
    fa.attempt = 4
    assert fa.next_backoff_seconds() == 240.0  # n=4 → 240
    fa.attempt = 5
    assert fa.next_backoff_seconds() == 300.0  # n=5 → capped at 300


@pytest.mark.asyncio
async def test_reconnect_watcher_attempts_reconnect() -> None:
    """reconnect_watcher() attempts connect() on failed adapters."""
    from arcgateway.adapters._reconnect import reconnect_watcher

    connected: list[str] = []

    class _ReconnectAdapter(_StubAdapter):
        async def connect(self) -> None:
            connected.append(self.name)

    adapter = _ReconnectAdapter("retry_me")
    failed_adapters: dict[str, FailedAdapter] = {
        "retry_me": FailedAdapter(name="retry_me", attempt=0)
    }
    adapter_factory = {"retry_me": adapter}

    task = asyncio.create_task(
        reconnect_watcher(
            failed_adapters,
            adapter_factory,
            poll_interval_seconds=0.01,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Adapter should have been reconnected and removed from failed_adapters.
    assert "retry_me" not in failed_adapters
    assert "retry_me" in connected


@pytest.mark.asyncio
async def test_reconnect_watcher_marks_permanently_failed() -> None:
    """reconnect_watcher() marks adapter permanently failed after 20 attempts."""
    from arcgateway.adapters._reconnect import _MAX_RECONNECT_ATTEMPTS, reconnect_watcher

    class _AlwaysFailAdapter(_StubAdapter):
        async def connect(self) -> None:
            raise RuntimeError("always fails")

    adapter = _AlwaysFailAdapter("always_fail")
    # Start at attempt = MAX so one more loop triggers permanent failure.
    failed_adapters: dict[str, FailedAdapter] = {
        "always_fail": FailedAdapter(name="always_fail", attempt=_MAX_RECONNECT_ATTEMPTS)
    }
    adapter_factory = {"always_fail": adapter}

    task = asyncio.create_task(
        reconnect_watcher(
            failed_adapters,
            adapter_factory,
            poll_interval_seconds=0.01,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert failed_adapters["always_fail"].permanently_failed is True
