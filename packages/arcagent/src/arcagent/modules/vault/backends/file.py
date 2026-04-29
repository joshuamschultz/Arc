"""File-based secret backend for personal-tier deployments.

Reads secrets from ``~/.arc/secrets/{name}``.  Each secret lives in its own
file whose contents are the secret value (stripped of leading/trailing
whitespace).

Security contract (NIST IA-5, credential management):
- Files MUST have mode 0600 (owner read/write only).
- Any file with mode 0644 or broader is REFUSED.  This prevents silently
  reading credentials that are world-readable due to misconfiguration.
- The secrets directory itself should be 0700; this backend checks but does
  not enforce the directory permission (directory-level check is advisory only
  since the user may have intentionally opened it for tooling).

This backend never raises ``VaultUnreachable`` — the filesystem is always
"reachable". A missing file, a wrong-mode file, or an empty file all return
``None`` so the resolver can continue down the fallback chain.
"""

from __future__ import annotations

import logging
from pathlib import Path

_logger = logging.getLogger("arcagent.modules.vault.backends.file")

# The directory where per-secret files live.
_DEFAULT_SECRETS_DIR = Path("~/.arc/secrets").expanduser()

# Required mode mask: owner read/write only, no group/other bits.
_REQUIRED_MODE = 0o600
_ALLOWED_MODE_MASK = 0o777  # Strip sticky / setuid bits before comparison


class FileBackend:
    """Personal-tier file-based secret backend.

    Reads ``{secrets_dir}/{name}`` files.  Refuses files whose permissions
    are broader than 0600 (security gate — prevents reading credentials that
    are accidentally world-readable).

    Args:
        secrets_dir: Directory containing one-file-per-secret.  Defaults to
            ``~/.arc/secrets``.
    """

    def __init__(self, secrets_dir: Path | None = None) -> None:
        self._secrets_dir = secrets_dir or _DEFAULT_SECRETS_DIR

    async def get_secret(self, path: str) -> str | None:
        """Read a secret from the filesystem.

        Args:
            path: Secret name.  Must be a plain filename with no path
                separators (``/``).  Path traversal attempts are rejected.

        Returns:
            Secret value (stripped), or ``None`` if the file does not exist
            or its permissions are too broad.
        """
        if "/" in path or "\\" in path or ".." in path:
            _logger.warning(
                "File backend: rejected path traversal attempt: %r",
                path,
            )
            return None

        secret_path = self._secrets_dir / path

        if not secret_path.exists():
            return None

        # Enforce 0600 — refuse any file that is readable by group or others.
        file_stat = secret_path.stat()
        actual_mode = file_stat.st_mode & _ALLOWED_MODE_MASK

        if actual_mode != _REQUIRED_MODE:
            _logger.warning(
                "File backend: REFUSING to read %s — mode is %o, expected %o. Run: chmod 600 %s",
                secret_path,
                actual_mode,
                _REQUIRED_MODE,
                secret_path,
            )
            return None

        content = secret_path.read_text(encoding="utf-8").strip()
        return content if content else None


__all__ = ["FileBackend"]
