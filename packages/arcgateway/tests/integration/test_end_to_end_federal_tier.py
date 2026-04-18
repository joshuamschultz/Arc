"""End-to-end integration test — federal tier (SubprocessExecutor).

Tests that the federal-tier path uses SubprocessExecutor:
  - GatewayRunner.from_config(config_with_tier=federal) creates SubprocessExecutor
  - SubprocessExecutor spawns a subprocess for each inbound event
  - The subprocess runs arc-agent-worker which returns Delta JSON-lines
  - GatewayRunner.from_config(config_with_tier=personal) creates AsyncioExecutor

The subprocess tests do NOT require a real ArcAgent config or LLM credentials.
The arc-agent-worker falls back to the echo stub when no arcagent.toml is found,
which is the correct behavior for test environments.

M1 Acceptance Gate coverage:
  - tier=federal → SubprocessExecutor selected (not AsyncioExecutor)
  - tier=personal → AsyncioExecutor selected
  - SubprocessExecutor spawns a real subprocess and receives Delta stream
  - SubprocessExecutor stream ends with is_final=True sentinel
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from arcgateway.config import GatewayConfig
from arcgateway.executor import (
    AsyncioExecutor,
    Delta,
    InboundEvent,
    SubprocessExecutor,
)
from arcgateway.runner import GatewayRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Worker command using sys.executable so tests work from source without a wheel.
# PYTHONPATH is set by conftest.py so arccli.agent_worker is importable.
_WORKER_CMD = [sys.executable, "-m", "arccli.agent_worker"]

_TIMEOUT = 15.0  # seconds; generous for CI


def _make_event(
    message: str = "hello federal",
    session_key: str = "fed_session_01",
    agent_did: str = "did:arc:agent:federal_test",
) -> InboundEvent:
    """Build a minimal InboundEvent for federal-tier tests."""
    return InboundEvent(
        platform="telegram",
        chat_id="99",
        user_did="did:arc:telegram:99",
        agent_did=agent_did,
        session_key=session_key,
        message=message,
    )


# ---------------------------------------------------------------------------
# GatewayConfig helpers
# ---------------------------------------------------------------------------


def _federal_config(tmp_path: Path) -> GatewayConfig:
    """Return a minimal federal-tier GatewayConfig."""
    return GatewayConfig.from_toml_str(f"""
[gateway]
tier = "federal"
agent_did = "did:arc:agent:federal_test"
runtime_dir = "{tmp_path / 'run'}"
""")


def _personal_config(tmp_path: Path) -> GatewayConfig:
    """Return a minimal personal-tier GatewayConfig."""
    return GatewayConfig.from_toml_str(f"""
[gateway]
tier = "personal"
agent_did = "did:arc:agent:personal_test"
runtime_dir = "{tmp_path / 'run'}"
""")


# ---------------------------------------------------------------------------
# Tier selection tests
# ---------------------------------------------------------------------------


class TestTierExecutorSelection:
    """GatewayRunner.from_config() selects executor by tier."""

    def test_federal_tier_selects_subprocess_executor(self, tmp_path: Path) -> None:
        """tier=federal → GatewayRunner uses SubprocessExecutor."""
        config = _federal_config(tmp_path)
        runner = GatewayRunner.from_config(config)
        assert isinstance(runner._executor, SubprocessExecutor), (  # noqa: SLF001
            f"Federal tier must use SubprocessExecutor; got {type(runner._executor).__name__}"
        )

    def test_personal_tier_selects_asyncio_executor(self, tmp_path: Path) -> None:
        """tier=personal → GatewayRunner uses AsyncioExecutor."""
        config = _personal_config(tmp_path)
        runner = GatewayRunner.from_config(config)
        assert isinstance(runner._executor, AsyncioExecutor), (  # noqa: SLF001
            f"Personal tier must use AsyncioExecutor; got {type(runner._executor).__name__}"
        )

    def test_enterprise_tier_selects_asyncio_executor(self, tmp_path: Path) -> None:
        """tier=enterprise → GatewayRunner uses AsyncioExecutor."""
        config = GatewayConfig.from_toml_str(f"""
[gateway]
tier = "enterprise"
runtime_dir = "{tmp_path / 'run'}"
""")
        runner = GatewayRunner.from_config(config)
        assert isinstance(runner._executor, AsyncioExecutor), (  # noqa: SLF001
            f"Enterprise tier must use AsyncioExecutor; got {type(runner._executor).__name__}"
        )

    def test_runtime_dir_propagated_from_config(self, tmp_path: Path) -> None:
        """GatewayRunner.from_config uses config.gateway.runtime_dir."""
        config = _personal_config(tmp_path)
        runner = GatewayRunner.from_config(config)
        assert runner._runtime_dir == config.gateway.runtime_dir, (  # noqa: SLF001
            "runtime_dir must come from config"
        )


# ---------------------------------------------------------------------------
# SubprocessExecutor round-trip tests
# ---------------------------------------------------------------------------


class TestFederalTierSubprocessRoundTrip:
    """Federal tier: SubprocessExecutor spawns subprocess and receives deltas."""

    @pytest.mark.asyncio
    async def test_subprocess_spawned_for_federal_event(self, tmp_path: Path) -> None:
        """Federal tier spawns a subprocess and receives at least one Delta."""
        executor = SubprocessExecutor(
            worker_cmd=_WORKER_CMD,
        )
        event = _make_event()

        deltas: list[Delta] = []
        async with asyncio.timeout(_TIMEOUT):
            delta_iter = await executor.run(event)
            async for delta in delta_iter:
                deltas.append(delta)

        assert deltas, "SubprocessExecutor must yield at least one Delta"

        # Must end with a done sentinel
        done_deltas = [d for d in deltas if d.is_final]
        assert done_deltas, "Must have at least one is_final=True Delta"

    @pytest.mark.asyncio
    async def test_subprocess_done_sentinel_present(self, tmp_path: Path) -> None:
        """Federal tier delta stream ends with is_final=True done sentinel."""
        executor = SubprocessExecutor(worker_cmd=_WORKER_CMD)
        event = _make_event(message="check done sentinel")

        deltas: list[Delta] = []
        async with asyncio.timeout(_TIMEOUT):
            delta_iter = await executor.run(event)
            async for delta in delta_iter:
                deltas.append(delta)

        # Last delta must be final
        assert deltas, "Must have deltas"
        assert deltas[-1].is_final, (
            f"Last delta must have is_final=True; got: {deltas[-1]!r}"
        )

    @pytest.mark.asyncio
    async def test_subprocess_executor_used_via_runner(self, tmp_path: Path) -> None:
        """GatewayRunner.from_config federal tier uses SubprocessExecutor."""
        config = _federal_config(tmp_path)
        runner = GatewayRunner.from_config(config)
        executor = runner._executor  # noqa: SLF001

        assert isinstance(executor, SubprocessExecutor), (
            "Federal tier runner must use SubprocessExecutor"
        )

        # Drive one event through the executor to verify it spawns a subprocess
        event = _make_event()
        deltas: list[Delta] = []
        async with asyncio.timeout(_TIMEOUT):
            delta_iter = await executor.run(event)
            async for delta in delta_iter:
                deltas.append(delta)

        assert deltas, "Executor must yield deltas"
        assert any(d.is_final for d in deltas), "Must have done sentinel"

    @pytest.mark.asyncio
    async def test_subprocess_isolation_separate_pids(self, tmp_path: Path) -> None:
        """Each SubprocessExecutor.run() call spawns a distinct subprocess.

        Federal isolation requirement: every session gets its own process.
        We verify this by running two events and checking that the audit
        done sentinels carry different PID references.
        """
        executor = SubprocessExecutor(worker_cmd=_WORKER_CMD)

        async def collect_event(message: str) -> list[Delta]:
            ev = _make_event(message=message, session_key=f"sess_{message[:5]}")
            deltas: list[Delta] = []
            async with asyncio.timeout(_TIMEOUT):
                delta_iter = await executor.run(ev)
                async for d in delta_iter:
                    deltas.append(d)
            return deltas

        deltas1, deltas2 = await asyncio.gather(
            collect_event("first event"),
            collect_event("second event"),
        )

        # Extract PID from audit done sentinels (format: "[subprocess-audit] pid=N ...")
        def extract_pid(deltas: list[Delta]) -> int | None:
            for d in deltas:
                if d.is_final and "pid=" in d.content:
                    try:
                        pid_part = d.content.split("pid=")[1].split()[0]
                        return int(pid_part)
                    except (IndexError, ValueError):
                        pass
            return None

        pid1 = extract_pid(deltas1)
        pid2 = extract_pid(deltas2)

        # Both events must have produced PIDs
        assert pid1 is not None, f"Event 1 deltas have no PID: {deltas1}"
        assert pid2 is not None, f"Event 2 deltas have no PID: {deltas2}"

        # PIDs must differ — each event gets its own subprocess
        assert pid1 != pid2, (
            f"Federal isolation violated: both events used same PID {pid1}"
        )


# ---------------------------------------------------------------------------
# GatewayConfig tests
# ---------------------------------------------------------------------------


class TestGatewayConfig:
    """GatewayConfig TOML loading and defaults."""

    def test_defaults_are_personal_tier(self) -> None:
        """GatewayConfig() defaults to personal tier."""
        config = GatewayConfig()
        assert config.gateway.tier == "personal"

    def test_from_toml_str_federal(self, tmp_path: Path) -> None:
        """from_toml_str parses federal tier correctly."""
        config = _federal_config(tmp_path)
        assert config.gateway.tier == "federal"

    def test_from_toml_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        """from_toml on missing file returns defaults without raising."""
        config = GatewayConfig.from_toml(tmp_path / "nonexistent.toml")
        assert config.gateway.tier == "personal"

    def test_effective_agent_did_platform_override(self, tmp_path: Path) -> None:
        """Platform-level agent_did overrides gateway-level default."""
        config = GatewayConfig.from_toml_str("""
[gateway]
agent_did = "did:arc:agent:default"

[platforms.telegram]
enabled = true
token_env = "TELEGRAM_BOT_TOKEN"
agent_did = "did:arc:agent:telegram_specific"
""")
        assert config.effective_agent_did("telegram") == "did:arc:agent:telegram_specific"
        assert config.effective_agent_did("slack") == "did:arc:agent:default"

    def test_telegram_token_reads_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TelegramPlatformConfig.resolve_token reads from env var."""
        monkeypatch.setenv("MY_BOT_TOKEN", "test-token-abc123")
        config = GatewayConfig.from_toml_str("""
[platforms.telegram]
enabled = true
token_env = "MY_BOT_TOKEN"
""")
        assert config.platforms.telegram.resolve_token() == "test-token-abc123"

    def test_telegram_token_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """resolve_token returns None when env var is not set."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        config = GatewayConfig()
        assert config.platforms.telegram.resolve_token() is None
