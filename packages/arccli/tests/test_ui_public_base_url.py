"""``arc ui start`` resolves ``[ui] public_base_url`` on the default path (alpha-2 item 75b)."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest
from arcstore.backends.memory import FakeBackend

from arccli.commands.ui import _maybe_build_gateway_config, _public_base_url


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {"gateway_config": None, "no_chat": False, "public_base_url": None}
    return argparse.Namespace(**{**base, **overrides})


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    return tmp_path / "config"


def _resolve(args: argparse.Namespace) -> str | None:
    return _public_base_url(args, _maybe_build_gateway_config(args, Path.cwd()))


def test_default_start_reads_gateway_toml_ui_section(config_dir: Path) -> None:
    (config_dir / "gateway.toml").write_text(
        '[ui]\npublic_base_url = "https://arc.example.com/"\n'
    )
    assert _resolve(_args()) == "https://arc.example.com"


def test_default_start_notice_carries_the_deep_link(config_dir: Path) -> None:
    from arcui.connection_health import build_connection_health_monitor

    (config_dir / "gateway.toml").write_text('[ui]\npublic_base_url = "https://arc.example.com"\n')
    state = SimpleNamespace(arcstore_backend=FakeBackend(), public_base_url=_resolve(_args()))
    monitor = build_connection_health_monitor(SimpleNamespace(state=state))
    assert monitor is not None and monitor._ui_base == "https://arc.example.com"


def test_missing_gateway_toml_leaves_it_unset(config_dir: Path) -> None:
    assert _resolve(_args()) is None


def test_flag_overrides_gateway_toml(config_dir: Path) -> None:
    (config_dir / "gateway.toml").write_text('[ui]\npublic_base_url = "https://old.example.com"\n')
    assert _resolve(_args(public_base_url="https://new.example.com/")) == "https://new.example.com"


def test_flag_is_validated_like_the_config(config_dir: Path) -> None:
    with pytest.raises(ValueError):
        _resolve(_args(public_base_url="http://arc.example.com"))
