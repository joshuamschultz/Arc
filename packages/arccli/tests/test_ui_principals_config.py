"""``arc ui start`` reads ``[ui.principals]`` and stamps the deployment tier (SPEC-087).

The section is validated by ``arcui.principals.PrincipalsConfig``; the tier
always comes from the deployment, never from the section, so a config file
cannot loosen it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from arcui.principals import PrincipalsConfig

from arccli.commands.ui import _maybe_build_gateway_config, _principals_config


def _args() -> argparse.Namespace:
    return argparse.Namespace(gateway_config=None, no_chat=False)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    return tmp_path / "config"


def _write(config_dir: Path, text: str) -> None:
    (config_dir / "gateway.toml").write_text(text)


def _config(config_dir: Path) -> PrincipalsConfig:
    return _principals_config(_maybe_build_gateway_config(_args(), Path.cwd()))


def test_defaults_without_a_section(config_dir: Path) -> None:
    config = _config(config_dir)
    assert config.tier == "personal"
    assert config.rate_per_minute == 600
    assert config.signed_required is False


def test_section_is_read_and_the_deployment_tier_is_stamped(config_dir: Path) -> None:
    _write(
        config_dir,
        '[gateway]\ntier = "enterprise"\n'
        "[ui.principals]\nrequire_signed_requests = true\n"
        "require_named_decider = true\nrate_per_minute = 30\n",
    )
    config = _config(config_dir)
    assert config.tier == "enterprise"
    assert config.signed_required is True
    assert config.named_decider_required is True
    assert config.rate_per_minute == 30


def test_federal_forces_signed_requests_whatever_the_section_says(config_dir: Path) -> None:
    _write(
        config_dir,
        '[gateway]\ntier = "federal"\n[ui.principals]\nrequire_signed_requests = false\n',
    )
    config = _config(config_dir)
    assert config.tier == "federal"
    assert config.signed_required is True


@pytest.mark.parametrize(
    "section",
    [
        pytest.param('tier = "personal"\n', id="tier-is-not-settable"),
        pytest.param("rate_per_minuet = 5\n", id="unknown-key"),
        pytest.param("rate_per_minute = 0\n", id="non-positive-rate"),
    ],
)
def test_invalid_section_stops_the_start(config_dir: Path, section: str) -> None:
    _write(config_dir, "[ui.principals]\n" + section)
    with pytest.raises(SystemExit):
        _config(config_dir)
