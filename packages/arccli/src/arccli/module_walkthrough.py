"""Interactive module walkthrough for ``arc agent build``.

Presents each available module to the user, asks whether to enable it,
and prompts for key configuration values.  Returns TOML text ready to
append to ``arcagent.toml``.
"""

from __future__ import annotations

from typing import Any

import click

from arccli.formatting import click_echo

# ---------------------------------------------------------------------------
# Module registry — curated user-facing config per module
# ---------------------------------------------------------------------------

_MODULE_REGISTRY: dict[str, dict[str, Any]] = {
    "memory": {
        "description": "Persistent memory with markdown files (notes, entities, context)",
        "default_enabled": True,
        "prompts": [
            ("context_budget_tokens", "Context budget (tokens)", 2000, int),
            ("entity_extraction_enabled", "Entity extraction", True, bool),
        ],
    },
    "policy": {
        "description": "Self-learning behavioral policy (ACE framework)",
        "default_enabled": True,
        "prompts": [
            ("eval_interval_turns", "Evaluation interval (turns)", 5, int),
        ],
    },
    "scheduler": {
        "description": "Agent self-scheduling (cron, interval, one-time tasks)",
        "default_enabled": True,
        "prompts": [
            ("check_interval_seconds", "Check interval (seconds)", 30, int),
        ],
    },
    "browser": {
        "description": "Web browsing via Chrome DevTools Protocol",
        "default_enabled": False,
        "prompts": [
            ("headless", "Run headless (no visible window)", True, bool),
        ],
    },
    "telegram": {
        "description": "Bidirectional Telegram messaging",
        "default_enabled": False,
        "prompts": [],
        "setup_hint": (
            "Run 'arc agent setup-telegram {path}' after to configure bot token and chat ID"
        ),
    },
}


def walk_modules(existing_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Walk through all modules interactively.

    Parameters
    ----------
    existing_config:
        The current parsed ``arcagent.toml`` dict (may be empty).

    Returns
    -------
    dict mapping module name to ``{"enabled": bool, "config": {...}}``.
    """
    modules_cfg = existing_config.get("modules", {})
    result: dict[str, dict[str, Any]] = {}

    for idx, (name, info) in enumerate(_MODULE_REGISTRY.items(), 1):
        click_echo(f"\n  {idx}. {name} — {info['description']}")
        module_result = _prompt_module(name, info, modules_cfg.get(name, {}))
        if module_result is not None:
            result[name] = module_result

    return result


def _prompt_module(
    name: str,
    info: dict[str, Any],
    current_config: dict[str, Any],
) -> dict[str, Any] | None:
    """Ask enable/disable and key config for one module.

    Returns ``{"enabled": True, "config": {...}}`` when enabled,
    or ``None`` when disabled.
    """
    default_enabled: bool = current_config.get("enabled", info["default_enabled"])

    # Y/n when default enabled, y/N when default disabled
    enabled = click.confirm(
        f"     Enable {name}?",
        default=default_enabled,
    )

    if not enabled:
        return None

    config: dict[str, Any] = {}
    current_inner = current_config.get("config", {})

    for key, label, default_val, val_type in info["prompts"]:
        existing = current_inner.get(key, default_val)
        if val_type is bool:
            config[key] = click.confirm(f"     {label}", default=existing)
        elif val_type is int:
            config[key] = click.prompt(f"     {label}", default=existing, type=int)
        else:
            config[key] = click.prompt(f"     {label}", default=existing)

    # Show setup hint if present
    hint = info.get("setup_hint")
    if hint:
        click_echo(f"     → {hint}")

    return {"enabled": True, "config": config}


def format_modules_toml(modules: dict[str, dict[str, Any]]) -> str:
    """Generate TOML text for all enabled modules."""
    lines: list[str] = []
    for name, data in modules.items():
        lines.append(f"[modules.{name}]")
        lines.append(f"enabled = {_toml_val(data['enabled'])}")
        lines.append("")

        config = data.get("config", {})
        if config:
            lines.append(f"[modules.{name}.config]")
            for key, val in config.items():
                lines.append(f"{key} = {_toml_val(val)}")
            lines.append("")

    return "\n".join(lines)


def _toml_val(value: Any) -> str:
    """Format a Python value as a TOML literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)
