"""Deprecation shim for arcagent.modules.vault_azure.

This module has been superseded by ``arcagent.modules.vault`` (T1.5,
SPEC-018).  All symbols are re-exported from the new location so existing
callers continue to work without modification.

Migrate imports:
    Old: from arcagent.modules.vault_azure import AzureKeyVaultBackend
    New: from arcagent.modules.vault.backends.azure import AzureKeyVaultBackend

The shim emits a ``DeprecationWarning`` on import and will be removed in a
future major release.
"""

from __future__ import annotations

import warnings

warnings.warn(
    "arcagent.modules.vault_azure is deprecated. "
    "Use arcagent.modules.vault.backends.azure instead. "
    "This shim will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

from arcagent.modules.vault.backends.azure import AzureKeyVaultBackend  # noqa: E402

__all__ = ["AzureKeyVaultBackend"]
