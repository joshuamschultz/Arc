"""``GET/PUT /api/agents/{id}/memory/promotion`` — memory sharing settings (SPEC-083 COMP-028).

The ArcUI surface for ``[modules.memory.config.promotion]``: an operator turns
private -> shared promotion on or off and tunes the confidence threshold and the
pinned classifier model. Three settings, nothing else — any other field is refused,
so this route can never write a key (or any other knob) into agent TOML.

The Jev API key does NOT travel through here. It goes to ``PUT/DELETE
/api/keys/{env_var}`` (the one write-only :class:`arcagent.KeyStore`), keyed by
the agent's own configured ``api_key_env`` (default ``TYPESAFE_API_KEY``); this
route reports only ``key_set``.

Field bounds, the pinned-model rule, and the federal lock are validated through
``arcagent.MemoryPromotionConfig`` / ``arcagent.MemoryConfig`` — the same models
``arc agent promotion set`` validates through (see
``arccli.commands.agent.promotion``). This route restates none of that logic; it
only decides which three fields may be set here, converts a ``ValidationError``
into a field-only refusal message, and applies the write. The federal lock is
checked on the MERGED block at the agent's tier (fail closed: ``[security].tier``
OR the memory block's ``tier`` being federal locks it), so a hand-edited federal
file that is already enabled refuses every further write.

Every accepted write emits one ``memory.promotion.config_changed`` audit record
with old -> new values; refusals are audited as ``denied``.
"""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path
from typing import Any

import arcagent
import tomlkit
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit, operator_audit_sink
from arcui.routes.agent_detail._common import _agent_root
from arcui.routes.agent_detail.config_files import (
    BodyTooLargeError,
    _error,
    read_json_object,
)
from arcui.routes.arcllm_config import _atomic_write, _deep_merge

logger = logging.getLogger("arcui.routes.agent_detail.memory_promotion")

_AUDIT_OPERATION = "memory.promotion.config_changed"
_FEDERAL = "federal"

_SETTINGS = ("enabled", "confidence_threshold", "classifier_model")
_DEFAULTS: dict[str, Any] = {
    name: arcagent.MemoryPromotionConfig.model_fields[name].default for name in _SETTINGS
}


class SettingsRefusedError(ValueError):
    """A requested (or merged) promotion setting is invalid; the message is safe to show."""


# ---------------------------------------------------------------------------
# Reading the agent file
# ---------------------------------------------------------------------------


def _memory_config(doc: dict[str, Any]) -> dict[str, Any]:
    cfg = doc.get("modules", {}).get("memory", {}).get("config", {})
    return cfg if isinstance(cfg, dict) else {}


def _raw_promotion(doc: dict[str, Any]) -> dict[str, Any]:
    """The promotion block exactly as stored, including any knob outside the three
    UI settings (e.g. an operator-tuned ``max_items_per_sweep``)."""
    block = _memory_config(doc).get("promotion", {})
    return dict(block) if isinstance(block, dict) else {}


def _stored_settings(doc: dict[str, Any]) -> dict[str, Any]:
    """The three settings as stored, defaults filling any gap."""
    raw = _raw_promotion(doc)
    return {name: raw.get(name, _DEFAULTS[name]) for name in _SETTINGS}


def _effective_tier(doc: dict[str, Any]) -> str:
    """The agent's tier; federal if EITHER tier declaration says so (fail closed)."""
    declared = [
        str(doc.get("security", {}).get("tier", "")),
        str(_memory_config(doc).get("tier", "")),
    ]
    if any(t.strip().casefold() == _FEDERAL for t in declared):
        return _FEDERAL
    return next((t.strip() for t in declared if t.strip()), "personal")


_DEFAULT_API_KEY_ENV = arcagent.MemoryPromotionConfig.model_fields["api_key_env"].default


def _configured_key_env(doc: dict[str, Any]) -> str:
    """The agent's configured classifier-key coordinate, defaults from the model.

    Mirrors ``arc agent promotion show``: validate the stored block through
    ``MemoryPromotionConfig`` and read ``api_key_env`` off it. GET must never 500
    on a tampered/invalid stored block, so an unvalidatable block falls back to
    whatever coordinate is literally stored, or the model's own default.
    """
    raw = _raw_promotion(doc)
    try:
        return str(arcagent.MemoryPromotionConfig.model_validate(raw).api_key_env)
    except ValidationError:
        return str(raw.get("api_key_env", _DEFAULT_API_KEY_ENV))


async def _key_set(request: Request, doc: dict[str, Any]) -> bool:
    env_var = _configured_key_env(doc)
    store = arcagent.KeyStore(arcagent.default_env_file(), sink=operator_audit_sink(request))
    statuses = await store.list()
    return any(s.env_var == env_var and s.present for s in statuses)


async def _response(request: Request, doc: dict[str, Any]) -> JSONResponse:
    tier = _effective_tier(doc)
    settings = _stored_settings(doc)
    return JSONResponse(
        {
            "enabled": bool(settings["enabled"]),
            "confidence_threshold": float(settings["confidence_threshold"]),
            "classifier_model": str(settings["classifier_model"]),
            "tier": tier,
            "federal_locked": tier == _FEDERAL,
            "key_set": await _key_set(request, doc),
        }
    )


def _config_path(request: Request) -> Path | None:
    agent_root = _agent_root(request, request.path_params["id"])
    if agent_root is None:
        return None
    path = agent_root / "arcagent.toml"
    return path if path.is_file() else None


# ---------------------------------------------------------------------------
# Validation — delegated to arcagent's own promotion models
# ---------------------------------------------------------------------------


def _refusal(exc: ValidationError) -> str:
    """Every rejected field and why, joined; the submitted value is never echoed."""
    parts = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"] if part != "promotion")
        parts.append(f"{field}: {error['msg']}" if field else error["msg"])
    return "; ".join(parts)


def _validate_and_merge(
    body: dict[str, Any], raw_promotion: dict[str, Any], tier: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the requested update against the MERGED block at ``tier``.

    Every bound, type, pin, and federal check runs through ``arcagent.MemoryConfig``
    (the same model ``arc agent promotion set`` validates through) — this route
    carries no restated copy of that logic. Returns ``(updates, merged)``:
    pydantic-coerced canonical values for the fields the caller sent, and for all
    three UI settings (for the old -> new audit diff). Raises
    :class:`SettingsRefusedError` with a field-only message — never a submitted
    value — on any refusal, including a field outside the three UI settings.
    """
    if not body or any(name not in _SETTINGS for name in body):
        raise SettingsRefusedError(
            "only enabled, confidence_threshold and classifier_model may be set here"
        )
    try:
        config = arcagent.MemoryConfig.model_validate(
            {"tier": tier, "promotion": {**raw_promotion, **body}}
        )
    except ValidationError as exc:
        raise SettingsRefusedError(_refusal(exc)) from exc
    validated = config.promotion.model_dump()
    updates = {name: validated[name] for name in body}
    merged = {name: validated[name] for name in _SETTINGS}
    return updates, merged


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def get_memory_promotion(request: Request) -> JSONResponse:
    """GET — the three settings, the agent's tier, the federal lock and ``key_set``."""
    path = _config_path(request)
    if path is None:
        return _error("Agent not found", 404)
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        logger.exception("Failed to read arcagent.toml for %s", request.path_params["id"])
        return _error("Failed to read config", 500)
    return await _response(request, doc)


async def put_memory_promotion(request: Request) -> JSONResponse:
    """PUT — change any subset of the three settings. Operator only; audited old -> new."""
    if getattr(request.state, "role", None) != "operator":
        return _error("Operator role required", 403)

    agent_id = request.path_params["id"]
    path = _config_path(request)
    if path is None:
        return _error("Agent not found", 404)

    try:
        body = await read_json_object(request)
    except BodyTooLargeError:
        return _error("Request body too large", 413)
    if body is None:
        return _error("Body must be a JSON object", 400)

    try:
        with open(path, encoding="utf-8") as f:
            doc = tomlkit.load(f)
    except (OSError, tomlkit.exceptions.ParseError):
        logger.exception("Failed to read arcagent.toml for %s", agent_id)
        return _error("Failed to read config", 500)

    plain = doc.unwrap()
    old = _stored_settings(plain)
    try:
        updates, merged = _validate_and_merge(body, _raw_promotion(plain), _effective_tier(plain))
    except SettingsRefusedError as exc:
        emit_mutation_audit(
            request,
            target=f"agent:{agent_id}",
            operation=_AUDIT_OPERATION,
            outcome="denied",
            detail=str(exc),
        )
        return _error(str(exc), 422)

    _deep_merge(doc, {"modules": {"memory": {"config": {"promotion": updates}}}})
    try:
        _atomic_write(path, doc)
    except OSError as exc:
        logger.exception("Failed to write arcagent.toml for %s", agent_id)
        return _error(f"Failed to write config: {type(exc).__name__}", 500)

    changes = {k: {"old": old[k], "new": merged[k]} for k in updates}
    emit_mutation_audit(
        request,
        target=f"agent:{agent_id}",
        operation=_AUDIT_OPERATION,
        outcome="applied",
        detail=json.dumps(changes, sort_keys=True),
    )
    return await _response(request, doc.unwrap())


__all__ = ["get_memory_promotion", "put_memory_promotion"]
