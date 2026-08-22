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


def store_db_path(data_dir: str | Path | None = None) -> Path:
    """Canonical path to the shared operational store DB (``store/arcui.db``).

    The ``store/arcui.db`` literal was hardcoded in arcagent, arcui, and arccli
    (ARCH-2); one locator over :func:`resolve_data_dir` keeps every entry point
    pointed at the same file — the agent that writes tasks and the dashboard
    that reads them must never diverge on the path.
    """
    return resolve_data_dir(data_dir) / "store" / "arcui.db"


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
    backend: str = "postgres"
    database_url: SecretStr | None = Field(default=None, repr=False)
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
    def max_pool_not_below_min(cls, value: int, info: ValidationInfo) -> int:
        minimum = info.data.get("pool_min_size", 1)
        if value < minimum:
            raise ValueError("pool_max_size must be at least pool_min_size")
        return value

    def postgres_settings(self) -> PostgresSettings:
        raw = self.database_url or SecretStr(os.environ.get(ENV_DATABASE_URL, ""))
        if not raw.get_secret_value():
            raise ValueError("ArcStore PostgreSQL database URL is required")
        parsed = urlparse(raw.get_secret_value())
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ValueError("ArcStore database URL must be a PostgreSQL URL")
        query = parse_qs(parsed.query)
        ssl_mode = query.get("sslmode", ["require"])[0]
        external = parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        if external and ssl_mode == "disable":
            raise ValueError("TLS is required for external PostgreSQL hosts")
        transaction_pool = parsed.port == 6543
        return PostgresSettings(
            dsn=raw,
            pool_min_size=self.pool_min_size,
            pool_max_size=self.pool_max_size,
            command_timeout=self.command_timeout,
            connect_timeout=self.connect_timeout,
            ssl_mode=ssl_mode,
            statement_cache_size=0 if transaction_pool else 100,
            transaction_pool=transaction_pool,
        )

    def resolve_data_dir(self) -> Path:
        """Resolve this config's data dir with the shared env > toml > default rule."""
        return resolve_data_dir(self.data_dir or None)


class PostgresSettings(BaseModel):
    """Provider-neutral, secret-safe settings consumed by the Postgres adapter."""

    model_config = ConfigDict(extra="forbid")
    dsn: SecretStr = Field(repr=False)
    pool_min_size: int
    pool_max_size: int
    command_timeout: float
    connect_timeout: float
    ssl_mode: str
    statement_cache_size: int
    transaction_pool: bool
