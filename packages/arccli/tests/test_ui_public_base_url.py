"""``arc ui start`` resolves ``[ui] public_base_url`` on the default path (alpha-2 item 75b).

The address is set in Settings → Access only (J1-2); there is no start flag to
override it, so notices and OAuth can never disagree about it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest
from arcstore.backends.memory import FakeBackend

from arccli.commands.ui import _maybe_build_gateway_config, _public_base_url


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {"gateway_config": None, "no_chat": False}
    return argparse.Namespace(**{**base, **overrides})


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    return tmp_path / "config"


def _resolve(args: argparse.Namespace) -> str | None:
    return _public_base_url(_maybe_build_gateway_config(args, Path.cwd()))


def test_default_start_reads_gateway_toml_ui_section(config_dir: Path) -> None:
    (config_dir / "gateway.toml").write_text(
        '[ui]\npublic_base_url = "https://arc.example.com/"\n'
    )
    assert _resolve(_args()) == "https://arc.example.com"


def test_default_start_notice_carries_the_deep_link(config_dir: Path) -> None:
    from arcui.connection_health import build_connection_health_monitor
    from arcui.public_address import PublicAddress

    (config_dir / "gateway.toml").write_text('[ui]\npublic_base_url = "https://arc.example.com"\n')
    state = SimpleNamespace(
        arcstore_backend=FakeBackend(), public_address=PublicAddress(ui_port=8420)
    )
    monitor = build_connection_health_monitor(SimpleNamespace(state=state))
    assert monitor is not None and monitor._ui_base() == "https://arc.example.com"


def test_missing_gateway_toml_leaves_it_unset(config_dir: Path) -> None:
    assert _resolve(_args()) is None


def test_there_is_no_start_flag_for_the_address() -> None:
    from arccli.commands.ui import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["start", "--public-base-url", "https://x.example"])
