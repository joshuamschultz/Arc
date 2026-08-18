"""The arcteam 'working as a team' guidance reaches the assembled system prompt.

Drives the REAL assembly path: a real :class:`ModuleBus` with the real
messaging and tasks ``agent:assemble_prompt`` hooks subscribed, run through
:meth:`ContextManager.assemble_system_prompt`. Nothing here mocks the injection
— the section only appears if the module hooks actually fire and write their
sections, and only the tools each loaded module owns are named.

This is the guard against teaching an agent to call tools it does not have:
messaging-only agents must not be told to ``assign_task``; tasks-only agents
must not be pointed at ``channel://``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.core.config import ContextConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal.context import ContextManager
from arcagent.modules.messaging import _runtime as messaging_runtime
from arcagent.modules.messaging.capabilities import inject_messaging_sections
from arcagent.modules.tasks.capabilities import inject_team_handoff_section


@pytest.fixture(autouse=True)
def _reset_messaging_runtime() -> Any:
    messaging_runtime.reset()
    yield
    messaging_runtime.reset()


def _configure_messaging(tmp_path: Path) -> None:
    """Bootstrap the messaging runtime the way the loader does at startup."""
    ident = AgentIdentity.generate(org="local", agent_type="agent")
    messaging_runtime.configure(
        config=make_config_dict(entity_id="agent://me", entity_name="me"),
        workspace=tmp_path,
        identity=ident,
        operator_signer=make_operator_signer(),
    )


def _context_manager(bus: ModuleBus) -> ContextManager:
    config = ContextConfig(max_tokens=4000)
    return ContextManager(config=config, telemetry=MagicMock(), bus=bus)


async def _assemble(bus: ModuleBus, tmp_path: Path) -> str:
    """Everything the model receives this turn: the cached system tiers plus the
    turn-attached material. The messaging ``teams`` section rides the turn tier
    (volatile mailbox/roster); the tasks ``handoffs`` section is run-tier."""
    assembled = await _context_manager(bus).assemble_system_prompt(tmp_path)
    return "\n\n".join((assembled.session, assembled.run, assembled.turn))


class TestArcteamGuidancePresent:
    async def test_both_modules_name_their_real_tools(self, tmp_path: Path) -> None:
        """messaging + tasks loaded → the prompt names every real team tool."""
        _configure_messaging(tmp_path)
        bus = ModuleBus()
        bus.subscribe("agent:assemble_prompt", inject_messaging_sections, priority=50)
        bus.subscribe("agent:assemble_prompt", inject_team_handoff_section, priority=60)

        prompt = await _assemble(bus, tmp_path)

        # Messaging tools (channels, DMs, mentions, threads).
        assert "messaging_send" in prompt
        assert "channel://" in prompt
        assert "@handle" in prompt
        # Tasks tools (handoff).
        assert "assign_task" in prompt
        assert "create_task" in prompt


class TestArcteamGuidanceGated:
    async def test_absent_when_no_team_modules_loaded(self, tmp_path: Path) -> None:
        """No hooks subscribed → no team-tool guidance leaks in."""
        bus = ModuleBus()
        prompt = await _assemble(bus, tmp_path)

        for token in ("assign_task", "create_task", "messaging_send", "channel://"):
            assert token not in prompt

    async def test_messaging_only_never_mentions_task_tools(self, tmp_path: Path) -> None:
        """Messaging without tasks: the agent has no assign_task/create_task."""
        _configure_messaging(tmp_path)
        bus = ModuleBus()
        bus.subscribe("agent:assemble_prompt", inject_messaging_sections, priority=50)

        prompt = await _assemble(bus, tmp_path)

        assert "messaging_send" in prompt
        assert "assign_task" not in prompt
        assert "create_task" not in prompt

    async def test_tasks_only_never_points_at_channels(self, tmp_path: Path) -> None:
        """Tasks without messaging: the agent has no channel:// surface."""
        bus = ModuleBus()
        bus.subscribe("agent:assemble_prompt", inject_team_handoff_section, priority=60)

        prompt = await _assemble(bus, tmp_path)

        assert "assign_task" in prompt
        assert "channel://" not in prompt
        assert "messaging_send" not in prompt
