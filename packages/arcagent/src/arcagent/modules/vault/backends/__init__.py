"""Vault backend implementations.

Each sub-module provides a concrete ``VaultBackend`` implementation:

- ``azure``  — Azure Key Vault via ``DefaultAzureCredential``
- ``env``    — Environment-variable reader (enterprise/personal fallback)
- ``file``   — File-based reader from ``~/.arc/secrets/{name}`` (0600 enforced)
"""
