"""Environment-variable vault backend.

Resolves secrets from environment variables.  Used as an explicit backend at
enterprise and personal tiers, or as a named backend in config.

The env var to read is determined by the ``path`` argument:
- If an ``env_prefix`` is configured, the var name is ``{env_prefix}{path}``
  with hyphens/slashes replaced by underscores and uppercased.
- If no prefix is configured, the ``path`` is used as-is (uppercased with
  separator normalisation).

Design decision: this backend never raises ``VaultUnreachable`` — environment
variables are always "reachable" (they are either set or not).  A missing var
returns ``None``.
"""

from __future__ import annotations

import os
import re


def _path_to_env_var(path: str, prefix: str = "") -> str:
    """Convert a secret path to an environment variable name.

    Normalises separators (``-``, ``/``, ``.``) to ``_``, uppercases, and
    prepends any configured prefix.

    Examples:
        ``"openai-api-key"``          → ``"OPENAI_API_KEY"``
        ``"app/database/password"``   → ``"APP_DATABASE_PASSWORD"``
    """
    normalised = re.sub(r"[-/.]", "_", path).upper()
    if prefix:
        return f"{prefix.rstrip('_').upper()}_{normalised}"
    return normalised


class EnvBackend:
    """Reads secrets from environment variables.

    Args:
        env_prefix: Optional prefix prepended to every secret path when
            deriving the environment variable name.  Useful for namespaced
            environments (e.g., ``"MYAPP"``).
    """

    def __init__(self, env_prefix: str = "") -> None:
        self._env_prefix = env_prefix

    async def get_secret(self, path: str) -> str | None:
        """Look up the environment variable corresponding to ``path``.

        Args:
            path: Secret name.

        Returns:
            Environment variable value, or ``None`` if not set or empty.
        """
        env_var = _path_to_env_var(path, self._env_prefix)
        value = os.environ.get(env_var)
        return value if value else None


__all__ = ["EnvBackend"]
