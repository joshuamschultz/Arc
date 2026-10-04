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

from arcagent.connected_data import KnowledgeHome
from arcagent.modules.connected_data import _runtime
from arcagent.modules.connected_data.capabilities import inject_connections_catalog
from arcagent.modules.connected_data.service import CatalogEntry


def _ctx(data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(data=data)


class _FakeService:
    """The service's cached catalog view: the prompt reads nothing else."""

    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries = entries

    async def catalog_entries(self, *, refresh: bool = False) -> tuple[CatalogEntry, ...]:
        assert not refresh, "the prompt must never ask the service to refresh"
        return tuple(self._entries)


def _entry(name: str, kind: str, status: str, *homes: KnowledgeHome) -> CatalogEntry:
    return CatalogEntry(name=name, kind=kind, status=status, homes=homes)


def _configure_with(service: Any) -> None:
    _runtime.configure(agent_did="did:arc:catalog-test")
    _runtime.state().service = service


@pytest.mark.asyncio
async def test_catalog_lists_connected_sources_with_a_nudge_to_search() -> None:
    _configure_with(
        _FakeService(
            [
                _entry("Slack", "slack", "synced", KnowledgeHome.DOCUMENT),
                _entry("CRM", "postgres", "synced", KnowledgeHome.DATASTORE),
            ]
        )
    )
    sections: dict[str, str] = {}

    await inject_connections_catalog(_ctx({"sections": sections}))

    block = sections["connections"]
    assert "Slack" in block and "slack" in block
    assert "status=" not in block, "sync status is a turn-tier section, not the cached catalog"
    assert sections["connection_status"] == "- Slack: synced\n- CRM: synced"
    assert "document" in block and "datastore" in block
    # The nudge: lean toward searching them before declaring something unknown.
    assert "document_search" in block
    assert "undocumented" in block or "unknown" in block


@pytest.mark.asyncio
async def test_no_section_when_no_sources_are_connected() -> None:
    _configure_with(_FakeService([]))
    sections: dict[str, str] = {}

    await inject_connections_catalog(_ctx({"sections": sections}))

    assert "connections" not in sections


@pytest.mark.asyncio
async def test_no_section_when_the_module_has_no_service() -> None:
    _configure_with(None)
    sections: dict[str, str] = {}

    await inject_connections_catalog(_ctx({"sections": sections}))

    assert "connections" not in sections
