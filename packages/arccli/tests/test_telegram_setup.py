"""Tests for ``arc agent setup-telegram`` command helpers."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

from arccli.telegram_setup import (
    _discover_chat_id,
    _store_token,
    _update_agent_config,
    _verify_token,
    _write_file_secure,
)

# ── _verify_token ────────────────────────────────────────────────


class TestVerifyToken:
    def test_valid_token_returns_username(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ok": True,
            "result": {"username": "test_bot"},
        }
        with patch("arccli.telegram_setup.httpx.get", return_value=mock_resp):
            assert _verify_token("123:ABC") == "test_bot"

    def test_invalid_token_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ok": False,
            "description": "Unauthorized",
        }
        with patch("arccli.telegram_setup.httpx.get", return_value=mock_resp):
            assert _verify_token("bad-token") is None

    def test_network_error_returns_none(self):
        import httpx

        with patch(
            "arccli.telegram_setup.httpx.get",
            side_effect=httpx.ConnectError("offline"),
        ):
            assert _verify_token("123:ABC") is None


# ── _store_token ─────────────────────────────────────────────────


class TestStoreToken:
    def test_creates_new_env_file(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        monkeypatch.setattr("arccli.telegram_setup._ENV_PATH", env_path)

        _store_token("123:ABC-DEF")

        assert env_path.exists()
        assert "ARCAGENT_TELEGRAM_BOT_TOKEN=123:ABC-DEF" in env_path.read_text()

    def test_appends_to_existing_env(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("OPENAI_API_KEY=sk-xxx\n")
        monkeypatch.setattr("arccli.telegram_setup._ENV_PATH", env_path)

        _store_token("123:ABC-DEF")

        content = env_path.read_text()
        assert "OPENAI_API_KEY=sk-xxx" in content
        assert "ARCAGENT_TELEGRAM_BOT_TOKEN=123:ABC-DEF" in content

    def test_updates_existing_token(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("ARCAGENT_TELEGRAM_BOT_TOKEN=old-token\n")
        monkeypatch.setattr("arccli.telegram_setup._ENV_PATH", env_path)

        _store_token("new-token")

        content = env_path.read_text()
        assert "ARCAGENT_TELEGRAM_BOT_TOKEN=new-token" in content
        assert "old-token" not in content

    def test_skips_if_same_token(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        env_path.write_text("ARCAGENT_TELEGRAM_BOT_TOKEN=same-token\n")
        monkeypatch.setattr("arccli.telegram_setup._ENV_PATH", env_path)

        _store_token("same-token")

        # File should not change
        assert env_path.read_text() == "ARCAGENT_TELEGRAM_BOT_TOKEN=same-token\n"

    def test_file_has_restricted_permissions(self, tmp_path, monkeypatch):
        env_path = tmp_path / ".env"
        monkeypatch.setattr("arccli.telegram_setup._ENV_PATH", env_path)

        _store_token("123:ABC")

        mode = os.stat(env_path).st_mode
        assert stat.S_IMODE(mode) == 0o600


# ── _write_file_secure ──────────────────────────────────────────


class TestWriteFileSecure:
    def test_writes_content(self, tmp_path):
        path = tmp_path / "test.txt"
        _write_file_secure(path, "hello world")
        assert path.read_text() == "hello world"

    def test_sets_permissions(self, tmp_path):
        path = tmp_path / "secret.txt"
        _write_file_secure(path, "secret")
        mode = os.stat(path).st_mode
        assert stat.S_IMODE(mode) == 0o600


# ── _update_agent_config ─────────────────────────────────────────


class TestUpdateAgentConfig:
    def _make_config(self, tmp_path: Path, content: str) -> Path:
        config_path = tmp_path / "arcagent.toml"
        config_path.write_text(content)
        return config_path

    def test_appends_telegram_section(self, tmp_path):
        config_path = self._make_config(
            tmp_path,
            '[agent]\nname = "test"\n\n[modules.memory]\nenabled = true\n',
        )

        _update_agent_config(config_path, 12345)

        config = tomllib.loads(config_path.read_text())
        assert config["modules"]["telegram"]["enabled"] is True
        assert config["modules"]["telegram"]["config"]["allowed_chat_ids"] == [12345]
        assert config["modules"]["telegram"]["config"]["poll_interval"] == 1.0

    def test_adds_chat_id_to_existing_section(self, tmp_path):
        config_path = self._make_config(
            tmp_path,
            (
                '[agent]\nname = "test"\n\n'
                "[modules.telegram]\n"
                "enabled = true\n\n"
                "[modules.telegram.config]\n"
                "allowed_chat_ids = [111]\n"
                "poll_interval = 1.0\n"
            ),
        )

        _update_agent_config(config_path, 222)

        config = tomllib.loads(config_path.read_text())
        assert 111 in config["modules"]["telegram"]["config"]["allowed_chat_ids"]
        assert 222 in config["modules"]["telegram"]["config"]["allowed_chat_ids"]

    def test_skips_duplicate_chat_id(self, tmp_path):
        config_path = self._make_config(
            tmp_path,
            (
                '[agent]\nname = "test"\n\n'
                "[modules.telegram]\n"
                "enabled = true\n\n"
                "[modules.telegram.config]\n"
                "allowed_chat_ids = [12345]\n"
            ),
        )

        original = config_path.read_text()
        _update_agent_config(config_path, 12345)
        assert config_path.read_text() == original


# ── _discover_chat_id ────────────────────────────────────────────


class TestDiscoverChatId:
    def test_returns_chat_id_from_update(self):
        # First call: clear stale updates
        clear_resp = MagicMock()
        clear_resp.json.return_value = {"ok": True, "result": []}

        # Second call: return a message with chat_id
        poll_resp = MagicMock()
        poll_resp.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 98765, "first_name": "Josh"},
                        "text": "hello",
                    },
                }
            ],
        }

        with patch(
            "arccli.telegram_setup.httpx.get",
            side_effect=[clear_resp, poll_resp],
        ):
            result = _discover_chat_id("fake-token", "test_bot")
            assert result == 98765

    def test_clears_stale_updates(self):
        # Stale update returned first
        stale_resp = MagicMock()
        stale_resp.json.return_value = {
            "ok": True,
            "result": [{"update_id": 42, "message": {"chat": {"id": 1}}}],
        }

        # Fresh update
        fresh_resp = MagicMock()
        fresh_resp.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 43,
                    "message": {
                        "chat": {"id": 55555, "first_name": "Test"},
                        "text": "hi",
                    },
                }
            ],
        }

        with patch(
            "arccli.telegram_setup.httpx.get",
            side_effect=[stale_resp, fresh_resp],
        ):
            result = _discover_chat_id("fake-token", "test_bot")
            assert result == 55555
