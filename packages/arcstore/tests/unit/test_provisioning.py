"""Release-validation tests for ArcStore PostgreSQL provisioning surfaces."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import tomlkit
from scripts import deploy_node_overlays

ROOT = Path(__file__).parents[4]
INSTALLER = ROOT / "scripts" / "install-postgres.sh"
MEMORY_INSTALLER = ROOT / "scripts" / "install-memory-postgres.sh"
DEPLOY = ROOT / "scripts" / "deploy-node.sh"
BASH = shutil.which("bash") or "/bin/bash"


def test_postgres_installer_is_arcstore_specific_and_syntax_valid() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert subprocess.run([BASH, "-n", str(INSTALLER)], check=False).returncode == 0
    assert "arcstore-postgres" in source
    assert "arcstore-pg-data" in source
    assert "ARCSTORE_DATABASE_URL" in source
    assert "pgvector" not in source
    assert "ARC_MEMORY" not in source

    memory_source = MEMORY_INSTALLER.read_text(encoding="utf-8")
    assert "arc-memory-postgres" in memory_source
    assert "arcstore-postgres" not in memory_source
    assert "arc-memory-pg-data" in memory_source


def test_deploy_provisions_and_smoke_checks_arcstore_without_printing_dsn() -> None:
    source = DEPLOY.read_text(encoding="utf-8")

    assert "install-postgres.sh" in source
    assert "install-memory-postgres.sh" in source
    assert "ARCSTORE_DATABASE_URL" in source
    assert "ARCSTORE_REQUIRE_SCHEMA=1" in source
    assert 'echo "$ARCSTORE_DATABASE_URL"' not in source


def test_arcstore_overlay_removes_stale_sqlite_backend_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "arcagent.toml"
    path.write_text(
        '[arcstore]\nbackend = "sqlite"\ndata_dir = ""\n',
        encoding="utf-8",
    )

    deploy_node_overlays.apply_arcstore_overlay(path, "vault://arcstore/database")
    deploy_node_overlays.apply_arcstore_overlay(path, "vault://arcstore/database")

    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    assert document["arcstore"]["database_credential_ref"] == "vault://arcstore/database"
    assert "backend" not in document["arcstore"]
