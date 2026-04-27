"""Test configuration for arcllm.

Provides environment isolation so tests always run against the packaged
config defaults, never the developer's ~/.arc/arcllm.toml overrides.

Per ADR-019: audit is a universal default in enterprise/federal tiers.
Packaged defaults ship with audit=false (open tier baseline). Tests that
assert on module stacking must be isolated from the user config so that
assertions reflect explicit kwargs, not ambient user settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Point ARC_CONFIG_DIR at an empty tmp dir for every test.

    This prevents ~/.arc/arcllm.toml (or any user-installed override) from
    contaminating test assertions about default module stacking. Tests that
    want to exercise user-config merging can override ARC_CONFIG_DIR themselves
    by calling monkeypatch.setenv after this fixture has run.
    """
    # Use a directory that exists but contains no config files.
    # _user_config_path() will look for arcllm.toml and config.toml — neither
    # will be present, so load_global_config() falls back to packaged defaults.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    yield
