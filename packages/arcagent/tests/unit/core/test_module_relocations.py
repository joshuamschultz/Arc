"""Non-module infrastructure lives in core/utils, not under modules/.

vault (secret-resolution library), file_handler (shared bot utility), the
arcteam bootstrap helper, and ModuleConfig (module-framework base) are not
modules; they must import from their framework homes and no longer sit under
``arcagent.modules``.
"""

from __future__ import annotations

import importlib

import pytest


def test_relocated_symbols_import_from_new_homes() -> None:
    from arcagent.core.arcteam_bootstrap import make_backend, message_signer
    from arcagent.core.module_config import ModuleConfig
    from arcagent.core.vault import resolve_secret
    from arcagent.utils.file_handler import FileHandler

    assert all(
        callable(x) for x in (make_backend, message_signer, resolve_secret, FileHandler, ModuleConfig)
    )


@pytest.mark.parametrize(
    "old_path",
    [
        "arcagent.modules.vault",
        "arcagent.modules.file_handler",
        "arcagent.modules.base_config",
        "arcagent.modules.messaging._bootstrap",
    ],
)
def test_old_module_homes_are_gone(old_path: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(old_path)


def test_vault_is_not_discovered_as_a_module() -> None:
    from arcagent.core.module_discovery import discover_modules

    assert "vault" not in discover_modules()
