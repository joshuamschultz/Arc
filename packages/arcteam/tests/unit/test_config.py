"""Tests for arcteam.config — TeamConfig defaults and overrides."""

from pathlib import Path

from arcteam.config import TeamConfig


class TestTeamConfig:
    """Config defaults and override."""

    def test_defaults(self) -> None:
        cfg = TeamConfig()
        assert cfg.root == Path.home() / ".arc" / "team"
        assert cfg.hmac_key_env == "ARCTEAM_HMAC_KEY"
        assert cfg.max_body_bytes == 65536
        assert cfg.default_poll_limit == 10

    def test_override(self) -> None:
        cfg = TeamConfig(
            root=Path("/tmp/test-team"),
            hmac_key_env="MY_KEY",
            max_body_bytes=32768,
            default_poll_limit=50,
        )
        assert cfg.root == Path("/tmp/test-team")
        assert cfg.hmac_key_env == "MY_KEY"
        assert cfg.max_body_bytes == 32768
        assert cfg.default_poll_limit == 50

    def test_partial_override(self) -> None:
        cfg = TeamConfig(root=Path("/tmp/custom"))
        assert cfg.root == Path("/tmp/custom")
        assert cfg.hmac_key_env == "ARCTEAM_HMAC_KEY"  # Default
