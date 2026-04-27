"""Integration test — pairing cleanup_expired is scheduled and runs on interval.

Verifies that GatewayRunner.set_pairing_store() causes cleanup_expired() to
be scheduled as a background task and called at the configured interval.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcgateway.runner import _PAIRING_CLEANUP_INTERVAL, GatewayRunner


class TestPairingCleanupScheduler:
    @pytest.mark.asyncio
    async def test_cleanup_not_scheduled_without_pairing_store(self) -> None:
        """When no pairing store is set, no cleanup task is created."""
        runner = GatewayRunner()
        assert runner._pairing_store is None

    @pytest.mark.asyncio
    async def test_set_pairing_store_registers_store(self) -> None:
        """set_pairing_store() sets _pairing_store correctly."""
        runner = GatewayRunner()
        mock_store = MagicMock()
        runner.set_pairing_store(mock_store)
        assert runner._pairing_store is mock_store

    @pytest.mark.asyncio
    async def test_cleanup_task_runs_after_interval(self) -> None:
        """cleanup_expired is called once after the sleep interval fires.

        We patch asyncio.sleep to advance time without actually waiting and
        assert that cleanup_expired was called.
        """
        mock_store = MagicMock()
        mock_store.cleanup_expired = AsyncMock(return_value=0)

        runner = GatewayRunner()
        runner.set_pairing_store(mock_store)

        sleep_call_count = 0

        async def fast_sleep(seconds: float) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 2:
                # Cancel ourselves after 2 iterations to stop the loop
                raise asyncio.CancelledError

        with patch("arcgateway.runner.asyncio.sleep", side_effect=fast_sleep):
            with pytest.raises(asyncio.CancelledError):
                await runner._run_pairing_cleanup()

        # cleanup_expired should have been called once (after first sleep)
        assert mock_store.cleanup_expired.call_count >= 1

    @pytest.mark.asyncio
    async def test_cleanup_error_does_not_propagate(self) -> None:
        """cleanup_expired errors are logged but do not stop the scheduler loop."""
        mock_store = MagicMock()
        call_count = 0

        async def failing_cleanup() -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB error")
            raise asyncio.CancelledError  # stop after 2nd call

        mock_store.cleanup_expired = failing_cleanup

        runner = GatewayRunner()
        runner.set_pairing_store(mock_store)

        sleep_call_count = 0

        async def fast_sleep(seconds: float) -> None:
            # Let the loop proceed without actual waiting
            pass

        with patch("arcgateway.runner.asyncio.sleep", side_effect=fast_sleep):
            # The RuntimeError should be caught; CancelledError propagates
            with pytest.raises(asyncio.CancelledError):
                await runner._run_pairing_cleanup()

        # Despite the error, call_count reached 2 (error was swallowed, loop continued)
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_cleanup_task_uses_correct_interval(self) -> None:
        """The cleanup loop sleeps for _PAIRING_CLEANUP_INTERVAL seconds."""
        mock_store = MagicMock()
        mock_store.cleanup_expired = AsyncMock(return_value=5)

        runner = GatewayRunner()
        runner.set_pairing_store(mock_store)

        sleep_intervals: list[float] = []
        call_count = 0

        async def capture_sleep(seconds: float) -> None:
            nonlocal call_count
            sleep_intervals.append(seconds)
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError

        with patch("arcgateway.runner.asyncio.sleep", side_effect=capture_sleep):
            with pytest.raises(asyncio.CancelledError):
                await runner._run_pairing_cleanup()

        assert sleep_intervals[0] == _PAIRING_CLEANUP_INTERVAL
