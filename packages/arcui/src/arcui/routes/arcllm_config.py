"""ArcLLM config routes — /api/arcllm-config (GET/PATCH).

Reads and writes the actual arcllm config.toml file,
making the UI the single source of truth for all ArcLLM configuration.
Uses tomlkit to preserve comments and formatting.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import tomlkit
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Allowlist of valid top-level and nested config keys.
# Rejects arbitrary key injection (NIST SI-10, CM-6).
_ALLOWED_SECTIONS: dict[str, set[str] | None] = {
    "defaults": {"provider", "temperature", "max_tokens"},
    "vault": {"backend", "cache_ttl_seconds", "url", "region"},
    "modules": None,  # validated per-module below
}
_ALLOWED_MODULE_KEYS: dict[str, set[str]] = {
    "telemetry": {
        "enabled", "log_level", "monthly_limit_usd", "daily_limit_usd",
        "per_call_max_usd", "alert_threshold_pct", "enforcement",
    },
    "retry": {"enabled", "max_retries", "backoff_base_seconds"},
    "fallback": {"enabled", "chain"},
    "rate_limit": {"enabled", "requests_per_minute", "burst"},
    "queue": {"enabled", "max_concurrent", "call_timeout", "max_queued"},
    "routing": {"enabled", "enforcement", "default_classification"},
    "audit": {"enabled", "log_requests", "log_responses"},
    "security": {"enabled", "signing_key_env"},
    "otel": {"enabled", "endpoint", "service_name", "resource_attributes"},
}

# Sensitive fields redacted for viewer role (NIST AC-6, SC-28).
_SENSITIVE_SECTIONS = {"vault"}
_SENSITIVE_KEYS = {"signing_key_env", "url"}


def _get_config_path() -> Path | None:
    """Locate the arcllm config.toml via the package itself.

    Returns None if arcllm is not installed.
    """
    try:
        from arcllm.config import _get_config_dir

        return _get_config_dir() / "config.toml"
    except ImportError:
        logger.warning("arcllm not installed, cannot resolve config path")
        return None


def _read_config(config_path: Path) -> dict[str, Any]:
    """Read config.toml and return as a plain dict."""
    with open(config_path, "r", encoding="utf-8") as f:
        return dict(tomlkit.load(f))


def _tomlkit_to_plain(obj: Any) -> Any:
    """Recursively convert tomlkit objects to plain Python types."""
    if isinstance(obj, dict):
        return {k: _tomlkit_to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_tomlkit_to_plain(v) for v in obj]
    if isinstance(obj, tomlkit.items.Integer):
        return int(obj)
    if isinstance(obj, tomlkit.items.Float):
        return float(obj)
    if isinstance(obj, tomlkit.items.Bool):
        return bool(obj)
    if isinstance(obj, tomlkit.items.String):
        return str(obj)
    return obj


def _redact_for_viewer(data: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive fields for non-operator roles."""
    result = dict(data)
    for section in _SENSITIVE_SECTIONS:
        if section in result:
            result[section] = {k: "***" for k in result[section]}
    # Redact specific keys within modules
    if "modules" in result:
        mods = dict(result["modules"])
        for mod_name, mod_data in mods.items():
            if isinstance(mod_data, dict):
                mods[mod_name] = {
                    k: ("***" if k in _SENSITIVE_KEYS else v)
                    for k, v in mod_data.items()
                }
        result["modules"] = mods
    return result


def _validate_updates(updates: dict[str, Any]) -> str | None:
    """Validate update keys against allowlist. Returns error message or None."""
    for section, value in updates.items():
        if section not in _ALLOWED_SECTIONS:
            return f"Unknown config section: {section}"
        if not isinstance(value, dict):
            return f"Section '{section}' must be a dict"

        allowed_keys = _ALLOWED_SECTIONS[section]
        if allowed_keys is not None:
            # Direct key validation (defaults, vault)
            for key in value:
                if key not in allowed_keys:
                    return f"Unknown key '{key}' in section '{section}'"
        elif section == "modules":
            # Per-module key validation
            for mod_name, mod_data in value.items():
                if mod_name not in _ALLOWED_MODULE_KEYS:
                    return f"Unknown module: {mod_name}"
                if not isinstance(mod_data, dict):
                    return f"Module '{mod_name}' must be a dict"
                for key in mod_data:
                    if key not in _ALLOWED_MODULE_KEYS[mod_name]:
                        return f"Unknown key '{key}' in module '{mod_name}'"
    return None


def _atomic_write(config_path: Path, doc: Any) -> None:
    """Write tomlkit document to file atomically (write-to-temp + rename)."""
    dir_path = config_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".toml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            tomlkit.dump(doc, f)
        os.replace(tmp_path, str(config_path))
    except BaseException:
        # Clean up temp file on any error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def get_arcllm_config(request: Request) -> JSONResponse:
    """GET /api/arcllm-config — return arcllm config.toml as JSON.

    Viewers get redacted sensitive fields. Operators see full config.
    """
    config_path = _get_config_path()
    if config_path is None:
        return JSONResponse(
            {"error": "arcllm not installed"}, status_code=503
        )
    if not config_path.exists():
        return JSONResponse(
            {"error": "arcllm config.toml not found"}, status_code=404
        )

    try:
        data = _read_config(config_path)
        plain = _tomlkit_to_plain(data)
        # Redact sensitive fields for non-operator roles
        if request.state.role != "operator":
            plain = _redact_for_viewer(plain)
        return JSONResponse(plain)
    except (OSError, tomlkit.exceptions.ParseError):
        logger.exception("Failed to read arcllm config")
        return JSONResponse(
            {"error": "Failed to read config"}, status_code=500
        )


async def patch_arcllm_config(request: Request) -> JSONResponse:
    """PATCH /api/arcllm-config — update arcllm config.toml fields.

    Accepts a JSON body with the same structure as the TOML.
    Only updates fields that are present in the request body.
    Preserves comments and formatting via tomlkit.
    Requires operator role. Validates keys against allowlist.
    Uses atomic write (temp file + rename) to prevent corruption.
    """
    if request.state.role != "operator":
        return JSONResponse(
            {"error": "Operator role required"}, status_code=403
        )

    config_path = _get_config_path()
    if config_path is None:
        return JSONResponse(
            {"error": "arcllm not installed"}, status_code=503
        )
    if not config_path.exists():
        return JSONResponse(
            {"error": "arcllm config.toml not found"}, status_code=404
        )

    body = await request.body()
    if len(body) > 65_536:  # 64KB max
        return JSONResponse(
            {"error": "Request body too large"}, status_code=413
        )

    try:
        updates = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if not isinstance(updates, dict):
        return JSONResponse(
            {"error": "Body must be a JSON object"}, status_code=400
        )

    # Validate keys against allowlist (NIST SI-10)
    error = _validate_updates(updates)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    try:
        # Read existing config with tomlkit (preserves comments)
        with open(config_path, "r", encoding="utf-8") as f:
            doc = tomlkit.load(f)

        # Deep merge updates into the tomlkit document
        _deep_merge(doc, updates)

        # Atomic write: temp file + rename (prevents corruption on crash)
        _atomic_write(config_path, doc)

        # Return the updated config from the in-memory document
        plain = _tomlkit_to_plain(dict(doc))
        logger.info(
            "ArcLLM config updated by %s: %s",
            request.state.role,
            list(updates.keys()),
        )
        return JSONResponse(plain)

    except (OSError, tomlkit.exceptions.ParseError) as exc:
        logger.exception("Failed to update arcllm config")
        return JSONResponse(
            {"error": f"Failed to update config: {type(exc).__name__}"},
            status_code=500,
        )


def _deep_merge(target: Any, source: dict[str, Any]) -> None:
    """Deep merge source dict into a tomlkit document/table."""
    for key, value in source.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


routes = [
    Route("/api/arcllm-config", get_arcllm_config, methods=["GET"]),
    Route("/api/arcllm-config", patch_arcllm_config, methods=["PATCH"]),
]
