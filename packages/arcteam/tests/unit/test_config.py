"""Tests for arcteam.config — TeamConfig defaults and overrides."""

from pathlib import Path

import pytest

from arcteam.config import (
    TeamConfig,
    default_jetstream_store_dir,
)


class TestJetstreamStoreDir:
    """The JetStream store dir mirrors the team-root config-dir resolution."""

    def test_honors_arc_config_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An isolated ARC_CONFIG_DIR keeps the NATS store local (leak bug)."""
        monkeypatch.setenv("ARC_CONFIG_DIR", "/tmp/isotest/config")
        assert default_jetstream_store_dir() == Path("/tmp/isotest/config/nats/jetstream")

    def test_falls_back_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
        assert default_jetstream_store_dir() == Path.home() / ".arc" / "nats" / "jetstream"


class TestTeamConfig:
    """Config defaults and override."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
        cfg = TeamConfig()
        assert cfg.root == Path.home() / ".arc" / "team"
        assert cfg.max_body_bytes == 65536
        assert cfg.default_poll_limit == 10

    def test_root_honors_arc_config_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An isolated ARC_CONFIG_DIR keeps team data local (root escape bug)."""
        monkeypatch.setenv("ARC_CONFIG_DIR", "/tmp/isotest/config")
        assert TeamConfig().root == Path("/tmp/isotest/config/team")

    def test_root_falls_back_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
        assert TeamConfig().root == Path.home() / ".arc" / "team"

    def test_explicit_root_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit root beats ARC_CONFIG_DIR (mirrors CLI ``--root`` precedence)."""
        monkeypatch.setenv("ARC_CONFIG_DIR", "/tmp/isotest/config")
        assert TeamConfig(root=Path("/tmp/explicit")).root == Path("/tmp/explicit")

    def test_override(self) -> None:
        cfg = TeamConfig(
            root=Path("/tmp/test-team"),
            max_body_bytes=32768,
            default_poll_limit=50,
        )
        assert cfg.root == Path("/tmp/test-team")
        assert cfg.max_body_bytes == 32768
        assert cfg.default_poll_limit == 50

    def test_partial_override(self) -> None:
        cfg = TeamConfig(root=Path("/tmp/custom"))
        assert cfg.root == Path("/tmp/custom")
