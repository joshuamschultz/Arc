"""Unit tests for the single secure ArcStore PostgreSQL configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from arcstore import ArcStoreConfig, resolve_data_dir
from arcstore.config import ENV_DATA_DIR, ENV_DATABASE_URL


def test_config_defaults_and_rejects_unknown_keys() -> None:
    config = ArcStoreConfig()
    assert config.enabled is True
    assert config.pool_min_size == 1
    assert config.pool_max_size == 10
    with pytest.raises(ValidationError):
        ArcStoreConfig(not_a_setting=True)


def test_local_postgres_is_accepted_without_tls_and_external_requires_it() -> None:
    local = ArcStoreConfig()
    assert (
        local.postgres_settings(SecretStr("postgresql://user:secret@localhost/arcstore")).ssl_mode
        == "prefer"
    )
    with pytest.raises(ValueError, match="TLS"):
        ArcStoreConfig().postgres_settings(
            SecretStr("postgresql://user:secret@example.com/arcstore?sslmode=disable")
        )


def test_supabase_pooler_disables_prepared_statement_cache() -> None:
    config = ArcStoreConfig()
    settings = config.postgres_settings(
        SecretStr("postgresql://user:secret@db.pooler.supabase.com:6543/postgres")
    )
    assert settings.statement_cache_size == 0
    assert "user:secret@" not in repr(config)
    assert "user:secret@" not in repr(settings)


def test_database_url_env_is_resolved_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_DATABASE_URL, "postgresql://user:secret@localhost/arcstore")
    assert ArcStoreConfig().postgres_settings().dsn.get_secret_value().endswith("/arcstore")


def test_data_dir_resolution_still_uses_its_single_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "env-store"))
    assert ArcStoreConfig(data_dir=str(tmp_path / "configured")).resolve_data_dir() == (
        tmp_path / "env-store"
    )
    assert resolve_data_dir() == tmp_path / "env-store"
