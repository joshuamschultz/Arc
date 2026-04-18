"""arcagent.modules.vault — tier-driven secret resolution.

Public API
----------
``VaultBackend``     — Protocol every backend must implement
``VaultUnreachable`` — Exception raised when a backend cannot be contacted
``resolve_secret``   — Tier-driven resolution (federal / enterprise / personal)
``CachedVaultBackend`` — TTL cache wrapper around any VaultBackend

Backends
--------
``arcagent.modules.vault.backends.azure``  — Azure Key Vault
``arcagent.modules.vault.backends.env``    — Environment variables
``arcagent.modules.vault.backends.file``   — ~/.arc/secrets/{name} (0600)

Tier policy (SDD §3.1)
-----------------------
Federal   : vault required; VaultUnreachable → hard error, no fallback
Enterprise: vault first; VaultUnreachable → WARN + audit + env fallback
Personal  : vault → env → file; each step optional

Migration note
--------------
The old ``arcagent.modules.vault_azure`` import path continues to work via a
deprecation shim.  Update imports to ``arcagent.modules.vault.backends.azure``.
"""

from arcagent.modules.vault.cache import CachedVaultBackend
from arcagent.modules.vault.protocol import VaultBackend, VaultUnreachable
from arcagent.modules.vault.resolver import resolve_secret

__all__ = [
    "CachedVaultBackend",
    "VaultBackend",
    "VaultUnreachable",
    "resolve_secret",
]
