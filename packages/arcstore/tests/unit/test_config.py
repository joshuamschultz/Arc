"""Unit tests for ArcStoreConfig (SPEC-026 FR-7).

The single canonical ``[arcstore]`` config schema. arcllm / arcrun / arccli all
reference this model and its ``resolve_data_dir`` — none redefine the block — so a
direct ``arc llm`` call and a later ``arc agent serve`` land in the same store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from arcstore import ArcStoreConfig, resolve_data_dir
from arcstore.config import ENV_DATA_DIR


def test_arcstore_config_block_defaults() -> None:
    """The block validates with documented defaults (SDD §13.1)."""
    cfg = ArcStoreConfig()
    assert cfg.enabled is True
    assert cfg.data_dir == ""
    assert cfg.backend == "sqlite"
    assert cfg.store_raw_bodies is False
    assert cfg.rotation == "daily"
    assert cfg.retention == ""
    assert cfg.sample_rate == 1.0


def test_arcstore_config_block_accepts_overrides() -> None:
    cfg = ArcStoreConfig(
        enabled=False,
        data_dir="/tmp/arc-store",
        backend="postgres",
        store_raw_bodies=True,
        rotation="hourly",
        retention="30d",
        sample_rate=0.25,
    )
    assert cfg.enabled is False
    assert cfg.backend == "postgres"
    assert cfg.store_raw_bodies is True
    assert cfg.sample_rate == 0.25


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        ArcStoreConfig(notakey=1)


def test_sample_rate_bounded_0_to_1() -> None:
    with pytest.raises(ValidationError):
        ArcStoreConfig(sample_rate=1.5)
    with pytest.raises(ValidationError):
        ArcStoreConfig(sample_rate=-0.1)


def test_resolve_data_dir_uses_configured_value(tmp_path: Path) -> None:
    cfg = ArcStoreConfig(data_dir=str(tmp_path / "store"))
    assert cfg.resolve_data_dir() == tmp_path / "store"


def test_resolve_data_dir_falls_back_to_default_when_empty() -> None:
    cfg = ArcStoreConfig(data_dir="")
    # Mirrors the module-level resolver's default (env unset in this test).
    assert cfg.resolve_data_dir() == resolve_data_dir(None)


def test_env_overrides_configured_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ARCSTORE_DATA_DIR`` wins over the TOML value (FR-7 precedence)."""
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "env-store"))
    cfg = ArcStoreConfig(data_dir=str(tmp_path / "toml-store"))
    assert cfg.resolve_data_dir() == tmp_path / "env-store"


def test_disabled_short_circuits() -> None:
    """``enabled=false`` is the single switch every entry point checks (AC-7.4)."""
    assert ArcStoreConfig(enabled=False).enabled is False
    # Data-dir resolution still works (the dir is created lazily either way);
    # the gate is the boolean, checked by producers/lifecycle, not the resolver.
    assert isinstance(ArcStoreConfig(enabled=False).resolve_data_dir(), Path)


def test_env_var_name_is_stable() -> None:
    """Guard the documented env var so the shared contract can't drift."""
    assert ENV_DATA_DIR == "ARCSTORE_DATA_DIR"
