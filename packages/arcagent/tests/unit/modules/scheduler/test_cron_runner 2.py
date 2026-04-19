"""Unit tests for CronRunner — T1.12 + T1.13 (SPEC-018 §3.4).

Coverage targets
----------------
* [SILENT] marker stripping before agent invocation.
* CRON_AGENT_KWARGS passed to agent_run_fn.
* Delivery happens when deliver_to is set.
* Delivery suppressed on success when silent_on_success is True.
* Delivery always happens on failure regardless of silent flag.
* Audit events emitted for all state transitions.
* DeliverySender Protocol satisfied by mock via isinstance check.
* Header wrapping format.
* cron.delivery_failed audit event on delivery exception.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from arcagent.modules.scheduler.cron_runner import CRON_AGENT_KWARGS, CronRunner
from arcagent.modules.scheduler.delivery import DeliverySender
from arcagent.modules.scheduler.models import CronJob, SILENT_MARKER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(
    *,
    agent_run_fn: Any = None,
    delivery_sender: Any = None,
    telemetry: Any = None,
) -> CronRunner:
    """Build a CronRunner with sensible defaults."""
    if telemetry is None:
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()
    if agent_run_fn is None:
        agent_run_fn = AsyncMock(return_value=_Result("agent output"))
    return CronRunner(
        agent_run_fn=agent_run_fn,
        telemetry=telemetry,
        delivery_sender=delivery_sender,
    )


class _Result:
    """Minimal stand-in for an arcrun result object."""

    def __init__(self, content: str) -> None:
        self.content = content


# ---------------------------------------------------------------------------
# DeliverySender Protocol
# ---------------------------------------------------------------------------


class TestDeliverySenderProtocol:
    """Verify that mock implementations satisfy the runtime_checkable Protocol."""

    def test_async_mock_satisfies_protocol(self) -> None:
        """AsyncMock with send() attribute satisfies DeliverySender."""

        class _Sender:
            async def send(self, target: Any, message: str) -> None: ...

        assert isinstance(_Sender(), DeliverySender)

    def test_object_without_send_does_not_satisfy_protocol(self) -> None:
        """Object without send() does not satisfy DeliverySender."""

        class _NoSend:
            pass

        assert not isinstance(_NoSend(), DeliverySender)


# ---------------------------------------------------------------------------
# CRON_AGENT_KWARGS — self-scheduling prevention
# ---------------------------------------------------------------------------


class TestCronAgentKwargs:
    """CRON_AGENT_KWARGS must contain the SDD-mandated disabled toolsets."""

    def test_disabled_toolsets_present(self) -> None:
        assert "disabled_toolsets" in CRON_AGENT_KWARGS

    def test_cronjob_toolset_disabled(self) -> None:
        assert "cronjob" in CRON_AGENT_KWARGS["disabled_toolsets"]

    def test_messaging_toolset_disabled(self) -> None:
        assert "messaging" in CRON_AGENT_KWARGS["disabled_toolsets"]

    def test_clarify_toolset_disabled(self) -> None:
        assert "clarify" in CRON_AGENT_KWARGS["disabled_toolsets"]

    def test_quiet_mode_enabled(self) -> None:
        assert CRON_AGENT_KWARGS.get("quiet_mode") is True

    def test_skip_context_files_enabled(self) -> None:
        assert CRON_AGENT_KWARGS.get("skip_context_files") is True

    def test_skip_memory_enabled(self) -> None:
        assert CRON_AGENT_KWARGS.get("skip_memory") is True


# ---------------------------------------------------------------------------
# [SILENT] marker extraction
# ---------------------------------------------------------------------------


class TestSilentMarkerExtraction:
    """CronRunner must strip [SILENT] before the agent sees it."""

    @pytest.mark.asyncio
    async def test_silent_marker_stripped_from_prompt(self) -> None:
        captured: list[str] = []

        async def capture_prompt(prompt: str, **kwargs: Any) -> _Result:
            captured.append(prompt)
            return _Result("ok")

        job = CronJob(
            name="test-job",
            schedule="0 9 * * *",
            prompt=f"{SILENT_MARKER} summarize messages",
        )
        runner = _make_runner(agent_run_fn=capture_prompt)
        await runner.run_job(job)

        assert captured, "agent_run_fn was not called"
        assert SILENT_MARKER not in captured[0], (
            f"[SILENT] marker not stripped; agent received: {captured[0]!r}"
        )
        assert "summarize messages" in captured[0]

    @pytest.mark.asyncio
    async def test_silent_marker_sets_silent_for_run(self) -> None:
        """[SILENT] in prompt → result.silent is True even if job.silent_on_success is False."""
        job = CronJob(
            name="test-job",
            schedule="0 9 * * *",
            prompt=f"{SILENT_MARKER} check status",
            silent_on_success=False,
        )
        runner = _make_runner()
        result = await runner.run_job(job)
        assert result.silent is True

    @pytest.mark.asyncio
    async def test_no_marker_preserves_prompt(self) -> None:
        captured: list[str] = []

        async def capture(prompt: str, **kwargs: Any) -> _Result:
            captured.append(prompt)
            return _Result("ok")

        job = CronJob(
            name="test-job",
            schedule="0 9 * * *",
            prompt="check the logs",
        )
        runner = _make_runner(agent_run_fn=capture)
        await runner.run_job(job)

        assert captured[0] == "check the logs"

    @pytest.mark.asyncio
    async def test_silent_on_success_flag_without_marker(self) -> None:
        """job.silent_on_success=True also suppresses delivery."""
        sender = AsyncMock()
        job = CronJob(
            name="quiet-job",
            schedule="0 9 * * *",
            prompt="do something",
            deliver_to="telegram:12345",
            silent_on_success=True,
        )
        runner = _make_runner(delivery_sender=sender)
        result = await runner.run_job(job)

        sender.send.assert_not_called()
        assert result.silent is True


# ---------------------------------------------------------------------------
# Agent invocation — CRON_AGENT_KWARGS forwarded
# ---------------------------------------------------------------------------


class TestAgentInvocation:
    """CronRunner must forward CRON_AGENT_KWARGS to agent_run_fn."""

    @pytest.mark.asyncio
    async def test_cron_agent_kwargs_passed(self) -> None:
        received_kwargs: dict[str, Any] = {}

        async def capture_kwargs(prompt: str, **kwargs: Any) -> _Result:
            received_kwargs.update(kwargs)
            return _Result("ok")

        job = CronJob(
            name="test-job",
            schedule="0 9 * * *",
            prompt="do work",
        )
        runner = _make_runner(agent_run_fn=capture_kwargs)
        await runner.run_job(job)

        for key, expected in CRON_AGENT_KWARGS.items():
            assert received_kwargs.get(key) == expected, (
                f"CRON_AGENT_KWARGS[{key!r}] not forwarded correctly; "
                f"got {received_kwargs.get(key)!r}"
            )

    @pytest.mark.asyncio
    async def test_result_content_extracted(self) -> None:
        async def agent(prompt: str, **kwargs: Any) -> _Result:
            return _Result("summary text")

        job = CronJob(name="j", schedule="* * * * *", prompt="summarize")
        runner = _make_runner(agent_run_fn=agent)
        result = await runner.run_job(job)

        assert result.content == "summary text"
        assert result.success is True

    @pytest.mark.asyncio
    async def test_agent_exception_produces_failure_result(self) -> None:
        async def failing_agent(prompt: str, **kwargs: Any) -> _Result:
            raise RuntimeError("agent exploded")

        job = CronJob(name="j", schedule="* * * * *", prompt="do work")
        runner = _make_runner(agent_run_fn=failing_agent)
        result = await runner.run_job(job)

        assert result.success is False
        assert "agent exploded" in (result.error or "")
        assert result.content == ""


# ---------------------------------------------------------------------------
# Delivery — T1.13
# ---------------------------------------------------------------------------


class TestDelivery:
    """Platform delivery behaviour."""

    @pytest.mark.asyncio
    async def test_delivery_happens_when_deliver_to_set(self) -> None:
        sender = AsyncMock()
        job = CronJob(
            name="digest",
            schedule="0 9 * * *",
            prompt="summarize overnight",
            deliver_to="telegram:12345",
        )
        runner = _make_runner(delivery_sender=sender)
        await runner.run_job(job)

        sender.send.assert_awaited_once()
        target_arg, message_arg = sender.send.call_args[0]
        assert target_arg == "telegram:12345"
        assert "digest" in message_arg
        assert "summarize overnight" in message_arg or "agent output" in message_arg

    @pytest.mark.asyncio
    async def test_delivery_not_called_when_no_deliver_to(self) -> None:
        sender = AsyncMock()
        job = CronJob(
            name="no-deliver",
            schedule="0 9 * * *",
            prompt="do something",
        )
        runner = _make_runner(delivery_sender=sender)
        await runner.run_job(job)

        sender.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_suppressed_on_success_with_silent(self) -> None:
        sender = AsyncMock()
        job = CronJob(
            name="silent-job",
            schedule="0 9 * * *",
            prompt=f"{SILENT_MARKER} check status",
            deliver_to="slack:CABC",
        )
        runner = _make_runner(delivery_sender=sender)
        await runner.run_job(job)

        sender.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_happens_on_failure_even_when_silent(self) -> None:
        sender = AsyncMock()

        async def failing_agent(prompt: str, **kwargs: Any) -> _Result:
            raise RuntimeError("it broke")

        job = CronJob(
            name="silent-failing",
            schedule="0 9 * * *",
            prompt=f"{SILENT_MARKER} do something",
            deliver_to="telegram:99",
            silent_on_success=True,
        )
        runner = _make_runner(agent_run_fn=failing_agent, delivery_sender=sender)
        await runner.run_job(job)

        # Even with silent flag, failures must deliver.
        sender.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delivery_header_format(self) -> None:
        """Output must be wrapped with [Scheduled task: name | ran at ts]."""
        messages: list[str] = []

        async def capture_send(target: Any, message: str) -> None:
            messages.append(message)

        class _Sender:
            async def send(self, target: Any, message: str) -> None:
                await capture_send(target, message)

        job = CronJob(
            name="morning-digest",
            schedule="0 9 * * *",
            prompt="summarize",
            deliver_to="telegram:1",
        )
        runner = _make_runner(delivery_sender=_Sender())
        await runner.run_job(job)

        assert messages, "no delivery occurred"
        msg = messages[0]
        assert "[Scheduled task: morning-digest | ran at" in msg

    @pytest.mark.asyncio
    async def test_delivery_failure_does_not_raise(self) -> None:
        """A flaky delivery sender must not crash the scheduler."""
        sender = AsyncMock()
        sender.send.side_effect = ConnectionError("Telegram down")

        job = CronJob(
            name="resilient",
            schedule="0 9 * * *",
            prompt="do work",
            deliver_to="telegram:1",
        )
        runner = _make_runner(delivery_sender=sender)
        # Must not raise.
        result = await runner.run_job(job)
        assert result.success is True  # Agent succeeded; only delivery failed.


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


class TestAuditEvents:
    """Structured audit events must be emitted for all transitions."""

    @pytest.mark.asyncio
    async def test_disabled_tools_audit_event(self) -> None:
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()

        job = CronJob(name="j", schedule="* * * * *", prompt="p")
        runner = _make_runner(telemetry=telemetry)
        await runner.run_job(job)

        event_types = [c.args[0] for c in telemetry.audit_event.call_args_list]
        assert "cron.session.disabled_tools" in event_types

    @pytest.mark.asyncio
    async def test_delivered_audit_event(self) -> None:
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()
        sender = AsyncMock()

        job = CronJob(
            name="j",
            schedule="* * * * *",
            prompt="p",
            deliver_to="telegram:1",
        )
        runner = _make_runner(telemetry=telemetry, delivery_sender=sender)
        await runner.run_job(job)

        event_types = [c.args[0] for c in telemetry.audit_event.call_args_list]
        assert "cron.delivered" in event_types

    @pytest.mark.asyncio
    async def test_skipped_silent_audit_event(self) -> None:
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()
        sender = AsyncMock()

        job = CronJob(
            name="j",
            schedule="* * * * *",
            prompt=f"{SILENT_MARKER} check",
            deliver_to="telegram:1",
        )
        runner = _make_runner(telemetry=telemetry, delivery_sender=sender)
        await runner.run_job(job)

        event_types = [c.args[0] for c in telemetry.audit_event.call_args_list]
        assert "cron.skipped_silent" in event_types
        # cron.delivered must NOT appear — delivery was suppressed.
        assert "cron.delivered" not in event_types

    @pytest.mark.asyncio
    async def test_delivery_failed_audit_event(self) -> None:
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()
        sender = AsyncMock()
        sender.send.side_effect = ConnectionError("down")

        job = CronJob(
            name="j",
            schedule="* * * * *",
            prompt="p",
            deliver_to="telegram:1",
        )
        runner = _make_runner(telemetry=telemetry, delivery_sender=sender)
        await runner.run_job(job)

        event_types = [c.args[0] for c in telemetry.audit_event.call_args_list]
        assert "cron.delivery_failed" in event_types
