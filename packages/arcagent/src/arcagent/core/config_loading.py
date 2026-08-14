"""TOML-family composition and environment overlays for ArcAgent config."""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from arctrust.paths import config_file

from arcagent.core.errors import ConfigError

_logger = logging.getLogger("arcagent.config")
_ENV_PREFIX = "ARCAGENT_"
_ENV_DELIMITER = "__"
_ENV_DENYLIST_PREFIXES = frozenset(
    {
        "vault__backend",
        "tools__process",
        "tools__preamble",
        "tools__policy__allowed_paths",
        "identity__key_dir",
    }
)
_ARCLLM_SECTIONS = ("llm", "eval", "budget")


def apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        env_path = key[len(_ENV_PREFIX) :].lower()
        if any(env_path.startswith(prefix) for prefix in _ENV_DENYLIST_PREFIXES):
            _logger.warning("Blocked env var override for security-sensitive key: %s", key)
            continue
        parts = env_path.split(_ENV_DELIMITER)
        target = data
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def parse_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            code="CONFIG_SYNTAX",
            message=f"TOML syntax error: {exc}",
            details={"path": str(path), "error": str(exc)},
        ) from exc


def sibling_chain(filename: str, agent_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    user_path = config_file(filename)
    if user_path.exists():
        data = parse_toml(user_path)
    per_agent = agent_dir / filename
    if per_agent.exists():
        data = deep_merge(data, parse_toml(per_agent))
    return data


def compose_raw_config(path: Path, *, default_model: str) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    user_agent = config_file("arcagent.toml")
    if user_agent.exists():
        raw = parse_toml(user_agent)
    raw = deep_merge(raw, parse_toml(path))
    llm_raw = deep_merge(
        {"llm": {"model": default_model}}, sibling_chain("arcllm.toml", path.parent)
    )
    for section in _ARCLLM_SECTIONS:
        raw.pop(section, None)
        if section in llm_raw:
            raw[section] = llm_raw[section]
    raw["arcrun"] = sibling_chain("arcrun.toml", path.parent)
    return raw


__all__ = [
    "apply_env_overrides",
    "compose_raw_config",
    "deep_merge",
    "parse_toml",
    "sibling_chain",
]
