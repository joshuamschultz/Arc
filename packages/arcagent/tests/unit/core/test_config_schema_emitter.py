"""Tests for ConfigSchemaEmitter (SPEC-085 COMP-001, REQ-458/466).

``arcagent.core.config_schema.emit_form_schema()`` walks the root
``ArcAgentConfig`` pydantic model (via ``model_fields``) and returns a plain,
JSON-serializable ``FormSchema``: ``{"sections": [{"id": ..., "fields": [...]}]}``.

Each field dict carries:
  - ``id``: dotted path, e.g. ``"security.tier"``
  - ``type``: derived from the annotation (``"bool"``/``"int"``/``"float"``/
    ``"str"``/``"list"``/``"dict"``/``"nested"``)
  - ``help``: the pydantic ``Field(description=...)`` text, or ``""`` when
    the field is bare — the emitter must never crash on a bare field.
  - ``enum``: ``None`` when not derivable from the annotation (there are no
    ``Enum``/``Literal`` fields in this config today, so it is always
    ``None`` at present).
  - ``constraints``: ``{"min": ..., "max": ...}`` from ``ge``/``le`` when
    present on a numeric field, else ``None``.
  - ``tier`` / ``advanced``: gating metadata (values not pinned here).
  - ``editable``: ``False`` when the dotted path — joined with ``"__"`` the
    way ``ARCAGENT_`` env overrides are, per
    ``config_loading._ENV_DENYLIST_PREFIXES`` — matches a denylisted prefix.

This module (``arcagent.core.config_schema``) does not exist yet. Every
failure below must be an ImportError/AttributeError for the ABSENT module,
not a bug in these tests.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from arcagent.core.config_schema import emit_form_schema


def _find_field(schema: dict[str, Any], field_id: str) -> dict[str, Any]:
    """Locate a field dict by its dotted ``id`` across all sections."""
    for section in schema["sections"]:
        for field in section["fields"]:
            if field["id"] == field_id:
                return field
    raise AssertionError(f"field {field_id!r} not found in emitted schema")


def _find_section(schema: dict[str, Any], section_id: str) -> dict[str, Any]:
    for section in schema["sections"]:
        if section["id"] == section_id:
            return section
    raise AssertionError(f"section {section_id!r} not found in emitted schema")


def _assert_json_safe(value: Any) -> None:
    """Recursively assert every node is a plain JSON-serializable primitive.

    Fails on anything that is a pydantic BaseModel or other SDK object —
    the emitted schema must be plain data, not model instances.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), f"non-string dict key: {key!r}"
            _assert_json_safe(item)
        return
    raise AssertionError(f"non-JSON-safe value leaked into schema: {value!r} ({type(value)!r})")


def test_emit_form_schema_has_sections_for_nested_models() -> None:
    """(a) Nested config models (e.g. SecurityConfig) become sections."""
    schema = emit_form_schema()

    assert "sections" in schema
    section_ids = [section["id"] for section in schema["sections"]]
    assert "security" in section_ids


def test_field_with_description_carries_help_text() -> None:
    """(b) security.tier has a Field(description=...) — help must match it."""
    schema = emit_form_schema()

    field = _find_field(schema, "security.tier")
    assert field["help"] == (
        "Deployment tier: 'federal', 'enterprise', or 'personal'. "
        "Controls credential resolution, memory ACL defaults, and "
        "executor selection."
    )
    assert field["type"] == "str"


def test_bare_field_without_description_emits_empty_help_and_does_not_crash() -> None:
    """(c) security.clearance is a bare `str = "UNCLASSIFIED"` — no description."""
    schema = emit_form_schema()

    field = _find_field(schema, "security.clearance")
    assert field["help"] == ""


def test_numeric_field_with_ge_le_carries_constraints() -> None:
    """(d) tools.policy.timeout_seconds: Field(default=120, ge=1, le=600)."""
    schema = emit_form_schema()

    field = _find_field(schema, "tools.policy.timeout_seconds")
    assert field["constraints"] is not None
    assert field["constraints"]["min"] == 1
    assert field["constraints"]["max"] == 600


def test_numeric_field_with_only_ge_has_none_max() -> None:
    """spawn.max_depth: Field(default=3, ge=0, description=...) — no le."""
    schema = emit_form_schema()

    field = _find_field(schema, "spawn.max_depth")
    assert field["constraints"] is not None
    assert field["constraints"]["min"] == 0
    assert field["constraints"]["max"] is None
    assert field["help"] == "Maximum nesting depth for spawned children."


def test_non_numeric_field_has_no_constraints() -> None:
    field = _find_field(emit_form_schema(), "security.tier")
    assert field["constraints"] is None


def test_denylisted_field_is_not_editable() -> None:
    """(e) identity.key_dir maps to the env-override denylist entry
    ``identity__key_dir`` in ``config_loading._ENV_DENYLIST_PREFIXES`` —
    it must never be exposed as editable in the generated form.
    """
    schema = emit_form_schema()

    field = _find_field(schema, "identity.key_dir")
    assert field["editable"] is False


def test_non_denylisted_field_is_editable() -> None:
    schema = emit_form_schema()

    field = _find_field(schema, "security.tier")
    assert field["editable"] is True


def test_schema_is_plain_json_serializable_data() -> None:
    """(f) No pydantic/SDK objects leak — the schema is plain dict/list data."""
    schema = emit_form_schema()

    assert isinstance(schema, dict)
    _assert_json_safe(schema)

    # Round-trips cleanly through json without a custom encoder.
    serialized = json.dumps(schema)
    assert json.loads(serialized) == schema


def test_every_field_has_the_required_keys() -> None:
    schema = emit_form_schema()

    required_keys = {"id", "type", "help", "enum", "constraints", "tier", "advanced", "editable"}
    security_section = _find_section(schema, "security")
    assert len(security_section["fields"]) > 0
    for field in security_section["fields"]:
        assert required_keys.issubset(field.keys())


def test_emitter_does_not_require_arguments() -> None:
    """Grounding: the intended API is a zero-argument call that always walks
    the canonical ``ArcAgentConfig`` class — not an instance."""
    with pytest.raises(TypeError):
        emit_form_schema(object())  # type: ignore[call-arg]
