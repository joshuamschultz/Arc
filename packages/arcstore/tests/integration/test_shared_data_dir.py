"""FR-7: every entry point resolves the SAME data dir and config (AC-7.1/7.3/7.4).

A direct ``arc llm`` call and a later ``arc agent serve`` must agree on the
spool/store path, or history fragments silently. The single source of that
agreement is ``arcstore.config`` — referenced, never redefined.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_producers_import_the_one_resolver() -> None:
    """arcllm + arcrun reach the spool through the same arcstore.config resolver."""
    import arcllm.modules.telemetry as telemetry
    import arcrun.events as events

    from arcstore.config import resolve_data_dir
    from arcstore.spool import resolve_data_dir as spool_resolver

    # The spool module (used by both producers) binds the one resolver.
    assert spool_resolver is resolve_data_dir
    # Both producers record through arcstore.spool.record — the shared path.
    assert telemetry._spool_record.__module__ == "arcstore.spool"
    assert events._spool_record.__module__ == "arcstore.spool"


def test_all_entry_points_agree_on_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identical env → identical resolved data dir across every caller (AC-7.1)."""
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "shared"))

    import argparse

    from arccli.commands.store import _resolve_dir  # arccli entry point

    from arcstore import ArcStoreConfig, resolve_data_dir  # producers + lib

    ns = argparse.Namespace(data_dir=None)
    cli_dir = _resolve_dir(ns)
    lib_dir = resolve_data_dir()
    cfg_dir = ArcStoreConfig().resolve_data_dir()

    assert cli_dir == lib_dir == cfg_dir == tmp_path / "shared"


def test_arcstore_config_is_single_source() -> None:
    """``ArcStoreConfig`` is defined once in arcstore.config (AC-7.3)."""
    import arcstore
    from arcstore.config import ArcStoreConfig

    assert arcstore.ArcStoreConfig is ArcStoreConfig
    assert ArcStoreConfig.__module__ == "arcstore.config"


def test_disabled_flag_default_true_and_toggle() -> None:
    """``enabled`` is the single gate entry points check; defaults true (AC-7.4)."""
    from arcstore import ArcStoreConfig

    assert ArcStoreConfig().enabled is True
    assert ArcStoreConfig(enabled=False).enabled is False
