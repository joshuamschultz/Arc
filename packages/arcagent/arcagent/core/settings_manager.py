"""Settings Manager — runtime settings overlay on frozen config.

The original ArcAgentConfig is never mutated. A mutable overlay dict
takes priority for supported runtime keys. Changes persist to a
[settings] section in arcagent.toml.
"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path
from typing import Any, ClassVar

from arcagent.core.config import ArcAgentConfig
from arcagent.core.errors import SettingsError

_logger = logging.getLogger("arcagent.settings_manager")


class SettingsManager:
    """Runtime settings overlay on frozen Pydantic config."""

    # Keys that can be changed at runtime, with expected type
    MUTABLE_KEYS: ClassVar[dict[str, type]] = {
        "model": str,
        "compaction_threshold": float,
        "tool_timeout": int,
        "log_level": str,
    }

    # Keys blocked from runtime change (security-sensitive)
    BLOCKED_KEYS: ClassVar[set[str]] = {
        "identity",
        "vault",
        "keys",
        "did",
        "key_dir",
    }

    def __init__(
        self,
        config: ArcAgentConfig,
        telemetry: Any,
        bus: Any,
        config_path: Path,
    ) -> None:
        self._config = config
        self._telemetry = telemetry
        self._bus = bus
        self._config_path = config_path
        self._overlay: dict[str, Any] = {}

        # Load existing [settings] from TOML if present
        self._load_overlay_from_toml()

    def get(self, key: str) -> Any:
        """Get setting value. Overlay first, then config."""
        if key not in self.MUTABLE_KEYS:
            raise SettingsError(
                code="SETTINGS_UNKNOWN_KEY",
                message=f"Unknown setting key: {key}",
                details={"key": key, "valid_keys": list(self.MUTABLE_KEYS)},
            )
        if key in self._overlay:
            return self._overlay[key]
        return self._resolve_from_config(key)

    async def set(self, key: str, value: Any) -> None:
        """Set runtime override. Validates type, persists to TOML."""
        self._validate_key(key)
        self._validate_type(key, value)

        old_value = self.get(key)
        self._overlay[key] = value
        self._persist_to_toml()

        self._telemetry.audit_event(
            "settings.changed",
            {"key": key, "old_value": str(old_value), "new_value": str(value)},
        )
        await self._bus.emit(
            "agent:settings_changed",
            {"key": key, "old_value": old_value, "new_value": value},
        )

        _logger.info("Setting changed: %s = %s (was %s)", key, value, old_value)

    def _validate_key(self, key: str) -> None:
        """Validate key is mutable and not blocked."""
        if key in self.BLOCKED_KEYS:
            raise SettingsError(
                code="SETTINGS_BLOCKED_KEY",
                message=f"Setting '{key}' cannot be changed at runtime",
                details={"key": key},
            )
        if key not in self.MUTABLE_KEYS:
            raise SettingsError(
                code="SETTINGS_UNKNOWN_KEY",
                message=f"Unknown setting key: {key}",
                details={"key": key, "valid_keys": list(self.MUTABLE_KEYS)},
            )

    def _validate_type(self, key: str, value: Any) -> None:
        """Validate value type matches expected type for key."""
        expected = self.MUTABLE_KEYS[key]
        if not isinstance(value, expected):
            raise SettingsError(
                code="SETTINGS_TYPE_ERROR",
                message=(
                    f"Setting '{key}' expects {expected.__name__}, got {type(value).__name__}"
                ),
                details={
                    "key": key,
                    "expected_type": expected.__name__,
                    "actual_type": type(value).__name__,
                },
            )

    def _resolve_from_config(self, key: str) -> Any:
        """Map flat key name to nested config path."""
        config = self._config
        key_map: dict[str, Any] = {
            "model": config.llm.model,
            "compaction_threshold": config.context.compact_threshold,
            "tool_timeout": config.tools.policy.timeout_seconds,
            "log_level": config.telemetry.log_level,
        }
        return key_map.get(key)

    def _load_overlay_from_toml(self) -> None:
        """Load existing [settings] section from TOML file."""
        if not self._config_path.exists():
            return

        try:
            data = tomllib.loads(self._config_path.read_text(encoding="utf-8"))
        except Exception:
            _logger.warning("Cannot parse TOML for settings overlay: %s", self._config_path)
            return

        settings = data.get("settings")
        if not isinstance(settings, dict):
            return

        for key, value in settings.items():
            if key in self.MUTABLE_KEYS:
                expected_type = self.MUTABLE_KEYS[key]
                if isinstance(value, expected_type):
                    self._overlay[key] = value
                else:
                    _logger.warning(
                        "Ignoring setting '%s': expected %s, got %s",
                        key,
                        expected_type.__name__,
                        type(value).__name__,
                    )

    def _persist_to_toml(self) -> None:
        """Write overlay to [settings] section in TOML file.

        Reads existing file, replaces or appends [settings] section.
        Uses string manipulation since tomllib is read-only.
        """
        if not self._overlay:
            return

        if self._config_path.exists():
            content = self._config_path.read_text(encoding="utf-8")
        else:
            content = ""

        # Build [settings] section
        settings_lines = ["[settings]"]
        for key, value in sorted(self._overlay.items()):
            if isinstance(value, str):
                # Escape backslashes and quotes for valid TOML
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                escaped = escaped.replace("\n", "\\n").replace("\r", "\\r")
                settings_lines.append(f'{key} = "{escaped}"')
            elif isinstance(value, float):
                settings_lines.append(f"{key} = {value}")
            elif isinstance(value, int):
                settings_lines.append(f"{key} = {value}")
        settings_block = "\n".join(settings_lines) + "\n"

        # Replace existing [settings] section or append
        pattern = r"\[settings\]\n(?:[^\[]*?)(?=\n\[|\Z)"
        if re.search(pattern, content, re.DOTALL):
            content = re.sub(pattern, settings_block.rstrip(), content, flags=re.DOTALL)
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += "\n" + settings_block

        self._config_path.write_text(content, encoding="utf-8")
