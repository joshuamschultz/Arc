"""COMP-001 — source adapter + Router fan-out (T-1024/T-1025).

The ``Router`` applies an approved ``SourceMapping`` and fans records to
one-or-more home sinks. Exercised with recording fake sinks (append-only lists)
so fan-out, single-home routing, and the inert-empty-mapping case are asserted
on real call counts, not a mock's ``assert_called`` report.
"""

from __future__ import annotations

from arcmemory.router import Router
from arcmemory.types import SourceMapping, SourceRecord

_Calls = list[tuple[str, list[SourceRecord]]]


def _records() -> list[SourceRecord]:
    return [SourceRecord(external_id="r1", text="hello")]


def _recording_sink(calls: _Calls):
    async def sink(source_id: str, records: list[SourceRecord]) -> None:
        calls.append((source_id, records))

    return sink


async def test_route_mapping_with_two_homes_reaches_both_sinks() -> None:
    memory_calls: _Calls = []
    document_calls: _Calls = []
    router = Router(
        {"memory": _recording_sink(memory_calls), "document": _recording_sink(document_calls)}
    )
    records = _records()
    mapping = SourceMapping(source_id="source-a", homes=["memory", "document"])

    await router.route("source-a", records, mapping)

    assert memory_calls == [("source-a", records)]
    assert document_calls == [("source-a", records)]


async def test_route_datastore_only_mapping_reaches_only_that_sink() -> None:
    memory_calls: _Calls = []
    datastore_calls: _Calls = []
    router = Router(
        {"memory": _recording_sink(memory_calls), "datastore": _recording_sink(datastore_calls)}
    )
    records = _records()
    mapping = SourceMapping(source_id="source-b", homes=["datastore"])

    await router.route("source-b", records, mapping)

    assert datastore_calls == [("source-b", records)]
    assert memory_calls == []


async def test_route_empty_homes_mapping_calls_no_sink() -> None:
    memory_calls: _Calls = []
    router = Router({"memory": _recording_sink(memory_calls)})
    mapping = SourceMapping(source_id="source-c", homes=[])

    await router.route("source-c", _records(), mapping)

    assert memory_calls == []


async def test_route_home_with_no_registered_sink_is_skipped_not_an_error() -> None:
    router = Router({})  # no sinks registered at all
    mapping = SourceMapping(source_id="source-d", homes=["memory"])

    await router.route("source-d", _records(), mapping)  # must not raise
