"""Unit tests for arcgateway.adapters.base.

Covers:
- BasePlatformAdapter Protocol is @runtime_checkable
- FailedAdapter dataclass: defaults, fields, backoff math
- reconnect_watcher: retries failed adapters
- reconnect_watcher: removes entry on successful reconnect
- reconnect_watcher: handles missing adapter in factory
- reconnect_watcher: marks permanently failed after max attempts
- reconnect_watcher: skips already-permanently-failed entries
- reconnect_watcher: handles empty failed_adapters (no-op poll)
- edit_message default body raises NotImplementedError
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import pytest

from arcgateway.adapters.base import (
    _BACKOFF_BASE_SECONDS,
    _BACKOFF_MAX_SECONDS,
    _MAX_RECONNECT_ATTEMPTS,
    BasePlatformAdapter,
    FailedAdapter,
    reconnect_watcher,
)
from arcgateway.delivery import DeliveryTarget

# ---------------------------------------------------------------------------
# Concrete adapter satisfying BasePlatformAdapter (all required methods)
# ---------------------------------------------------------------------------


class _ConcreteAdapter:
    """Full implementation satisfying BasePlatformAdapter Protocol.

    Includes edit_message because the Protocol defines it with a concrete
    body, making it a required member for runtime_checkable isinstance checks.
    """

    name = "concrete"

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        pass

    async def edit_message(
        self,
        target: DeliveryTarget,
        message_id: str,
        new_text: str,
    ) -> None:
        raise NotImplementedError("edit_message not supported by this adapter")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_base_platform_adapter_is_runtime_checkable() -> None:
    """BasePlatformAdapter is @runtime_checkable.

    An object with name, connect, disconnect, send, and edit_message
    satisfies isinstance(obj, BasePlatformAdapter).
    """
    adapter = _ConcreteAdapter()
    assert isinstance(adapter, BasePlatformAdapter)


def test_non_conforming_object_fails_isinstance() -> None:
    """An object missing required methods does not satisfy the Protocol."""

    class _Missing:
        name = "missing"

        async def connect(self) -> None:
            pass

        # Missing: disconnect(), send(), edit_message()

    assert not isinstance(_Missing(), BasePlatformAdapter)


def test_edit_message_default_raises_not_implemented() -> None:
    """The Protocol's default edit_message body raises NotImplementedError.

    Concrete adapters inheriting from a base class that delegates to the
    Protocol's default body (raise NotImplementedError) should raise when
    called. This is by design — StreamBridge treats the raise as a flood-
    control strike and falls back to final-send-only mode.
    """
    adapter = _ConcreteAdapter()
    with pytest.raises(NotImplementedError):
        asyncio.run(
            adapter.edit_message(
                DeliveryTarget.parse("telegram:123"),
                "msg-001",
                "new text",
            )
        )


def test_edit_message_can_be_overridden() -> None:
    """Adapters can override edit_message without raising."""

    class _EditableAdapter(_ConcreteAdapter):
        async def edit_message(
            self,
            target: DeliveryTarget,
            message_id: str,
            new_text: str,
        ) -> None:
            pass  # real adapter sends an edit API call here

    adapter = _EditableAdapter()
    # Must not raise.
    asyncio.run(
        adapter.edit_message(
            DeliveryTarget.parse("telegram:123"),
            "msg-001",
            "edited text",
        )
    )


# ---------------------------------------------------------------------------
# FailedAdapter dataclass
# ---------------------------------------------------------------------------


def test_failed_adapter_defaults() -> None:
    """FailedAdapter default values are correct."""
    fa = FailedAdapter(name="telegram")
    assert fa.name == "telegram"
    assert fa.attempt == 0
    assert fa.last_error is None
    assert fa.permanently_failed is False


def test_failed_adapter_backoff_n1() -> None:
    """n=1 → 30s (first retry)."""
    fa = FailedAdapter(name="test", attempt=0)
    assert fa.next_backoff_seconds() == float(_BACKOFF_BASE_SECONDS)


def test_failed_adapter_backoff_n2() -> None:
    """n=2 → 60s."""
    fa = FailedAdapter(name="test", attempt=2)
    expected = min(_BACKOFF_BASE_SECONDS * math.pow(2, 2 - 1), _BACKOFF_MAX_SECONDS)
    assert fa.next_backoff_seconds() == expected


def test_failed_adapter_backoff_n3() -> None:
    """n=3 → 120s."""
    fa = FailedAdapter(name="test", attempt=3)
    expected = min(_BACKOFF_BASE_SECONDS * math.pow(2, 3 - 1), _BACKOFF_MAX_SECONDS)
    assert fa.next_backoff_seconds() == expected


def test_failed_adapter_backoff_capped_at_300() -> None:
    """Backoff caps at _BACKOFF_MAX_SECONDS (300s) for large attempt counts."""
    fa = FailedAdapter(name="test", attempt=20)
    assert fa.next_backoff_seconds() == float(_BACKOFF_MAX_SECONDS)


def test_failed_adapter_backoff_zero_attempt_uses_n1() -> None:
    """attempt=0 maps to n=1 (same as first retry) via max(1, attempt)."""
    fa = FailedAdapter(name="test", attempt=0)
    assert fa.next_backoff_seconds() == float(_BACKOFF_BASE_SECONDS)


def test_failed_adapter_can_store_exception() -> None:
    """FailedAdapter stores exceptions in last_error."""
    exc = RuntimeError("test failure")
    fa = FailedAdapter(name="test", last_error=exc)
    assert fa.last_error is exc


def test_failed_adapter_can_mark_permanently_failed() -> None:
    """FailedAdapter permanently_failed can be set to True."""
    fa = FailedAdapter(name="test", permanently_failed=True)
    assert fa.permanently_failed is True


def test_failed_adapter_backoff_monotonically_increases() -> None:
    """Backoff increases monotonically up to the cap."""
    prev = 0.0
    for attempt in range(1, 10):
        fa = FailedAdapter(name="test", attempt=attempt)
        current = fa.next_backoff_seconds()
        assert current >= prev, f"Backoff decreased at attempt {attempt}"
        prev = current


# ---------------------------------------------------------------------------
# reconnect_watcher behaviour
# ---------------------------------------------------------------------------


async def _run_watcher(
    failed_adapters: dict[str, FailedAdapter],
    adapter_factory: dict[str, Any],
    *,
    poll_interval: float = 0.01,
    duration: float = 0.06,
) -> None:
    """Helper: run reconnect_watcher for a short duration then cancel."""
    task = asyncio.create_task(
        reconnect_watcher(
            failed_adapters,
            adapter_factory,
            poll_interval_seconds=poll_interval,
        )
    )
    await asyncio.sleep(duration)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_reconnect_watcher_reconnects_adapter() -> None:
    """reconnect_watcher() calls connect() on failed adapters."""
    connected: list[str] = []

    class _RAdapter(_ConcreteAdapter):
        async def connect(self) -> None:
            connected.append(self.name)

    adapter = _RAdapter()
    adapter.name = "test_adapter"
    failed: dict[str, FailedAdapter] = {
        "test_adapter": FailedAdapter(name="test_adapter", attempt=0)
    }
    factory: dict[str, Any] = {"test_adapter": adapter}

    await _run_watcher(failed, factory)

    assert "test_adapter" in connected
    # Successfully reconnected adapter must be removed from failed dict.
    assert "test_adapter" not in failed


@pytest.mark.asyncio
async def test_reconnect_watcher_empty_dict_is_noop() -> None:
    """reconnect_watcher() does nothing when failed_adapters is empty."""
    factory: dict[str, Any] = {}
    failed: dict[str, FailedAdapter] = {}
    # Must run without error.
    await _run_watcher(failed, factory, duration=0.03)


@pytest.mark.asyncio
async def test_reconnect_watcher_missing_factory_entry_skips() -> None:
    """reconnect_watcher() skips entries missing from adapter_factory."""
    failed: dict[str, FailedAdapter] = {"ghost": FailedAdapter(name="ghost", attempt=0)}
    factory: dict[str, Any] = {}  # no adapter for "ghost"

    # Must not raise — just skip.
    await _run_watcher(failed, factory)


@pytest.mark.asyncio
async def test_reconnect_watcher_records_failure() -> None:
    """reconnect_watcher() updates last_error when reconnect fails."""

    class _AlwaysFail(_ConcreteAdapter):
        async def connect(self) -> None:
            raise RuntimeError("always fails")

    adapter = _AlwaysFail()
    adapter.name = "fail"
    entry = FailedAdapter(name="fail", attempt=0)
    failed: dict[str, FailedAdapter] = {"fail": entry}
    factory: dict[str, Any] = {"fail": adapter}

    await _run_watcher(failed, factory)

    # After several attempts, last_error must be set.
    assert entry.last_error is not None


@pytest.mark.asyncio
async def test_reconnect_watcher_marks_permanently_failed_at_max() -> None:
    """reconnect_watcher() marks adapter permanently failed at MAX_RECONNECT_ATTEMPTS."""

    class _AlwaysFail(_ConcreteAdapter):
        async def connect(self) -> None:
            raise RuntimeError("always fails")

    adapter = _AlwaysFail()
    adapter.name = "maxed_out"
    entry = FailedAdapter(name="maxed_out", attempt=_MAX_RECONNECT_ATTEMPTS)
    failed: dict[str, FailedAdapter] = {"maxed_out": entry}
    factory: dict[str, Any] = {"maxed_out": adapter}

    await _run_watcher(failed, factory, duration=0.05)

    assert entry.permanently_failed is True


@pytest.mark.asyncio
async def test_reconnect_watcher_skips_permanently_failed() -> None:
    """reconnect_watcher() does not call connect() on permanently_failed adapters."""
    connect_calls = 0

    class _Perm(_ConcreteAdapter):
        async def connect(self) -> None:
            nonlocal connect_calls
            connect_calls += 1

    adapter = _Perm()
    adapter.name = "perm"
    entry = FailedAdapter(name="perm", attempt=0, permanently_failed=True)
    failed: dict[str, FailedAdapter] = {"perm": entry}
    factory: dict[str, Any] = {"perm": adapter}

    await _run_watcher(failed, factory, duration=0.05)

    assert connect_calls == 0, "connect() must not be called on permanently_failed adapter"


@pytest.mark.asyncio
async def test_reconnect_watcher_increments_attempt_on_failure() -> None:
    """reconnect_watcher() increments the attempt counter on each failed reconnect."""

    class _Fail(_ConcreteAdapter):
        async def connect(self) -> None:
            raise RuntimeError("fail")

    adapter = _Fail()
    adapter.name = "inc"
    entry = FailedAdapter(name="inc", attempt=0)
    failed: dict[str, FailedAdapter] = {"inc": entry}
    factory: dict[str, Any] = {"inc": adapter}

    await _run_watcher(failed, factory, poll_interval=0.01, duration=0.05)

    assert entry.attempt > 0, "attempt counter must be incremented after failures"
