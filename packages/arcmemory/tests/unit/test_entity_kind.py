"""Entity kind taxonomy: one canonical type, topical-only tags (arcmemory.entity_kind)."""

from __future__ import annotations

import pytest

from arcmemory.entity_kind import (
    clean_tags,
    infer_kind,
    kind_rank,
    kinds_compatible,
    more_specific_kind,
    normalize_kind,
    numbered_label,
)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("person", "person"),
        ("People", "person"),
        ("company", "company"),
        ("organization", "company"),
        ("project", "project"),
        ("projects", "project"),
        ("thesis", "thesis"),
        ("business-idea", "concept"),
        ("insight", "concept"),
        ("note", "other"),
        ("thing", "other"),
        ("unknown", "other"),
        ("", "other"),
        ("email_account", "account"),
        ("tool", "product"),
        ("opportunity", "deal"),
        ("wibble", "other"),
        ("source", "source"),
        ("mapping", "mapping"),
        ("db_table", "db_table"),
        ("blob_folder", "blob_folder"),
    ],
)
def test_normalize_kind_folds_free_text_onto_the_vocabulary(raw: str, kind: str) -> None:
    assert normalize_kind(raw) == kind


def test_rank_orders_vague_below_generic_below_specific() -> None:
    assert kind_rank("thing") == kind_rank("note") == kind_rank("unknown") == 0
    assert kind_rank("concept") == kind_rank("insight") == 1
    for specific in ("document", "person", "company", "project"):
        assert kind_rank(specific) == 2
    assert kind_rank("thesis") == 3  # a thesis refines a document


def test_more_specific_kind_keeps_the_specific_one() -> None:
    assert more_specific_kind(["thing", "thesis"]) == "thesis"
    assert more_specific_kind(["business-idea", "project"]) == "project"
    assert more_specific_kind(["note", "insight"]) == "concept"
    assert more_specific_kind([]) == "other"


def test_identity_kinds_are_mutually_exclusive() -> None:
    assert not kinds_compatible("person", "place")
    assert not kinds_compatible("person", "project")
    assert not kinds_compatible("company", "person")
    assert kinds_compatible("thing", "person")
    assert kinds_compatible("thesis", "project")
    assert kinds_compatible("business-idea", "project")
    assert not kinds_compatible("source", "source")  # system cards never merge


def test_clean_tags_drops_restatements_of_any_kind() -> None:
    tags = ["company", "people", "Projects", "product", "thesis", "knowledge", "federal", "AI"]
    assert clean_tags(tags) == ["federal", "AI"]


def test_clean_tags_dedupes_case_insensitively_and_keeps_order() -> None:
    assert clean_tags(["DOE", "doe", " nnl ", ""]) == ["DOE", "nnl"]


def test_infer_kind_recovers_the_kind_a_legacy_tag_carried() -> None:
    assert infer_kind("thing", ["thesis"], "Harness Advantage") == "thesis"
    assert infer_kind("thing", ["product"], "Browserbase") == "product"
    assert infer_kind("entity", ["people"], "Ann") == "person"
    assert infer_kind("unknown", [], "Ai Thesis 16 Model Routing") == "thesis"
    assert infer_kind("unknown", [], "Ai Concepts") == "other"
    assert infer_kind("person", ["project"], "Sam") == "person"  # a specific type stays
    assert infer_kind("source", [], "Source 12ab") == "source"


def test_numbered_label_reads_series_members() -> None:
    assert numbered_label("Thesis 5: Multi-Layer Tuning") == ("thesis", "5")
    assert numbered_label("ai-thesis-16-model-routing") == ("thesis", "16")
    assert numbered_label("AI Concept 1: Data as your Moat") == ("concept", "1")
    assert numbered_label("Thesis 05") == ("thesis", "5")
    assert numbered_label("Theses tracker") is None
    assert numbered_label("Brad Baker") is None
