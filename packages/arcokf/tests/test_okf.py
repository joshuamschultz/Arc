from __future__ import annotations

import pytest
from arcokf import DiagnosticCode, OKFValidationError, parse, render, validate


def test_round_trip_preserves_unknown_metadata() -> None:
    source = "---\ntype: Entity\ntitle: Test\ncustom:\n  value: 3\n---\n# Body\n"
    document = parse(source)
    output = render(document)
    assert "custom:\n  value: 3" in output
    assert parse(output).metadata["custom"] == {"value": 3}


def test_missing_type_has_structured_diagnostic() -> None:
    result = validate("---\ntitle: Missing\n---\nBody\n", path="note.md")
    assert not result.valid
    assert result.diagnostics[0].code is DiagnosticCode.MISSING_TYPE


def test_reserved_index_and_log_are_not_concept_documents() -> None:
    assert validate("# Index\n", path="index.md").valid
    assert validate("# Log\n", path="log.md").valid
    assert validate("---\ntype: Entity\n---\n", path="index.md").valid is False


def test_wiki_links_are_rejected() -> None:
    result = validate("---\ntype: Entity\n---\nSee [[other]].\n", path="x.md")
    assert not result.valid
    assert any(item.code is DiagnosticCode.WIKI_LINK for item in result.diagnostics)


def test_standard_relative_links_are_accepted() -> None:
    result = validate("---\ntype: Entity\n---\nSee [other](../other.md).\n", path="x.md")
    assert result.valid


def test_alias_limit_has_typed_diagnostic() -> None:
    aliases = "\n".join(f"item_{index}: *base" for index in range(17))
    result = validate("---\ntype: Entity\nbase: &base value\n" + aliases + "\n---\n")
    assert not result.valid
    assert any(item.code is DiagnosticCode.TOO_MANY_ALIASES for item in result.diagnostics)


def test_nesting_limit_has_typed_diagnostic() -> None:
    nested = "value: end\n"
    for _ in range(33):
        nested = "level:\n  " + nested.replace("\n", "\n  ")
    result = validate("---\ntype: Entity\n" + nested.rstrip() + "\n---\n")
    assert not result.valid
    assert any(item.code is DiagnosticCode.NESTING_TOO_DEEP for item in result.diagnostics)


@pytest.mark.parametrize(
    "source",
    ["---\ntype: [bad]\n---\n", "---\ntype: Entity\n---\n" + ("x" * 2_000_001)],
)
def test_invalid_documents_raise_typed_errors(source: str) -> None:
    with pytest.raises(OKFValidationError):
        parse(source)


def test_duplicate_yaml_keys_are_rejected() -> None:
    with pytest.raises(OKFValidationError):
        parse("---\ntype: Entity\ntype: Other\n---\n")


def test_invalid_utf8_is_rejected() -> None:
    with pytest.raises(OKFValidationError):
        parse(b"---\ntype: Entity\n---\n\xff")
