"""Tests for SettingsReadModel (SPEC-085 COMP-002, REQ-459).

``arcagent.core.settings_read_model.settings_read_model(config, config_path)``
composes on ``config_schema.emit_form_schema()``'s section/field walk to
report each setting's *effective* value plus whether it came from a TOML
file or is still at its pydantic default (overlay-source detection is a
Phase-2 hook, not implemented here).

This module does not exist yet. Every failure below must be an ImportError
for the ABSENT module, not a bug in these tests.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from arcagent.core.config import load_config
from arcagent.core.settings_read_model import settings_read_model


@pytest.fixture(autouse=True)
def _isolated_user_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the user-wide config root at an empty dir so the real ~/.arc can't leak."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path_factory.mktemp("empty-arc")))


@pytest.fixture()
def minimal_toml(tmp_path: Path) -> Path:
    (tmp_path / "arcagent.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "minimal"
    """)
    )
    (tmp_path / "arcllm.toml").write_text(
        textwrap.dedent("""\
        [llm]
        model = "anthropic/claude-sonnet-4-5-20250929"
    """)
    )
    return tmp_path / "arcagent.toml"


@pytest.fixture()
def tier_set_toml(tmp_path: Path) -> Path:
    """Per-agent TOML explicitly sets security.tier away from its pydantic default."""
    (tmp_path / "arcagent.toml").write_text(
        textwrap.dedent("""\
        [agent]
        name = "tier-agent"

        [security]
        tier = "enterprise"
    """)
    )
    (tmp_path / "arcllm.toml").write_text(
        textwrap.dedent("""\
        [llm]
        model = "anthropic/claude-sonnet-4-5-20250929"
    """)
    )
    return tmp_path / "arcagent.toml"


def _find_field(model: dict[str, Any], field_id: str) -> dict[str, Any]:
    for section in model["sections"]:
        for field in section["fields"]:
            if field["id"] == field_id:
                return field
    raise AssertionError(f"field {field_id!r} not found in read model")


def test_value_left_at_default_reports_source_default(minimal_toml: Path) -> None:
    config = load_config(minimal_toml)

    model = settings_read_model(config, minimal_toml)

    field = _find_field(model, "security.tier")
    assert field["value"] == "personal"
    assert field["source"] == "default"


def test_value_set_in_toml_reports_source_file(tier_set_toml: Path) -> None:
    config = load_config(tier_set_toml)

    model = settings_read_model(config, tier_set_toml)

    field = _find_field(model, "security.tier")
    assert field["value"] == "enterprise"
    assert field["source"] == "file"


def test_every_field_has_id_value_and_source(minimal_toml: Path) -> None:
    config = load_config(minimal_toml)

    model = settings_read_model(config, minimal_toml)

    security_section = next(s for s in model["sections"] if s["id"] == "security")
    assert len(security_section["fields"]) > 0
    for field in security_section["fields"]:
        assert {"id", "value", "source"}.issubset(field.keys())
        assert field["source"] in {"default", "file"}


def test_result_is_plain_json_serializable_data(minimal_toml: Path) -> None:
    config = load_config(minimal_toml)

    model = settings_read_model(config, minimal_toml)

    assert isinstance(model, dict)
    serialized = json.dumps(model)
    assert json.loads(serialized) == model
