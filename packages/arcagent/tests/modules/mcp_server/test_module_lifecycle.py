"""SPEC-082 /review backfill — the mcp_server module runtime + capability lifecycle.

The review found ``_runtime.py`` and ``capabilities.py`` at 0% coverage: the
ContextVar-bound state machine that every mcp_server capability reads, and the
``McpServerDoor`` setup/teardown lifecycle, had never executed under test. These
are the load-bearing pieces that keep per-agent state from leaking across agents
(the reason the module uses a ContextVar, not a module global — see ``_runtime``'s
docstring), so they are exactly what must be proven.

Covered here:

``_runtime``
  - ``configure()`` binds a ``_State`` from a ``McpServerConfig`` and from a plain
    dict;
  - ``state()`` raises ``RuntimeError`` before ``configure()``;
  - ``bind()`` installs a prebuilt state and ``reset()`` clears it;
  - two concurrent, forcibly-interleaved asyncio tasks each see only their own
    state (the isolation the ContextVar exists to guarantee).

``capabilities.McpServerDoor``
  - ``setup()`` builds an ``McpServer`` from the configured tool registry;
  - ``setup()`` is idempotent when a server already exists;
  - ``setup()`` logs and no-ops when the tool registry is ``None``;
  - ``teardown()`` clears the server so a re-``setup()`` rebuilds it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from arcagent.modules.mcp_server import _runtime
from arcagent.modules.mcp_server._runtime import _State
from arcagent.modules.mcp_server.capabilities import McpServerDoor
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.server import McpServer


@pytest.fixture(autouse=True)
def _clean_runtime() -> Any:
    """Every test starts and ends with an unconfigured runtime — no state bleed."""
    _runtime.reset()
    yield
    _runtime.reset()


@dataclass(frozen=True)
class _FakeTool:
    name: str
    description: str
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


@dataclass
class _FakeRegistry:
    tools: dict[str, _FakeTool]


def _registry() -> _FakeRegistry:
    return _FakeRegistry(tools={"read_file": _FakeTool("read_file", "Read a file")})


# --- _runtime ----------------------------------------------------------------


def test_configure_from_model_binds_the_state() -> None:
    """``configure`` accepts a ready ``McpServerConfig`` and binds it verbatim."""
    registry: Any = _registry()
    _runtime.configure(
        config=McpServerConfig(enabled=True, page_size=7),
        tool_registry=registry,
        agent_name="probe",
    )

    st = _runtime.state()
    assert isinstance(st.config, McpServerConfig)
    assert st.config.page_size == 7
    assert st.tool_registry is registry
    assert st.agent_name == "probe"
    assert st.server is None


def test_configure_from_dict_builds_the_config() -> None:
    """``configure`` accepts a raw dict and constructs the ``McpServerConfig``."""
    _runtime.configure(config={"expose": ["read_file"], "server_name": "srv"})

    st = _runtime.state()
    assert isinstance(st.config, McpServerConfig)
    assert st.config.expose == ["read_file"]
    assert st.config.server_name == "srv"


def test_state_raises_before_configure() -> None:
    """Reading state before ``configure`` fails loud — the module is unusable idle."""
    with pytest.raises(RuntimeError, match="before runtime is configured"):
        _runtime.state()


def test_bind_installs_a_prebuilt_state_and_reset_clears_it() -> None:
    """``bind`` idempotently installs a built ``_State``; ``reset`` removes it."""
    prebuilt = _State(config=McpServerConfig(enabled=True), agent_name="bound")

    _runtime.bind(prebuilt)
    assert _runtime.state() is prebuilt

    _runtime.reset()
    with pytest.raises(RuntimeError):
        _runtime.state()


@pytest.mark.asyncio
async def test_state_is_isolated_across_concurrent_tasks() -> None:
    """Two interleaved tasks each configure and read only their own state.

    A :class:`asyncio.Barrier` forces both tasks to finish ``configure()`` before
    either reads back, so a shared-global implementation would let the second write
    clobber the first — the ContextVar keeps each task's state private.
    """
    barrier = asyncio.Barrier(2)
    seen: dict[str, str] = {}

    async def worker(name: str) -> None:
        _runtime.configure(config={}, agent_name=name)
        await barrier.wait()  # both have configured before anyone reads
        seen[name] = _runtime.state().agent_name

    await asyncio.gather(worker("agent-A"), worker("agent-B"))

    assert seen == {"agent-A": "agent-A", "agent-B": "agent-B"}


# --- capabilities.McpServerDoor ----------------------------------------------


@pytest.mark.asyncio
async def test_setup_builds_the_server_from_the_registry() -> None:
    """``setup`` builds an ``McpServer`` that lists the configured registry's tools."""
    _runtime.configure(config={"expose": ["*"]}, tool_registry=_registry())  # type: ignore[arg-type]

    await McpServerDoor().setup(None)

    server = _runtime.state().server
    assert isinstance(server, McpServer)
    assert [tool["name"] for tool in server.list_tools().tools] == ["read_file"]


@pytest.mark.asyncio
async def test_setup_is_idempotent_when_a_server_exists() -> None:
    """A second ``setup`` leaves the already-built server untouched (no rebuild)."""
    _runtime.configure(config={"expose": ["*"]}, tool_registry=_registry())  # type: ignore[arg-type]
    door = McpServerDoor()

    await door.setup(None)
    first = _runtime.state().server
    await door.setup(None)

    assert _runtime.state().server is first


@pytest.mark.asyncio
async def test_setup_without_a_registry_logs_and_stays_idle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no tool registry the door logs a warning and never builds a server."""
    _runtime.configure(config={"expose": ["*"]}, tool_registry=None)

    with caplog.at_level(logging.WARNING, logger="arcagent.modules.mcp_server.capabilities"):
        await McpServerDoor().setup(None)

    assert _runtime.state().server is None
    assert any("no tool registry" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_teardown_clears_the_server_so_setup_rebuilds() -> None:
    """``teardown`` drops the server; a fresh ``setup`` builds a new one."""
    _runtime.configure(config={"expose": ["*"]}, tool_registry=_registry())  # type: ignore[arg-type]
    door = McpServerDoor()

    await door.setup(None)
    first = _runtime.state().server
    assert first is not None

    await door.teardown()
    assert _runtime.state().server is None

    await door.setup(None)
    rebuilt = _runtime.state().server
    assert isinstance(rebuilt, McpServer)
    assert rebuilt is not first
