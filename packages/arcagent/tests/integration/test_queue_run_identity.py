"""Real agent/model composition keeps provider calls and traces on one run identity."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from arcllm._trace_crypto import unseal
from arcllm.adapters.anthropic import AnthropicAdapter
from arcllm.modules.telemetry import TelemetryModule
from arcllm.types import Delta, LLMResponse, Message, Usage
from arcrun import CallQueueCoordinator
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from arcagent.core.agent import ArcAgent
from arcagent.core.background_tasks import BackgroundTaskSupervisor
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)


@pytest.mark.asyncio
async def test_shutdown_cancels_stream_waiting_for_session_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "identity.md").write_text("Agent: waiting")
    config = ArcAgentConfig(
        agent=AgentConfig(name="waiting", workspace=str(workspace)),
        llm=LLMConfig(model="anthropic/claude-opus-5"),
        identity=IdentityConfig(key_dir=str(tmp_path / "keys")),
        telemetry=TelemetryConfig(enabled=False),
    )
    agent = ArcAgent(config)
    await agent.startup()
    session = await agent.session("blocked")
    await agent._run_coordinator.acquire_turn(session.session_id)
    stream = agent.run("waiting", session=session)
    consumer = asyncio.create_task(anext(stream))
    try:
        for _ in range(100):
            if any(
                task.get_name() == f"agent_stream:{session.session_id}"
                for task in asyncio.all_tasks()
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("stream producer did not start")
        await asyncio.wait_for(agent.shutdown(), 5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 1)
        assert agent._background_tasks.task_count == 0
        assert not any(
            task.get_name() == f"agent_stream:{session.session_id}" for task in asyncio.all_tasks()
        )
    finally:
        agent._run_coordinator.release_turn(session.session_id)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
async def test_early_close_releases_stream_owner_with_full_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arcagent.core import agent_dispatch

    entered = asyncio.Event()
    released = asyncio.Event()
    producer_done = asyncio.Event()
    owner: asyncio.Task[None] | None = None
    retained: list[AsyncIterator[Any]] = []

    @asynccontextmanager
    async def turn(_session_id: str) -> AsyncIterator[None]:
        entered.set()
        try:
            yield
        finally:
            released.set()

    async def locked(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        nonlocal owner
        owner = asyncio.current_task()
        try:
            for _ in range(100):
                yield object()
        finally:
            producer_done.set()

    def create_locked(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        stream = locked(*args, **kwargs)
        retained.append(stream)
        return stream

    monkeypatch.setattr(agent_dispatch, "_dispatch_stream_locked", create_locked)

    class FakeAgent:
        _run_coordinator = type("Coordinator", (), {"turn": staticmethod(turn)})()
        _background_tasks = BackgroundTaskSupervisor()

    class FakeSession:
        session_id = "session"

    stream = agent_dispatch.dispatch_stream(
        cast(ArcAgent, FakeAgent()), "task", session=cast(Any, FakeSession())
    )
    await asyncio.wait_for(asyncio.create_task(anext(stream)), 1)
    await asyncio.wait_for(asyncio.create_task(stream.aclose()), 1)
    assert producer_done.is_set()
    assert released.is_set()
    assert owner is not None and owner.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_from", ["provider", "shutdown"])
async def test_producer_cancellation_wakes_waiting_consumer(
    monkeypatch: pytest.MonkeyPatch, cancel_from: str
) -> None:
    from arcagent.core import agent_dispatch

    entered = asyncio.Event()
    producer: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def turn(_session_id: str) -> AsyncIterator[None]:
        yield

    async def locked(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        nonlocal producer
        producer = asyncio.current_task()
        entered.set()
        if cancel_from == "provider":
            raise asyncio.CancelledError
        await asyncio.Event().wait()
        yield object()

    monkeypatch.setattr(agent_dispatch, "_dispatch_stream_locked", locked)

    class FakeAgent:
        _run_coordinator = type("Coordinator", (), {"turn": staticmethod(turn)})()
        _background_tasks = BackgroundTaskSupervisor()

    class FakeSession:
        session_id = "session"

    stream = agent_dispatch.dispatch_stream(
        cast(ArcAgent, FakeAgent()), "task", session=cast(Any, FakeSession())
    )
    consumer = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 1)
    if cancel_from == "shutdown":
        assert producer is not None
        producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer, 1)
    assert producer is not None and producer.done()


@pytest.mark.asyncio
async def test_stream_and_tracked_runs_join_wire_prompt_encrypted_trace_and_spool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "identity.md").write_text("Agent: queue-integrated")
    (workspace / "context.md").write_text("Context marker: complete work.")
    key = AESGCM.generate_key(bit_length=256)
    monkeypatch.setenv("ARCLLM_TRACE_WRAP_KEY", base64.b64encode(key).decode("ascii"))
    config = ArcAgentConfig(
        agent=AgentConfig(
            name="queue-integrated", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(
            model="anthropic/claude-opus-5",
            modules={
                "telemetry": {
                    "store_raw_bodies": True,
                    "encryption": {"enabled": True, "key_ref": "test-kek"},
                    "encryption_key_secret": base64.b64encode(key).decode("ascii"),
                }
            },
        ),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=10000),
    )
    coordinator = CallQueueCoordinator()
    agent = ArcAgent(config, queue_coordinator=coordinator, queue_tenant_id="tenant-verified")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import arcstore.spool as spool

    spool_file = tmp_path / "spool.jsonl"
    monkeypatch.setattr(spool, "spool_path", lambda: spool_file)
    wire_messages: list[list[Message]] = []

    async def wire(*args: Any, **_kwargs: Any) -> LLMResponse:
        wire_messages.append(args[1])
        return LLMResponse(
            content="finished",
            model="claude-opus-5",
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=2, total_tokens=12),
        )

    async def wire_stream(*args: Any, **_kwargs: Any) -> AsyncIterator[Delta]:
        wire_messages.append(args[1])
        yield Delta(text="finished")

    with (
        patch.object(AnthropicAdapter, "invoke", wire),
        patch.object(AnthropicAdapter, "invoke_stream", wire_stream),
    ):
        await agent.startup()
        try:
            session = await agent.session("chat-thread")
            model = agent._ensure_model()
            while not isinstance(model, TelemetryModule):
                model = model._inner
            model._lineage_default = {
                "oversized": "x" * 10000,
                "run_id": "spoofed",
                "origin": "spoofed",
            }
            provider_closed = asyncio.Event()

            async def early_wire(*_args: Any, **_kwargs: Any) -> AsyncIterator[Delta]:
                try:
                    yield Delta(text="first")
                    await asyncio.Event().wait()
                finally:
                    provider_closed.set()

            async def early_run_stream(**kwargs: Any) -> AsyncIterator[Any]:
                async def stream() -> AsyncIterator[Any]:
                    provider_stream = direct_model.invoke_stream(
                        [Message(role="user", content="first")]
                    )
                    try:
                        yield await anext(provider_stream)
                    finally:
                        await provider_stream.aclose()

                return stream()

            from arcllm import load_model

            direct_model = load_model(
                "anthropic",
                queue_coordinator=coordinator,
                telemetry=False,
                security=False,
                retry=False,
            )
            with (
                patch.object(AnthropicAdapter, "invoke_stream", early_wire),
                patch("arcrun.run_stream", early_run_stream),
            ):
                early = agent.run("Early close", session=session, run_id="early-run")
                await asyncio.wait_for(anext(early), 1)
                await asyncio.wait_for(early.aclose(), 1)
            assert provider_closed.is_set()
            assert coordinator.snapshot()["active"] == 0
            await direct_model.close()
            async for _ in agent.run(
                "First task", session=session, run_id="stream-run", allowed_strategies=["oneshot"]
            ):
                pass
            cross_task = agent.run(
                "Cross task",
                session=session,
                run_id="cross-task-run",
                allowed_strategies=["oneshot"],
            )
            await asyncio.create_task(anext(cross_task))

            async def consume_elsewhere() -> None:
                async for _ in cross_task:
                    pass

            await asyncio.create_task(consume_elsewhere())
            handle = await agent.start_tracked_run("Second task", session_key="chat-thread")
            await handle.result()
            await asyncio.sleep(0)
            from arcstore.spool import request_context

            from arcagent.core.session_internal.capability_ledger import (
                bind_session_id,
                reset_session_id,
            )

            session_token = bind_session_id(session.session_id)
            try:
                with request_context("parent-run"):
                    await agent.run_oneshot(system="Evaluate", user="One decision")
            finally:
                reset_session_id(session_token)
            jobs = await coordinator.jobs()
            traces, _cursor = await agent._trace_store.query(limit=100)
        finally:
            await agent.shutdown()

    assert {job.run_id for job in jobs} >= {"stream-run"}
    assert {job.session_id for job in jobs} == {session.session_id}
    assert all(job.tenant_id == "tenant-verified" for job in jobs)
    assert len({job.call_id for job in jobs}) == len(jobs)
    assert wire_messages
    assert any(
        "Context marker" in str(message.content) for call in wire_messages for message in call
    )
    assert any(
        "strategy" in str(message.content).lower() for call in wire_messages for message in call
    )
    run_traces = [record for record in traces if record.lineage and record.lineage.get("run_id")]
    assert run_traces
    for record in run_traces:
        assert record.encryption is not None
        assert record.request_body is None
        assert record.lineage is not None
        assert record.lineage["session_id"] == session.session_id
        assert record.lineage["request_id"] == record.lineage["run_id"]
        assert record.lineage["run_id"] != "spoofed"
        assert record.lineage["origin"] != "spoofed"
        assert record.lineage["truncated"] is True
        body = unseal(
            record.encryption,
            trace_id=record.trace_id,
            timestamp=record.timestamp,
            wrapping_key=key,
        )
        assert body["request_body"]["messages"]
    spool_rows = [json.loads(line) for line in spool_file.read_text().splitlines()]
    assert any(
        row["request_id"] == "stream-run" for row in spool_rows if row["kind"] == "llm_call"
    )
    assert all("request_body" not in row.get("extra", {}) for row in spool_rows)
    evaluations = [
        record
        for record in run_traces
        if record.lineage and record.lineage["origin"] == "evaluation"
    ]
    assert len(evaluations) == 1
    assert evaluations[0].lineage is not None
    assert evaluations[0].lineage["session_id"] == session.session_id
    assert evaluations[0].lineage["parent_run_id"] == "parent-run"
