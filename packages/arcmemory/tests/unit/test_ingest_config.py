"""COMP-014 — MemoryConfig ingestion/doc-search fields + toggles (T-1022/T-1023).

Every new field is asserted against the exact CONTRACTS_P1 default, each
capability off-switch must be representable as False, and federal ``for_tier``
must tighten the ingest caps relative to personal (the existing stringency
pattern, never a special-cased branch in the algorithms themselves).
"""

from __future__ import annotations

from arcmemory.config import MemoryConfig


def test_doc_search_and_ingest_fields_have_contract_defaults() -> None:
    cfg = MemoryConfig()

    assert cfg.doc_chunk_tokens == 512
    assert cfg.doc_chunk_overlap == 0.10
    assert cfg.doc_rerank_margin == 0.05
    assert cfg.ingest_max_batch == 1000
    assert cfg.backfill_max_age_days == 90
    assert cfg.backfill_max_object_bytes == 10_485_760
    assert cfg.backfill_max_total_bytes == 5_368_709_120
    assert cfg.backfill_max_objects == 50_000
    assert cfg.index_backend == "sqlite"
    assert cfg.doc_search_enabled is True
    assert cfg.datastore_enabled is True
    assert cfg.source_sync_enabled is True


def test_each_capability_toggle_is_representable_as_false() -> None:
    cfg = MemoryConfig(
        doc_search_enabled=False,
        datastore_enabled=False,
        source_sync_enabled=False,
    )

    assert cfg.doc_search_enabled is False
    assert cfg.datastore_enabled is False
    assert cfg.source_sync_enabled is False


def test_federal_tier_tightens_ingest_caps_versus_personal() -> None:
    personal = MemoryConfig.for_tier("personal")
    federal = MemoryConfig.for_tier("federal")

    assert federal.ingest_max_batch < personal.ingest_max_batch
    assert federal.backfill_max_age_days < personal.backfill_max_age_days
