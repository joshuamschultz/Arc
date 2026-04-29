"""Backward-compatibility tests: vault_azure deprecation shim.

These tests verify that existing callers importing from
``arcagent.modules.vault_azure`` continue to work after the module was
relocated to ``arcagent.modules.vault.backends.azure``.
"""

from __future__ import annotations

import importlib
import warnings


def test_vault_azure_shim_emits_deprecation_warning() -> None:
    """Importing vault_azure must emit a DeprecationWarning."""
    # Re-import the module fresh to trigger the warning again
    import sys

    # Remove cached modules so the warning fires on next import
    for key in list(sys.modules.keys()):
        if "vault_azure" in key:
            del sys.modules[key]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("arcagent.modules.vault_azure")

    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecation_warnings, "Expected a DeprecationWarning from vault_azure import"
    assert "vault_azure" in str(deprecation_warnings[0].message).lower()


def test_vault_azure_still_exports_azure_key_vault_backend() -> None:
    """AzureKeyVaultBackend must still be importable from the old path."""
    import sys

    for key in list(sys.modules.keys()):
        if "vault_azure" in key:
            del sys.modules[key]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from arcagent.modules.vault.backends.azure import (
            AzureKeyVaultBackend as NewAzureKeyVaultBackend,
        )
        from arcagent.modules.vault_azure import AzureKeyVaultBackend

    # Both import paths must point to the same class
    assert AzureKeyVaultBackend is NewAzureKeyVaultBackend


def test_new_import_path_works() -> None:
    """New import path works without any deprecation warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from arcagent.modules.vault.backends.azure import AzureKeyVaultBackend  # noqa: F401

    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not deprecation_warnings, "New import path must not emit deprecation warnings"


def test_vault_module_public_api() -> None:
    """arcagent.modules.vault exports its full public API."""
    from arcagent.modules.vault import (  # noqa: F401
        CachedVaultBackend,
        VaultBackend,
        VaultUnreachable,
        resolve_secret,
    )
