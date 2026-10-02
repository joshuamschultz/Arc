"""Item 56: import review resolves strict skill sections from the agent's tier + config."""

from __future__ import annotations

from pathlib import Path

import pytest

import arcagent


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "arcagent.toml"
    path.write_text(f'[agent]\nname = "ada"\n{body}', encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('[security]\ntier = "federal"\n[capabilities]\nstrict_skill_sections = true\n', True),
        ('[security]\ntier = "federal"\n', False),
        ('[security]\ntier = "enterprise"\n[capabilities]\nstrict_skill_sections = true\n', False),
        ("[capabilities]\nstrict_skill_sections = true\n", False),
    ],
)
def test_strict_only_when_federal_and_configured(
    tmp_path: Path, body: str, expected: bool
) -> None:
    assert arcagent.strict_sections_for_agent(_config(tmp_path, body)) is expected


def test_unreadable_config_fails_closed(tmp_path: Path) -> None:
    assert arcagent.strict_sections_for_agent(tmp_path / "missing.toml") is True
