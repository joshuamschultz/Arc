"""The connected-data module surfaces its own catalog into the prompt.

A deployment's connected sources are invisible to the agent unless something
tells it they exist — the exact gap behind an agent answering "nothing in
connected sources references X" while 47 indexed Slack channels sat unsearched.
This module owns that surface: a lean, always-cheap catalog injected into
``sections["connections"]``, present only when sources are connected, so a
deployment with no connectors keeps a clean prompt.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.modules.connected_data import _runtime
from arcagent.modules.connected_data.capabilities import inject_connections_catalog


def _ctx(data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(data=data)


class _Home:
    def __init__(self, value: str) -> None:
        self.value = value


class _Proposal:
    def __init__(self, *homes: str) -> None:
        self.homes = [_Home(h) for h in homes]


class _Status:
    def __init__(self, connection_id: str, display_name: str, kind: str, status: str) -> None:
        self.connection_id = connection_id
        self.description = SimpleNamespace(display_name=display_name, source_kind=kind)
        self.status = status


class _FakeService:
    def __init__(self, statuses: list[_Status], homes: dict[str, _Proposal]) -> None:
        self._statuses = statuses
        self._homes = homes

    async def list_sources(self) -> list[_Status]:
        return self._statuses

    async def get_mapping_proposal(self, connection_id: str) -> _Proposal | None:
        return self._homes.get(connection_id)


def _configure_with(service: Any) -> None:
    _runtime.configure(agent_did="did:arc:catalog-test")
    _runtime.state().service = service


@pytest.mark.asyncio
async def test_catalog_lists_connected_sources_with_a_nudge_to_search() -> None:
    _configure_with(
        _FakeService(
            [
                _Status("slack", "Slack", "slack", "synced"),
                _Status("crm", "CRM", "postgres", "synced"),
            ],
            {"slack": _Proposal("document"), "crm": _Proposal("datastore")},
        )
    )
    sections: dict[str, str] = {}

    await inject_connections_catalog(_ctx({"sections": sections}))

    block = sections["connections"]
    assert "Slack" in block and "slack" in block and "synced" in block
    assert "document" in block and "datastore" in block
    # The nudge: lean toward searching them before declaring something unknown.
    assert "document_search" in block
    assert "undocumented" in block or "unknown" in block


@pytest.mark.asyncio
async def test_no_section_when_no_sources_are_connected() -> None:
    _configure_with(_FakeService([], {}))
    sections: dict[str, str] = {}

    await inject_connections_catalog(_ctx({"sections": sections}))

    assert "connections" not in sections


@pytest.mark.asyncio
async def test_no_section_when_the_module_has_no_service() -> None:
    _configure_with(None)
    sections: dict[str, str] = {}

    await inject_connections_catalog(_ctx({"sections": sections}))

    assert "connections" not in sections
