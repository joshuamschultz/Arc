"""Task 32 — interleaving regression tests for the highest-risk module
runtimes swept from module-global state to ``contextvars.ContextVar``:
memory (DB/workspace path bleed), telegram and slack (bot credential
bleed), and session (identity-graph/index bleed).

These mirror ``packages/arcagent/tests/security/test_multi_agent_runtime_isolation.py``
(task 27's reproduction of the live DGX incident): two simulated agent
turns are forced to interleave via ``asyncio.Event`` so agent B's
``configure()`` call genuinely runs while agent A's turn is suspended
mid-flight, exactly as concurrent chat sessions do in the embedded
gateway (per feedback_concurrency_tests_must_interleave — sequential
calls alone don't prove a concurrency fix).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arcagent.modules.memory import _runtime as memory_runtime
from arcagent.modules.session import _runtime as session_runtime
from arcagent.modules.slack import _runtime as slack_runtime
from arcagent.modules.telegram import _runtime as telegram_runtime


@pytest.fixture(autouse=True)
def _reset_all() -> None:
    memory_runtime.reset()
    telegram_runtime.reset()
    slack_runtime.reset()
    session_runtime.reset()


class TestMemoryRuntimeIsolation:
    """Task 32 highest-risk target: memory DB/workspace path bleed."""

    @pytest.mark.asyncio
    async def test_interleaved_agents_keep_own_workspace(self, tmp_path: Path) -> None:
        josh_ws = tmp_path / "josh_agent" / "workspace"
        coder_ws = tmp_path / "coder_agent" / "workspace"
        josh_ws.mkdir(parents=True)
        coder_ws.mkdir(parents=True)

        coder_started = asyncio.Event()
        josh_may_resume = asyncio.Event()
        observed: dict[str, object] = {}

        async def josh_turn() -> None:
            memory_runtime.configure(
                config={"brain": "none"}, workspace=josh_ws, agent_did="did:arc:agent:josh"
            )
            coder_started.set()
            await josh_may_resume.wait()
            observed["workspace"] = memory_runtime.state().workspace

        async def coder_turn() -> None:
            await coder_started.wait()
            memory_runtime.configure(
                config={"brain": "none"}, workspace=coder_ws, agent_did="did:arc:agent:coder"
            )
            josh_may_resume.set()

        await asyncio.gather(josh_turn(), coder_turn())

        assert observed["workspace"] == josh_ws.resolve(), (
            "josh's memory tool calls must resolve josh's own workspace/DB path, "
            "not coder's — a sibling task's configure() must never leak across tasks"
        )


class TestTelegramRuntimeIsolation:
    """Task 32 highest-risk target: bot credential bleed.

    ``bot_token_env_var`` names the env var the real token is resolved
    from at connect time (never the token itself in config) — a distinct
    value per agent is sufficient to prove config-level isolation without
    needing to actually resolve secrets in this test.
    """

    @pytest.mark.asyncio
    async def test_interleaved_agents_keep_own_bot_config(self, tmp_path: Path) -> None:
        coder_started = asyncio.Event()
        josh_may_resume = asyncio.Event()
        observed: dict[str, object] = {}

        async def josh_turn() -> None:
            telegram_runtime.configure(
                config={"bot_token_env_var": "JOSH_TELEGRAM_TOKEN"}, workspace=tmp_path
            )
            coder_started.set()
            await josh_may_resume.wait()
            observed["env_var"] = telegram_runtime.state().config.bot_token_env_var
            observed["bot_config_env_var"] = telegram_runtime.state().bot._config.bot_token_env_var

        async def coder_turn() -> None:
            await coder_started.wait()
            telegram_runtime.configure(
                config={"bot_token_env_var": "CODER_TELEGRAM_TOKEN"}, workspace=tmp_path
            )
            josh_may_resume.set()

        await asyncio.gather(josh_turn(), coder_turn())

        assert observed["env_var"] == "JOSH_TELEGRAM_TOKEN", (
            "josh's telegram tool calls must use josh's own bot config, not "
            "coder's — the exact credential-bleed vector the DGX incident "
            "demonstrated for signing keys, closed here for bot tokens"
        )
        assert observed["bot_config_env_var"] == "JOSH_TELEGRAM_TOKEN", (
            "the TelegramBot instance itself (built at configure() time) must "
            "also carry josh's own config, not coder's"
        )


class TestSlackRuntimeIsolation:
    """Task 32 highest-risk target: bot credential bleed (Slack side)."""

    @pytest.mark.asyncio
    async def test_interleaved_agents_keep_own_config(self, tmp_path: Path) -> None:
        coder_started = asyncio.Event()
        josh_may_resume = asyncio.Event()
        observed: dict[str, object] = {}

        async def josh_turn() -> None:
            slack_runtime.configure(
                config={"bot_token_env_var": "JOSH_SLACK_TOKEN"}, workspace=tmp_path
            )
            coder_started.set()
            await josh_may_resume.wait()
            observed["env_var"] = slack_runtime.state().config.bot_token_env_var

        async def coder_turn() -> None:
            await coder_started.wait()
            slack_runtime.configure(
                config={"bot_token_env_var": "CODER_SLACK_TOKEN"}, workspace=tmp_path
            )
            josh_may_resume.set()

        await asyncio.gather(josh_turn(), coder_turn())

        assert observed["env_var"] == "JOSH_SLACK_TOKEN", (
            "josh's slack tool calls must use josh's own bot config, not coder's"
        )


class TestSessionRuntimeIsolation:
    """Task 32 highest-risk target: session index/identity-graph bleed."""

    @pytest.mark.asyncio
    async def test_interleaved_agents_keep_own_workspace(self, tmp_path: Path) -> None:
        josh_ws = tmp_path / "josh_agent" / "workspace"
        coder_ws = tmp_path / "coder_agent" / "workspace"
        josh_ws.mkdir(parents=True)
        coder_ws.mkdir(parents=True)

        coder_started = asyncio.Event()
        josh_may_resume = asyncio.Event()
        observed: dict[str, object] = {}

        async def josh_turn() -> None:
            session_runtime.configure(workspace=josh_ws)
            coder_started.set()
            await josh_may_resume.wait()
            observed["workspace"] = session_runtime.state().workspace

        async def coder_turn() -> None:
            await coder_started.wait()
            session_runtime.configure(workspace=coder_ws)
            josh_may_resume.set()

        await asyncio.gather(josh_turn(), coder_turn())

        assert observed["workspace"] == josh_ws.resolve(), (
            "josh's session tool calls must resolve josh's own session index/"
            "identity-graph workspace, not coder's"
        )
