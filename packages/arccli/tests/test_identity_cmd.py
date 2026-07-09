"""``arc identity`` — create/show the standalone signing authority."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from arccli.commands.identity import (
    DEFAULT_KEY_DIR,
    identity_handler,
    load_signing_authority,
)
from arccli.commands.registry import resolve_command


def test_registered_in_command_registry() -> None:
    cmd = resolve_command("identity")
    assert cmd is not None and cmd.handler is not None


def test_init_creates_and_persists_authority(tmp_path: Path) -> None:
    identity_handler(["init", "--key-dir", str(tmp_path)])
    loaded = load_signing_authority(tmp_path)
    assert loaded is not None
    assert loaded.did.startswith("did:arc:")
    assert loaded.can_sign

    key_file = next(tmp_path.glob("*.key"))
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600  # private key is owner-only


def test_show_before_init_reports_none(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    identity_handler(["show", "--key-dir", str(tmp_path)])
    assert "No signing authority" in capsys.readouterr().out


def test_init_is_idempotent_without_force(tmp_path: Path) -> None:
    identity_handler(["init", "--key-dir", str(tmp_path)])
    first = load_signing_authority(tmp_path)
    with pytest.raises(SystemExit):
        identity_handler(["init", "--key-dir", str(tmp_path)])
    # unchanged
    assert load_signing_authority(tmp_path).did == first.did  # type: ignore[union-attr]


def test_force_replaces_authority(tmp_path: Path) -> None:
    identity_handler(["init", "--key-dir", str(tmp_path)])
    first = load_signing_authority(tmp_path)
    identity_handler(["init", "--key-dir", str(tmp_path), "--force"])
    second = load_signing_authority(tmp_path)
    assert first is not None and second is not None
    assert first.did != second.did


def test_default_key_dir_is_under_home() -> None:
    assert DEFAULT_KEY_DIR == Path("~/.arc/identity").expanduser()


def test_init_honors_arc_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARC_CONFIG_DIR redirects the key under <dir>/identity, not ~/.arc (F5)."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    identity_handler(["init"])
    key_dir = tmp_path / "identity"
    loaded = load_signing_authority(key_dir)
    assert loaded is not None and loaded.did.startswith("did:arc:")
    assert (key_dir / "active.did").exists()


def test_init_honors_dir_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--dir sets the config base; the key lands under <dir>/identity (F5)."""
    monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
    identity_handler(["init", "--dir", str(tmp_path)])
    loaded = load_signing_authority(tmp_path / "identity")
    assert loaded is not None and loaded.did.startswith("did:arc:")


def test_key_dir_flag_overrides_arc_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--key-dir pins the exact dir even when ARC_CONFIG_DIR is set (F5)."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "env"))
    explicit = tmp_path / "explicit"
    identity_handler(["init", "--key-dir", str(explicit)])
    assert load_signing_authority(explicit) is not None
    assert not (tmp_path / "env" / "identity" / "active.did").exists()
