"""Integration tests for T1.13 — Cron Platform Delivery (SPEC-018 §3.4).

Verifies end-to-end delivery behaviour:
- Delivery occurs when deliver_to is set and silent is inactive.
- Delivery suppressed on success when silent_on_success=True or [SILENT] in prompt.
- Delivery always occurs on failure regardless of silent flag.
- [SILENT] stripped so agent receives clean prompt.
- cron.delivered / cron.skipped_silent audit events fire correctly.
- Mock DeliverySender satisfies Protocol via isinstance check.
- Output is wrapped with the canonical delivery header.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.scheduler.cron_runner import CronRunner
from arcagent.modules.scheduler.delivery import DeliverySender
from arcagent.modules.scheduler.models import CronJob, SILENT_MARKER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockDeliverySender:
    """Concrete DeliverySender implementation for testing."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    async def send(self, target: Any, message: str) -> None:
        self.calls.append((target, message))


def _make_runner(
    *,
    agent_fn: Any = None,
    sender: Any = None,
    telemetry: Any = None,
) -> tuple[CronRunner, MagicMock]:
    if telemetry is None:
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()
    if agent_fn is None:
        agent_fn = AsyncMock(
            return_value=type("R", (), {"content": "agent output"})()
        )
    runner = CronRunner(
        agent_run_fn=agent_fn,
        telemetry=telemetry,
        delivery_sender=sender,
    )
    return runner, telemetry


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestDeliverySenderProtocolCompliance:
    """Mock DeliverySender implementations must satisfy the Protocol."""

    def test_concrete_impl_satisfies_protocol(self) -> None:
        assert isinstance(_MockDeliverySender(), DeliverySender)

    def test_async_mock_satisfies_protocol(self) -> None:
        """AsyncMock objects with send() satisfy DeliverySender (runtime_checkable)."""

        class _Sender:
            async def send(self, target: Any, message: str) -> None: ...

        assert isinstance(_Sender(), DeliverySender)

    def test_plain_object_without_send_fails_protocol(self) -> None:
        class _Bad:
            pass

        assert not isinstance(_Bad(), DeliverySender)


# ---------------------------------------------------------------------------
# Delivery on success (not silent)
# ---------------------------------------------------------------------------


class TestDeliveryOnSuccess:
    @pytest.mark.asyncio
    async def test_delivery_happens_when_deliver_to_set(self) -> None:
        sender = _MockDeliverySender()
        runner, _ = _make_runner(sender=sender)

        job = CronJob(
            name="morning-digest",
            schedule="0 9 * * 1-5",
            prompt="summarize overnight Slack DMs",
            deliver_to="telegram:joshs-channel",
        )
        await runner.run_job(job)

        assert len(sender.calls) == 1
        target, message = sender.calls[0]
        assert target == "telegram:joshs-channel"

    @pytest.mark.asyncio
    async def test_no_delivery_when_deliver_to_not_set(self) -> None:
        sender = _MockDeliverySender()
        runner, _ = _make_runner(sender=sender)

        job = CronJob(name="local", schedule="0 9 * * *", prompt="run locally")
        await runner.run_job(job)

        assert len(sender.calls) == 0

    @pytest.mark.asyncio
    async def test_delivery_header_present_in_message(self) -> None:
        sender = _MockDeliverySender()
        runner, _ = _make_runner(sender=sender)

        job = CronJob(
            name="digest-job",
            schedule="0 9 * * *",
            prompt="summarize",
            deliver_to="slack:C123",
        )
        await runner.run_job(job)

        assert sender.calls
        _, message = sender.calls[0]
        assert "[Scheduled task: digest-job | ran at" in message

    @pytest.mark.asyncio
    async def test_delivery_message_contains_agent_output(self) -> None:
        async def agent_fn(prompt: str, **kwargs: Any) -> Any:
            return type("R", (), {"content": "here is the summary"})()

        sender = _MockDeliverySender()
        runner, _ = _make_runner(agent_fn=agent_fn, sender=sender)

        job = CronJob(
            name="j",
            schedule="0 9 * * *",
            prompt="summarize",
            deliver_to="telegram:1",
        )
        await runner.run_job(job)

        _, message = sender.calls[0]
        assert "here is the summary" in message


# ---------------------------------------------------------------------------
# Silent suppression
# ---------------------------------------------------------------------------


class TestSilentSuppression:
    @pytest.mark.asyncio
    async def test_delivery_suppressed_on_success_with_silent_on_success_flag(self) -> None:
        sender = _MockDeliverySender()
        runner, _ = _make_runner(sender=sender)

        job = CronJob(
            name="quiet-job",
            schedule="0 9 * * *",
            prompt="check logs",
            deliver_to="telegram:12345",
            silent_on_success=True,
        )
        await runner.run_job(job)

        assert len(sender.calls) == 0, "Delivery should have been suppressed on success"

    @pytest.mark.asyncio
    async def test_delivery_suppressed_with_silent_marker_in_prompt(self) -> None:
        sender = _MockDeliverySender()
        runner, _ = _make_runner(sender=sender)

        job = CronJob(
            name="silent-marker-job",
            schedule="0 9 * * *",
            prompt=f"{SILENT_MARKER} check status",
            deliver_to="telegram:12345",
        )
        await runner.run_job(job)

        assert len(sender.calls) == 0

    @pytest.mark.asyncio
    async def test_silent_marker_stripped_agent_receives_clean_prompt(self) -> None:
        captured_prompts: list[str] = []

        async def agent_fn(prompt: str, **kwargs: Any) -> Any:
            captured_prompts.append(prompt)
            return type("R", (), {"content": "ok"})()

        sender = _MockDeliverySender()
        runner, _ = _make_runner(agent_fn=agent_fn, sender=sender)

        job = CronJob(
            name="clean-prompt",
            schedule="0 9 * * *",
            prompt=f"{SILENT_MARKER} summarize messages",
            deliver_to="telegram:1",
        )
        await runner.run_job(job)

        assert captured_prompts, "agent_fn not called"
        assert SILENT_MARKER not in captured_prompts[0], (
            f"Agent received raw [SILENT] marker: {captured_prompts[0]!r}"
        )
        assert "summarize messages" in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_delivery_happens_on_failure_despite_silent_flag(self) -> None:
        """Failures always deliver, even when silent_on_success=True."""

        async def failing_fn(prompt: str, **kwargs: Any) -> Any:
            raise RuntimeError("downstream error")

        sender = _MockDeliverySender()
        runner, _ = _make_runner(agent_fn=failing_fn, sender=sender)

        job = CronJob(
            name="silent-but-failing",
            schedule="0 9 * * *",
            prompt=f"{SILENT_MARKER} risky task",
            deliver_to="slack:C456",
            silent_on_success=True,
        )
        await runner.run_job(job)

        assert len(sender.calls) == 1, (
            "Failure should always deliver, even with silent flag active"
        )

    @pytest.mark.asyncio
    async def test_failure_message_contains_error(self) -> None:
        async def failing_fn(prompt: str, **kwargs: Any) -> Any:
            raise RuntimeError("disk full")

        sender = _MockDeliverySender()
        runner, _ = _make_runner(agent_fn=failing_fn, sender=sender)

        job = CronJob(
            name="failing-job",
            schedule="0 9 * * *",
            prompt="do work",
            deliver_to="telegram:1",
        )
        await runner.run_job(job)

        assert sender.calls
        _, message = sender.calls[0]
        assert "disk full" in message or "ERROR" in message


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


class TestDeliveryAuditEvents:
    @pytest.mark.asyncio
    async def test_cron_delivered_audit_event_fired(self) -> None:
        sender = _MockDeliverySender()
        runner, telemetry = _make_runner(sender=sender)

        job = CronJob(
            name="j",
            schedule="* * * * *",
            prompt="p",
            deliver_to="telegram:1",
        )
        await runner.run_job(job)

        event_types = [c.args[0] for c in telemetry.audit_event.call_args_list]
        assert "cron.delivered" in event_types

    @pytest.mark.asyncio
    async def test_cron_skipped_silent_audit_event_fired(self) -> None:
        sender = _MockDeliverySender()
        runner, telemetry = _make_runner(sender=sender)

        job = CronJob(
            name="j",
            schedule="* * * * *",
            prompt=f"{SILENT_MARKER} check",
            deliver_to="telegram:1",
        )
        await runner.run_job(job)

        event_types = [c.args[0] for c in telemetry.audit_event.call_args_list]
        assert "cron.skipped_silent" in event_types
        # cron.delivered must NOT appear since suppressed.
        assert "cron.delivered" not in event_types

    @pytest.mark.asyncio
    async def test_cron_delivery_failed_audit_event_on_sender_error(self) -> None:
        sender = AsyncMock()
        sender.send.side_effect = ConnectionError("Telegram down")
        runner, telemetry = _make_runner(sender=sender)

        job = CronJob(
            name="j",
            schedule="* * * * *",
            prompt="p",
            deliver_to="telegram:1",
        )
        await runner.run_job(job)

        event_types = [c.args[0] for c in telemetry.audit_event.call_args_list]
        assert "cron.delivery_failed" in event_types

    @pytest.mark.asyncio
    async def test_cron_session_disabled_tools_audit_event_always_fired(self) -> None:
        """cron.session.disabled_tools is the first event regardless of outcome."""
        runner, telemetry = _make_runner()

        job = CronJob(name="j", schedule="* * * * *", prompt="p")
        await runner.run_job(job)

        calls = telemetry.audit_event.call_args_list
        assert calls, "No audit events emitted"
        assert calls[0].args[0] == "cron.session.disabled_tools"


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


class TestDeliveryResilience:
    @pytest.mark.asyncio
    async def test_sender_exception_does_not_propagate(self) -> None:
        """A crashing delivery sender must not surface an exception to the caller."""
        sender = AsyncMock()
        sender.send.side_effect = OSError("network failure")
        runner, _ = _make_runner(sender=sender)

        job = CronJob(
            name="resilient",
            schedule="* * * * *",
            prompt="p",
            deliver_to="telegram:1",
        )
        # Must not raise.
        result = await runner.run_job(job)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_no_sender_configured_no_error(self) -> None:
        runner, _ = _make_runner(sender=None)

        job = CronJob(
            name="no-sender",
            schedule="* * * * *",
            prompt="p",
            deliver_to="telegram:1",  # Configured but no sender wired
        )
        # Must not raise.
        result = await runner.run_job(job)
        assert result.success is True
