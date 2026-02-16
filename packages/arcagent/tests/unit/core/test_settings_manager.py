"""Tests for the settings manager."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.config import ArcAgentConfig
from arcagent.core.errors import SettingsError
from arcagent.core.settings_manager import SettingsManager


def _make_config(tmp_path: Path) -> tuple[ArcAgentConfig, Path]:
    """Create a minimal config and TOML file for testing."""
    toml_path = tmp_path / "arcagent.toml"
    toml_path.write_text(
        '[agent]\nname = "test-agent"\n\n'
        '[llm]\nmodel = "anthropic/claude-sonnet"\n\n'
        '[telemetry]\nlog_level = "INFO"\n\n'
    )
    config = ArcAgentConfig(
        agent={"name": "test-agent"},  # type: ignore[arg-type]
        llm={"model": "anthropic/claude-sonnet"},  # type: ignore[arg-type]
    )
    return config, toml_path


@pytest.fixture()
def mock_bus() -> MagicMock:
    bus = MagicMock()
    bus.emit = AsyncMock()
    return bus


@pytest.fixture()
def mock_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


class TestSettingsGet:
    """Get setting value tests."""

    def test_get_returns_config_value(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        assert sm.get("model") == "anthropic/claude-sonnet"

    def test_get_overlay_takes_priority(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        sm._overlay["model"] = "anthropic/claude-opus"
        assert sm.get("model") == "anthropic/claude-opus"

    def test_get_unknown_key_raises(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        with pytest.raises(SettingsError) as exc_info:
            sm.get("nonexistent_key")
        assert exc_info.value.code == "SETTINGS_UNKNOWN_KEY"


class TestSettingsSet:
    """Set setting value tests."""

    async def test_set_updates_overlay(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        await sm.set("model", "openai/gpt-4o")
        assert sm.get("model") == "openai/gpt-4o"

    async def test_set_emits_audit_event(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        await sm.set("model", "openai/gpt-4o")
        mock_telemetry.audit_event.assert_called_once()
        args = mock_telemetry.audit_event.call_args[0]
        assert args[0] == "settings.changed"

    async def test_set_emits_bus_event(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        await sm.set("log_level", "DEBUG")
        mock_bus.emit.assert_called()
        call_args = mock_bus.emit.call_args[0]
        assert call_args[0] == "agent:settings_changed"


class TestSettingsTypeValidation:
    """Type validation tests."""

    async def test_set_wrong_type_raises(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        with pytest.raises(SettingsError) as exc_info:
            await sm.set("model", 123)  # type: ignore[arg-type]
        assert exc_info.value.code == "SETTINGS_TYPE_ERROR"

    async def test_set_unknown_key_raises(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        with pytest.raises(SettingsError) as exc_info:
            await sm.set("nonexistent", "value")
        assert exc_info.value.code == "SETTINGS_UNKNOWN_KEY"


class TestSettingsBlockedKeys:
    """Blocked key tests."""

    async def test_set_identity_blocked(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        with pytest.raises(SettingsError) as exc_info:
            await sm.set("identity", "hacked")
        assert exc_info.value.code in ("SETTINGS_BLOCKED_KEY", "SETTINGS_UNKNOWN_KEY")


class TestSettingsPersistence:
    """TOML persistence tests."""

    async def test_persists_to_toml(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        await sm.set("model", "openai/gpt-4o")
        content = toml_path.read_text()
        assert "[settings]" in content
        assert "openai/gpt-4o" in content

    async def test_persists_multiple_settings(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        await sm.set("model", "openai/gpt-4o")
        await sm.set("log_level", "DEBUG")
        content = toml_path.read_text()
        assert "openai/gpt-4o" in content
        assert "DEBUG" in content

    def test_loads_existing_settings_section(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        config, toml_path = _make_config(tmp_path)
        # Pre-populate [settings] section
        content = toml_path.read_text()
        content += '\n[settings]\nmodel = "pre-set/model"\n'
        toml_path.write_text(content)

        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        assert sm.get("model") == "pre-set/model"


class TestSettingsOverlayTypeValidation:
    """Overlay type validation during TOML loading."""

    def test_wrong_type_in_toml_ignored(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        """Integer value for string key in TOML is silently ignored."""
        config, toml_path = _make_config(tmp_path)
        # Inject wrong-typed value (model should be str, not int)
        content = toml_path.read_text()
        content += "\n[settings]\nmodel = 42\n"
        toml_path.write_text(content)

        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        # Should fall through to config default, not the invalid 42
        assert sm.get("model") == "anthropic/claude-sonnet"

    def test_correct_type_in_toml_loaded(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        """Correctly typed value in TOML is loaded into overlay."""
        config, toml_path = _make_config(tmp_path)
        content = toml_path.read_text()
        content += '\n[settings]\nmodel = "openai/gpt-4o"\n'
        toml_path.write_text(content)

        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        assert sm.get("model") == "openai/gpt-4o"

    def test_unknown_key_in_toml_ignored(
        self, tmp_path: Path, mock_bus: MagicMock, mock_telemetry: MagicMock
    ) -> None:
        """Keys not in MUTABLE_KEYS are silently ignored."""
        config, toml_path = _make_config(tmp_path)
        content = toml_path.read_text()
        content += '\n[settings]\nunknown_key = "evil"\nmodel = "valid/model"\n'
        toml_path.write_text(content)

        sm = SettingsManager(config, mock_telemetry, mock_bus, toml_path)
        assert sm.get("model") == "valid/model"
        assert "unknown_key" not in sm._overlay
