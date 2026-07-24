"""COMP-002 / REQ-127 / REQ-138: PromptDocument frontmatter model + newline identity."""

from __future__ import annotations

import pytest
from arctrust.artifact import content_sha256

from arcprompt.document import parse_prompt, render_prompt
from arcprompt.errors import PromptUnparseable


def test_parses_name_description_tunable() -> None:
    raw = render_prompt("do the thing", name="p", description="d", tunable=False)
    doc = parse_prompt(raw, source="stock")
    assert doc.name == "p"
    assert doc.description == "d"
    assert doc.tunable is False
    assert doc.body == "do the thing"
    assert doc.source == "stock"


def test_sha256_is_derived_from_raw_bytes_not_authored() -> None:
    raw = render_prompt("body", name="p", description="d")
    doc = parse_prompt(raw, source="stock")
    assert doc.sha256 == content_sha256(raw)


def test_authored_version_field_is_ignored_not_honored() -> None:
    raw = b"---\nname: p\ndescription: d\nversion: 99.9\n---\nbody text\n"
    doc = parse_prompt(raw, source="stock")
    # No 'version' attribute exists; the field is dropped, identity stays the digest.
    assert not hasattr(doc, "version")
    assert doc.sha256 == content_sha256(raw)


@pytest.mark.parametrize(
    "constant",
    [
        "no trailing newline",
        "trailing newline\n",
        "two trailing newlines\n\n",
        "multi\nline\nbody\nwith internal newlines",
        "  leading and trailing spaces preserved  ",
    ],
)
def test_render_then_parse_is_byte_identical(constant: str) -> None:
    """The newline rule must round-trip any constant (REQ-138)."""
    raw = render_prompt(constant, name="p", description="d")
    assert parse_prompt(raw, source="stock").body == constant


def test_workpad_backslash_continuation_case() -> None:
    """A constant whose author suppressed a leading newline has no leading blank line."""
    constant = "You maintain context.md. Keep it an open-loops cockpit."
    raw = render_prompt(constant, name="context_maintainer", description="d")
    assert parse_prompt(raw, source="stock").body == constant
    assert not parse_prompt(raw, source="stock").body.startswith("\n")


def test_empty_body_raises() -> None:
    raw = b"---\nname: p\ndescription: d\n---\n\n"
    with pytest.raises(PromptUnparseable):
        parse_prompt(raw, source="stock")


def test_missing_frontmatter_raises() -> None:
    with pytest.raises(PromptUnparseable):
        parse_prompt(b"just a body, no frontmatter", source="stock")


def test_unterminated_frontmatter_raises() -> None:
    with pytest.raises(PromptUnparseable):
        parse_prompt(b"---\nname: p\ndescription: d\nbody with no close", source="stock")


def test_frontmatter_not_a_mapping_raises() -> None:
    with pytest.raises(PromptUnparseable):
        parse_prompt(b"---\n- just\n- a\n- list\n---\nbody\n", source="stock")


def test_missing_required_field_raises() -> None:
    with pytest.raises(PromptUnparseable):
        parse_prompt(b"---\nname: p\n---\nbody\n", source="stock")


def test_invalid_yaml_raises() -> None:
    with pytest.raises(PromptUnparseable):
        parse_prompt(b"---\nname: : : bad\ndescription: d\n---\nbody\n", source="stock")


def test_non_utf8_raises() -> None:
    with pytest.raises(PromptUnparseable):
        parse_prompt(b"---\nname: p\ndescription: d\n---\n\xff\xfe body\n", source="stock")
