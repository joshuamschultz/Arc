"""Gates on the memory module's operator seam and read-only tools.

Covers the refusal paths the happy-path suites do not reach:

* ``MemoryPromotionRun`` (SPEC-083 REQ-512, the "Run now" seam): a backend that
  cannot promote is a typed, safe-to-show refusal; a Mapping result passes through.
* ``procedure_list`` / ``procedure_get``: inactive memory and an ACL denial never
  reach the Brain, and a permitted read is audited.
* ``connected_sources`` / auto-resolved ``datastore_describe``: only what the
  operator connected and approved is surfaced, and nothing when connected-data
  is absent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.brain import NullBrain
from arcagent.core.errors import CapabilityUnavailableError
from arcagent.modules.connected_data import _runtime as connected_runtime
from arcagent.modules.connected_data.service import CatalogEntry
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import (
    MemoryPromotionRun,
    connected_sources,
    datastore_describe,
    procedure_get,
    procedure_list,
)
from arcagent.modules.memory.config import MemoryConfig

_DID = "did:arc:test-agent"


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    connected_runtime.reset()
    yield
    _runtime.reset()
    connected_runtime.reset()


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[str] = []

    def audit_event(self, event: str, detail: dict[str, Any]) -> None:
        del detail
        self.events.append(event)


class _ProcedureBrain:
    def __init__(self, *, allow: bool) -> None:
        self.allow = allow
        self.reads: list[str] = []

    async def authorize(self, operation: str, *, caller_did: str) -> bool:
        del operation, caller_did
        return self.allow

    async def list_procedures(self) -> str:
        self.reads.append("list")
        return "- weekly-report: when the operator asks for the weekly report"

    async def get_procedure(self, slug: str) -> str:
        self.reads.append(slug)
        return f"{slug}: 1. gather 2. send"


def _bind(brain: Any, telemetry: Any = None) -> None:
    _runtime.bind(
        _runtime._State(
            config=MemoryConfig(),
            brain=brain,
            workspace=Path("."),
            telemetry=telemetry,
            bus=None,
            agent_did=_DID,
            active=not isinstance(brain, NullBrain),
        )
    )


# -- MemoryPromotionRun ("Run now") ------------------------------------------


async def test_promotion_run_on_backend_without_promotion_is_a_typed_refusal() -> None:
    _bind(SimpleNamespace())  # a live Brain that exposes no run_promotion

    with pytest.raises(CapabilityUnavailableError) as info:
        await MemoryPromotionRun().run(agent_did=_DID)

    assert info.value.code == "MEMORY_PROMOTION_UNAVAILABLE"


async def test_promotion_run_returns_a_mapping_result_as_a_plain_dict() -> None:
    seen: list[int | None] = []

    async def run_promotion(*, max_items: int | None) -> dict[str, object]:
        seen.append(max_items)
        return {"status": "ok", "promoted": 2}

    _bind(SimpleNamespace(run_promotion=run_promotion))

    result = await MemoryPromotionRun().run(agent_did=_DID, max_items=5)

    assert result == {"status": "ok", "promoted": 2}
    assert type(result) is dict
    assert seen == [5]


async def test_promotion_run_for_an_unregistered_agent_fails_closed() -> None:
    _bind(SimpleNamespace(run_promotion=None))

    with pytest.raises(_runtime.MemoryIsolationError):
        await MemoryPromotionRun().run(agent_did="did:arc:someone-else")


# -- procedure tools ----------------------------------------------------------


async def test_procedure_tools_report_disabled_memory() -> None:
    _bind(NullBrain())

    assert await procedure_list() == "Memory is not enabled for this agent."
    assert await procedure_get("weekly-report") == "Memory is not enabled for this agent."


async def test_procedure_tools_denied_by_acl_never_read_the_brain() -> None:
    brain = _ProcedureBrain(allow=False)
    _bind(brain)

    assert await procedure_list() == "(no procedures recorded)"
    assert await procedure_get("weekly-report") == "(no procedure 'weekly-report')"
    assert brain.reads == []


async def test_procedure_tools_permitted_read_is_returned_and_audited() -> None:
    brain = _ProcedureBrain(allow=True)
    telemetry = _Telemetry()
    _bind(brain, telemetry)

    listing = await procedure_list()
    steps = await procedure_get("weekly-report")

    assert "weekly-report" in listing
    assert steps.startswith("weekly-report: 1. gather")
    assert brain.reads == ["list", "weekly-report"]
    assert telemetry.events == ["memory.procedure_listed", "memory.procedure_used"]


# -- connected sources --------------------------------------------------------


def _source(
    connection_id: str, kind: str, *, name: str = "", source_id: str = "", described: bool = True
) -> Any:
    description = SimpleNamespace(source_kind=kind, display_name=name) if described else None
    return SimpleNamespace(
        connection_id=connection_id,
        source_id=source_id,
        status="synced",
        description=description,
    )


class _ConnectedService:
    def __init__(self, sources: list[Any], proposals: dict[str, Any]) -> None:
        self._sources = sources
        self._proposals = proposals

    async def list_sources(self) -> list[Any]:
        return self._sources

    async def get_mapping_proposal(self, connection_id: str) -> Any:
        return self._proposals.get(connection_id)

    async def guide_context(self, *, source_ids: Any = None, run_key: str) -> str:
        """No operator guides written for these sources."""
        return ""

    async def catalog_entries(self, *, refresh: bool = False) -> tuple[CatalogEntry, ...]:
        """The service's own view: described sources with the homes they are mapped to."""
        entries = []
        for source in await self.list_sources():
            if source.description is None:
                continue
            proposal = self._proposals.get(source.connection_id)
            entries.append(
                CatalogEntry(
                    name=source.description.display_name or source.description.source_kind,
                    kind=source.description.source_kind,
                    status=source.status,
                    homes=tuple(proposal.homes) if proposal else (),
                )
            )
        return tuple(entries)


def _connect(service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connected_runtime, "state", lambda: SimpleNamespace(service=service))


async def test_connected_sources_without_the_connected_data_module_reports_none() -> None:
    assert await connected_sources() == "No connected sources are available."


async def test_connected_sources_lists_described_sources_with_their_homes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapped = SimpleNamespace(homes=(SimpleNamespace(value="company"),))
    service = _ConnectedService(
        [
            _source("c1", "confluence", name="Team wiki"),
            _source("c2", "gmail"),
            _source("c3", "jira", described=False),
        ],
        {"c1": mapped},
    )
    _connect(service, monkeypatch)

    out = await connected_sources()

    assert out.splitlines() == [
        "- Team wiki: confluence; status=synced; homes=company",
        "- gmail: gmail; status=synced; homes=not mapped",
    ]


async def test_datastore_describe_auto_resolves_only_approved_postgres_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = SimpleNamespace(approval_status="approved")
    pending = SimpleNamespace(approval_status="pending")
    service = _ConnectedService(
        [
            _source("pg-ok", "postgres", source_id="src-approved"),
            _source("pg-pending", "postgres", source_id="src-pending"),
            _source("wiki", "confluence", source_id="src-wiki"),
            _source("pg-undescribed", "postgres", source_id="src-x", described=False),
        ],
        {"pg-ok": approved, "pg-pending": pending, "wiki": approved},
    )
    _connect(service, monkeypatch)
    described: list[str] = []

    async def describe_datastore(source_id: str, *, table: str | None, caller_did: str) -> str:
        del table, caller_did
        described.append(source_id)
        return f"{source_id}: orders(amt = order total in USD)"

    _bind(SimpleNamespace(describe_datastore=describe_datastore))

    out = await datastore_describe()

    assert described == ["src-approved"]
    assert out == "src-approved: orders(amt = order total in USD)"


async def test_datastore_describe_with_no_approved_source_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connect(None, monkeypatch)

    async def describe_datastore(source_id: str, *, table: str | None, caller_did: str) -> str:
        raise AssertionError("nothing is approved, so nothing may be described")

    _bind(SimpleNamespace(describe_datastore=describe_datastore))

    assert await datastore_describe() == "No connected datastore found."
