"""Vault resolver instantiation.

Sibling of ``arcagent.core.agent``. Owns the small set of helpers that
validate and instantiate a vault backend from the agent's TOML config.
Backend reference is ``module.path:ClassName`` and is import-checked
before any class load to prevent injection of arbitrary strings into
``importlib``.

Re-exported through ``arcagent.core.agent`` so existing imports
(``from arcagent.core.agent import _validate_vault_backend``) keep
working unchanged.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from arcagent.core.config import ArcAgentConfig
from arcagent.core.errors import ConfigError

_logger = logging.getLogger("arcagent.vault_resolver")


def _validate_vault_backend(backend_ref: str) -> None:
    """Validate vault backend module reference format.

    Must be ``module.path:ClassName`` format. Prevents injection
    of arbitrary strings into importlib.
    """
    if ":" not in backend_ref:
        raise ConfigError(
            code="CONFIG_INVALID_VAULT_BACKEND",
            message=f"Invalid vault backend format (missing ':'): {backend_ref}",
            details={"backend": backend_ref},
        )

    module_path, _ = backend_ref.rsplit(":", 1)
    if not module_path or ".." in module_path:
        raise ConfigError(
            code="CONFIG_INVALID_VAULT_BACKEND",
            message=f"Invalid vault backend module path: {module_path}",
            details={"backend": backend_ref},
        )


def create_vault_resolver(config: ArcAgentConfig) -> Any:
    """Create a vault resolver instance from config, or return None.

    Validates the backend reference format before importing.
    Returns the instantiated vault backend (with cache_ttl_seconds
    threaded through).
    """
    backend_ref = config.vault.backend
    if not backend_ref:
        return None

    _validate_vault_backend(backend_ref)

    try:
        module_path, class_name = backend_ref.rsplit(":", 1)
        module = importlib.import_module(module_path)
        backend_cls = getattr(module, class_name)
        return backend_cls(cache_ttl_seconds=config.vault.cache_ttl_seconds)
    except Exception:  # reason: re-raise after log
        _logger.exception("Failed to create vault resolver: %s", backend_ref)
        raise
