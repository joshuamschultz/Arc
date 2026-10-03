"""A browser leaving never cancels the run it started.

The run is the agent's work, not the tab's. These tests drive the real
``SessionRouter`` + ``WebPlatformAdapter`` + ``AsyncioExecutor`` with a fake
agent that is held on an ``asyncio.Event`` so the test, not a sleep, decides
when the run finishes. Production incident 2026-10-03: a 14-minute turn ended
"Run cancelled by did:arc:gateway: browser disconnected" and its answer was lost.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import arcagent
import pytest

from arcgateway import fs_reader
from arcgateway.adapters.web import WebPlatformAdapter
from arcgateway.executor import AsyncioExecutor
from arcgateway.session import SessionRouter

pytestmark = pytest.mark.asyncio

_AGENT = "did:arc:agent:olivia"
_USER = "did:arc:user:josh"
_FINAL = "Here is the finished research answer."
_REPLAY_TTL = 0.01


class _Socket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


class _RunHandle:
    def __init__(self, agent: _HeldAgent) -> None:
        self._agent = agent

    async def cancel(self, caller_did: str, *, reason: str = "") -> None:
        self._agent.cancels.append((caller_did, reason))
        self._agent.release.set()


class _HeldAgent:
    """Streams one text frame, holds until released, then persists + finishes.

    Persisting to ``sessions/<key>.jsonl`` mirrors what the real agent's
    session manager does: the agent owns history, independent of any socket.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancels: list[tuple[str, str]] = []
        self.terminal_status = ""

    def active_run(self, _session_key: str) -> _RunHandle:
        return _RunHandle(self)

    async def stream_delivered_message(
        self, *, session_key: str, **_: Any
    ) -> AsyncIterator[arcagent.DeliveryStreamEvent]:
        yield arcagent.DeliveryTextEvent(run_id="run-1", sequence=1, text="working")
        self.started.set()
        await self.release.wait()
        if self.cancels:
            self.terminal_status = "cancelled"
            yield arcagent.DeliveryTerminalEvent(
                run_id="run-1", sequence=3, status="cancelled", reason="browser disconnected"
            )
            return
        sessions = self._workspace / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        line = {"type": "message", "role": "assistant", "content": _FINAL, "timestamp": "t"}
        with (sessions / f"{session_key}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
        yield arcagent.DeliveryTextEvent(run_id="run-1", sequence=2, text=_FINAL)
        self.terminal_status = "completed"
        yield arcagent.DeliveryTerminalEvent(run_id="run-1", sequence=3, status="completed")


class _Rig:
    def __init__(self, workspace: Path) -> None:
        self.agent = _HeldAgent(workspace)
        self.workspace = workspace

        async def _factory(_agent_did: str) -> _HeldAgent:
            return self.agent

        self.router = SessionRouter(executor=AsyncioExecutor(agent_factory=_factory))
        self.adapter = WebPlatformAdapter(
            on_message=self.router.handle, agent_did=_AGENT, replay_ttl_seconds=_REPLAY_TTL
        )
        self.router.register_adapter(self.adapter)
        self.chat_id = self.router.current_session_key(_AGENT, _USER)

    async def start_run_then_close_tab(self) -> None:
        socket = _Socket()
        self.adapter.register_socket(socket, _AGENT, _USER, self.chat_id)
        await self.adapter.ingest(self.chat_id, "research this", client_seq=1, ws=socket)
        await self.agent.started.wait()
        self.adapter.unregister_socket(socket)
        # Past the replay TTL: every grace window the old code had has expired.
        await asyncio.gather(*self.adapter._eviction_tasks.values(), return_exceptions=True)

    async def finish(self) -> None:
        self.agent.release.set()
        await asyncio.gather(*self.router._pending_tasks)

    def history(self) -> list[dict[str, Any]]:
        content = fs_reader.read_file(
            scope="agent",
            agent_id="olivia",
            agent_root=self.workspace,
            rel_path=f"sessions/{self.chat_id}.jsonl",
            caller_did=_USER,
        )
        return [json.loads(line) for line in content.content.splitlines() if line]


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    return _Rig(tmp_path)


async def test_run_completes_after_the_last_socket_leaves_past_the_grace_window(
    rig: _Rig,
) -> None:
    await rig.start_run_then_close_tab()

    await rig.finish()

    assert rig.agent.cancels == []
    assert rig.agent.terminal_status == "completed"


async def test_finished_while_away_answer_is_in_session_history(rig: _Rig) -> None:
    await rig.start_run_then_close_tab()

    await rig.finish()

    assistant = [m for m in rig.history() if m["role"] == "assistant"]
    assert [m["content"] for m in assistant] == [_FINAL]


async def test_reconnect_after_completion_attaches_and_history_has_the_answer(
    rig: _Rig,
) -> None:
    await rig.start_run_then_close_tab()
    await rig.finish()

    returning = _Socket()
    rig.adapter.register_socket(returning, _AGENT, _USER, rig.chat_id, since_seq=-1)

    assert rig.chat_id in rig.adapter._sockets
    assert rig.history()[-1]["content"] == _FINAL
    await rig.adapter.disconnect()
