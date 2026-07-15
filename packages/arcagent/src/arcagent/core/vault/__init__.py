"""arcagent.core.vault — tier-driven secret resolution.

Public API
----------
``VaultBackend``     — Protocol every backend must implement
``VaultUnreachable`` — Exception raised when a backend cannot be contacted
``resolve_secret``   — Tier-driven resolution (federal / enterprise / personal)
``CachedVaultBackend`` — TTL cache wrapper around any VaultBackend

Backends
--------
``arcagent.core.vault.backends.azure``  — Azure Key Vault
``arcagent.core.vault.backends.env``    — Environment variables
``arcagent.core.vault.backends.file``   — ~/.arc/secrets/{name} (0600)

Tier policy (SDD §3.1)
-----------------------
Federal   : vault required; VaultUnreachable → hard error, no fallback
Enterprise: vault first; VaultUnreachable → WARN + audit + env fallback
Personal  : vault → env → file; each step optional
"""

from arcagent.core.vault.cache import CachedVaultBackend
from arcagent.core.vault.protocol import VaultBackend, VaultUnreachable
from arcagent.core.vault.resolver import resolve_secret

__all__ = [
    "CachedVaultBackend",
    "VaultBackend",
    "VaultUnreachable",
    "resolve_secret",
]
