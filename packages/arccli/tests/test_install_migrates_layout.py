"""``arc install`` splits a flat Arc home before any stage reads a path.

Order is the whole point: preflight loads the operator key, the bundle stage
reads the staged-bundle store, and the module stage writes under the runtime. A
migration that ran after any of them would leave one stage answering from the
pre-split layout and the next from the post-split one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import paths

from arccli.commands import install


@pytest.fixture
def flat_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "arc-home"
    monkeypatch.setenv("ARC_CONFIG_DIR", str(root))
    root.mkdir()
    (root / "arcagent.toml").write_text("[agent]\n", encoding="utf-8")
    (root / "operator").mkdir()
    (root / "operator" / "operator.key").write_text("seed", encoding="utf-8")
    return root


def test_install_migrates_a_flat_home(flat_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install.migrate_layout_or_exit()

    assert paths.config_file("arcagent.toml").read_text(encoding="utf-8") == "[agent]\n"
    assert paths.default_operator_key_path().read_text(encoding="utf-8") == "seed"
    assert "Layout" in capsys.readouterr().out


def test_running_install_twice_reports_nothing(
    flat_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Idempotent: the second run is silent and does not exit non-zero."""
    install.migrate_layout_or_exit()
    capsys.readouterr()

    install.migrate_layout_or_exit()

    assert capsys.readouterr().out == ""
    assert paths.default_operator_key_path().exists()


def test_install_stops_when_the_migration_refuses(
    flat_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blocked migration must not fall through into stages that read paths."""
    occupied = paths.default_operator_key_path()
    occupied.parent.mkdir(parents=True)
    occupied.write_text("IMPOSTOR", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        install.migrate_layout_or_exit()

    assert exit_info.value.code == 1
    assert "already exists" in capsys.readouterr().err
    assert (flat_home / "operator" / "operator.key").read_text(encoding="utf-8") == "seed"


def test_migration_runs_before_preflight(flat_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the ORDER, not just the presence — that is the defect being prevented."""
    order: list[str] = []

    def record_migration() -> None:
        order.append("migrate")

    def stop_at_preflight(_root: object) -> None:
        order.append("preflight")
        raise SystemExit(0)

    monkeypatch.setattr(install, "migrate_layout_or_exit", record_migration)
    monkeypatch.setattr(install, "_preflight_or_exit", stop_at_preflight)
    monkeypatch.setattr(install.up, "resolve_team_root", lambda _root: None)

    with pytest.raises(SystemExit):
        install._install.__wrapped__(object()) if hasattr(
            install._install, "__wrapped__"
        ) else install._install(_Args())

    assert order == ["migrate", "preflight"]


class _Args:
    team_root = None


def test_arc_up_also_migrates_before_it_reads_anything(
    flat_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``arc up`` on an un-migrated box must not read an empty config root.

    `arc install` and the systemd unit both migrate first, but nothing forces an
    operator through either. Without this, `arc up` would resolve a fresh empty
    `config/`, come up with none of the operator's settings, and look healthy.
    """
    from arccli.commands import up

    order: list[str] = []
    monkeypatch.setattr(
        up,
        "resolve_team_root",
        lambda _root: order.append("read") or None,  # type: ignore[func-returns-value]
    )

    with pytest.raises(SystemExit):
        up._up(_UpArgs())

    assert order == ["read"], "arc up did not reach the read stage"
    # The migration ran first: the config is where the deployment now reads it.
    assert paths.config_file("arcagent.toml").read_text(encoding="utf-8") == "[agent]\n"
    assert paths.default_operator_key_path().read_text(encoding="utf-8") == "seed"


class _UpArgs:
    check = False
    no_install = False
    team_root = None
    host = "127.0.0.1"
    port = 8420
