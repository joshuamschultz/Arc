"""Shared configuration + data-dir resolution for arcstore.

This module is the single source of the Arc data directory and the single
``[arcstore]`` config schema used by every entry point (arcllm, arcrun, arccli,
arcagent). Resolving the same path everywhere is what makes the "call a direct
arcllm now, spin up arcstore later, see the call" guarantee hold — divergent
paths would silently fragment history (SPEC-026 D-013).

``ArcStoreConfig`` is referenced, never redefined, by the other packages
(AC-7.3): one schema, one resolver, one precedence rule.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from arctrust.paths import store_dir
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator

ENV_DATA_DIR = "ARCSTORE_DATA_DIR"
ENV_DATABASE_URL = "ARCSTORE_DATABASE_URL"
"""Environment override for the Arc data directory (highest precedence)."""


def resolve_data_dir(configured: str | Path | None = None) -> Path:
    """Resolve the Arc data directory with a single, shared precedence rule.

    Precedence (SPEC-026 §13.2): ``ARCSTORE_DATA_DIR`` env  >  configured
    ``[arcstore].data_dir``  >  :func:`arctrust.paths.store_dir`. Every entry
    point calls this same function so a direct ``arc llm`` call and a later
    ``arc agent serve`` agree on the spool/store path.
    """
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return Path(env).expanduser()
    if configured:
        return Path(configured).expanduser()
    return store_dir()


class ArcStoreConfig(BaseModel):
    """The one canonical ``[arcstore]`` block (SPEC-026 FR-7, §13.1).

    arcllm / arcrun / arccli / arcagent reference this model and
    ``resolve_data_dir`` rather than redefining the block, so every entry point
    agrees on the spool/store path and the on/off switch. ``enabled`` is the
    single gate producers and the agent lifecycle check before recording or
    spinning up ingest.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    data_dir: str = ""
    database_credential_ref: str = ""
    pool_min_size: int = Field(default=1, ge=1, le=100)
    pool_max_size: int = Field(default=10, ge=1, le=100)
    command_timeout: float = Field(default=30.0, gt=0, le=300)
    connect_timeout: float = Field(default=10.0, gt=0, le=120)
    store_raw_bodies: bool = False
    rotation: str = "daily"
    retention: str = ""
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("pool_max_size")
    @classmethod
    def _pool_max_not_below_min(cls, value: int, info: ValidationInfo) -> int:
        if value < info.data.get("pool_min_size", 1):
            raise ValueError("pool_max_size must be at least pool_min_size")
        return value

    def postgres_settings(self, secret: SecretStr | None = None) -> PostgresSettings:
        """Resolve a runtime secret without ever persisting a DSN in config."""
        dsn = secret or SecretStr(os.environ.get(ENV_DATABASE_URL, ""))
        raw_dsn = dsn.get_secret_value()
        parsed = urlparse(raw_dsn)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ArcStoreConfigurationError("ArcStore PostgreSQL database URL is required")
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ssl_mode = parse_qs(parsed.query).get("sslmode", ["prefer" if local else "require"])[0]
        if not local and ssl_mode in {"disable", "allow", "prefer"}:
            raise ArcStoreConfigurationError("TLS is required for external PostgreSQL hosts")
        transaction_pool = parsed.port == 6543
        return PostgresSettings(
            dsn=dsn,
            pool_min_size=self.pool_min_size,
            pool_max_size=self.pool_max_size,
            command_timeout=self.command_timeout,
            connect_timeout=self.connect_timeout,
            ssl_mode=ssl_mode,
            statement_cache_size=0 if transaction_pool else 100,
        )

    def resolve_data_dir(self) -> Path:
        """Resolve this config's data dir with the shared env > toml > default rule."""
        return resolve_data_dir(self.data_dir or None)


class PostgresSettings(BaseModel):
    """Secret-safe local PostgreSQL and Supabase connection settings."""

    model_config = ConfigDict(extra="forbid")

    dsn: SecretStr = Field(repr=False)
    pool_min_size: int
    pool_max_size: int
    command_timeout: float
    connect_timeout: float
    ssl_mode: str
    statement_cache_size: int


class ArcStoreConfigurationError(ValueError):
    """Expected missing/invalid ArcStore deployment configuration."""
