from __future__ import annotations

import pytest
from arcokf import OKFValidationError, parse

from arcmemory.mdfile import parse_document, render_document


def test_curated_memory_render_is_valid_okf_and_keeps_graph_links() -> None:
    encoded = render_document({"title": "Arc"}, "# Arc\n\nSee [[other]].")

    document = parse(encoded)
    metadata, body = parse_document(encoded)
    assert metadata["type"] == "ArcMemory"
    assert "[other](other.md)" in document.body
    assert "[[other]]" in body


def test_curated_memory_read_fails_closed_for_malformed_okf() -> None:
    with pytest.raises(OKFValidationError):
        parse_document("---\ntype: [invalid]\n---\nbody\n")
