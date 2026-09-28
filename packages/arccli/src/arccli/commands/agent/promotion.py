"""`arc agent promotion` — memory promotion settings and the classifier key.

SPEC-083 COMP-027 (REQ-508, REQ-510, REQ-511). Promotion moves an agent's
company knowledge into the fleet store; this verb is how an operator tunes it::

    arc agent promotion show <agent_dir> [--json]
    arc agent promotion set  <agent_dir> [--enabled|--disabled] [--threshold X] [--model M]
    arc agent promotion key set | remove

Three properties shape it:

* **Validated before written.** The MERGED ``[modules.memory.config.promotion]``
  block is checked by the memory module's own config model at the agent's tier,
  so a refused change (threshold under the floor, a ``*-latest`` model, enabled at
  federal) exits non-zero and the file is not touched. The tier is federal when
  EITHER ``[security].tier`` or the memory block says so — fail closed.
* **One audited write.** An accepted change is written atomically with tomlkit
  (comments and every other setting survive) and emits exactly one
  ``memory.promotion.config_changed`` event, field by field old -> new, on the
  operator-signed chain ``arc keys`` writes to.
* **The key is write-only.** ``key set`` reads piped stdin or a hidden prompt and
  stores through the same :class:`arcagent.KeyStore` as ``arc keys``; there is no
  flag that takes a value, and ``show`` reports only ``set`` / ``not set``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import tomllib
from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import arcagent
import tomlkit
from arctrust.audit import AuditEvent, emit
from pydantic import ValidationError
from tomlkit.toml_document import TOMLDocument

from arccli.commands._shared import err, print_json
from arccli.commands._shared import write as _out
from arccli.commands.agent._common import _resolve_agent_dir
from arccli.commands.agent._config_sync import _set_dotted
from arccli.commands.keys import _actor_did, _arc_dir, _audit_sink, _audited, _data_dir
from arccli.commands.module import _write_atomic

T = TypeVar("T")

CONFIG_CHANGED = "memory.promotion.config_changed"
_PROG = "arc agent promotion"
_CONFIG_FILE = "arcagent.toml"
_PROMOTION_PATH = ("modules", "memory", "config", "promotion")
_FEDERAL = "federal"
_DEFAULT_TIER = "personal"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fail(message: str) -> NoReturn:
    """Report the problem to stderr and stop. Never returns."""
    err(f"{_PROG}: {message}")
    sys.exit(1)


def _run_store(call: Coroutine[Any, Any, T]) -> T:
    """Run one KeyStore call; its refusal names the coordinate, never the value."""
    try:
        return asyncio.run(call)
    except arcagent.ArcAgentError as exc:
        _fail(exc.message)


def _config_path(args: argparse.Namespace) -> Path:
    path = _resolve_agent_dir(args.path) / _CONFIG_FILE
    if not path.is_file():
        _fail(f"no {_CONFIG_FILE} in {path.parent}")
    return path


def _promotion_block(config: Mapping[str, Any]) -> dict[str, Any]:
    """The raw ``[modules.memory.config.promotion]`` table, empty when absent."""
    node: Any = config
    for part in _PROMOTION_PATH:
        node = node.get(part, {}) if isinstance(node, Mapping) else {}
    return dict(node) if isinstance(node, Mapping) else {}


def _agent_tier(config: Mapping[str, Any]) -> str:
    """The agent's tier; federal if ANY tier the file declares is federal."""
    security = config.get("security", {})
    memory = config.get("modules", {}).get("memory", {}).get("config", {})
    declared = [str(table.get("tier") or "") for table in (security, memory)]
    if any(tier.strip().casefold() == _FEDERAL for tier in declared):
        return _FEDERAL
    return next((tier for tier in declared if tier), _DEFAULT_TIER)


def _refusal(exc: ValidationError) -> str:
    """Every rejected field and why — names only, never the whole input."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'promotion'}: {error['msg']}"
        for error in exc.errors()
    )


def _key_env() -> str:
    """The fleet-wide classifier key coordinate the memory module declares."""
    return str(arcagent.MemoryPromotionConfig().api_key_env)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _show(args: argparse.Namespace) -> None:
    """Print the settings, the tier and whether the key is stored — never the key."""
    config = tomllib.loads(_config_path(args).read_text(encoding="utf-8"))
    try:
        settings = arcagent.MemoryPromotionConfig.model_validate(_promotion_block(config))
    except ValidationError as exc:
        _fail(f"invalid promotion settings: {_refusal(exc)}")
    tier = _agent_tier(config)
    with _audited(args) as (store, did):
        statuses = _run_store(store.list(caller_did=did))
    key_set = any(s.present for s in statuses if s.env_var == settings.api_key_env)
    view: dict[str, Any] = {
        "enabled": settings.enabled,
        "confidence_threshold": settings.confidence_threshold,
        "classifier_model": settings.classifier_model,
        "tier": tier,
        "federal_locked": tier == _FEDERAL,
        "key_set": key_set,
    }
    if args.json:
        print_json(view)
        return
    for field in ("enabled", "confidence_threshold", "classifier_model", "tier", "federal_locked"):
        value = view[field]
        _out(f"{field}: {str(value).lower() if isinstance(value, bool) else value}")
    _out(f"key: {'set' if key_set else 'not set'} ({settings.api_key_env})")


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


def _requested(args: argparse.Namespace) -> dict[str, Any]:
    fields = {
        "enabled": args.enabled,
        "confidence_threshold": args.threshold,
        "classifier_model": args.model,
    }
    return {field: value for field, value in fields.items() if value is not None}


def _changes(current: Mapping[str, Any], requested: Mapping[str, Any]) -> dict[str, Any]:
    """Field -> ``{"old", "new"}`` for every requested value that differs."""
    defaults = arcagent.MemoryPromotionConfig.model_fields
    changes: dict[str, Any] = {}
    for field, new in requested.items():
        old = current.get(field, defaults[field].default)
        if old != new:
            changes[field] = {"old": old, "new": new}
    return changes


def _ensure_promotion_table(document: TOMLDocument) -> None:
    """Create any missing table on the path to the promotion block."""
    node: Any = document
    for part in _PROMOTION_PATH:
        if part not in node:
            node[part] = tomlkit.table(is_super_table=part != _PROMOTION_PATH[-1])
        node = node[part]


def _set(args: argparse.Namespace) -> None:
    """Validate the merged block at the agent's tier, write it, audit old -> new."""
    requested = _requested(args)
    if not requested:
        _fail("nothing to set: pass --enabled/--disabled, --threshold or --model")
    config_path = _config_path(args)
    text = config_path.read_text(encoding="utf-8")
    config = tomllib.loads(text)
    current = _promotion_block(config)
    tier = _agent_tier(config)
    try:
        arcagent.MemoryConfig.model_validate({"tier": tier, "promotion": current | requested})
    except ValidationError as exc:
        _fail(f"refused ({tier} tier): {_refusal(exc)}")

    changes = _changes(current, requested)
    if not changes:
        _out("No change: the promotion settings already have those values.")
        return

    arc_dir = _arc_dir(args)
    sink = _audit_sink(arc_dir, _data_dir(args))
    try:
        actor = _actor_did(arc_dir)
        document = tomlkit.parse(text)
        _ensure_promotion_table(document)
        for field, change in changes.items():
            _set_dotted(document, ".".join((*_PROMOTION_PATH, field)), change["new"])
        _write_atomic(config_path, document)
        agent = str(config.get("identity", {}).get("did") or config_path.parent.name)
        emit(
            AuditEvent(
                actor_did=actor,
                action=CONFIG_CHANGED,
                target=f"agent:{agent}",
                outcome="allow",
                tier=tier,
                extra={"changes": changes, "config_path": str(config_path)},
            ),
            sink,
        )
    finally:
        sink.close()
    _out(f"Updated {', '.join(changes)} in {config_path}.")


# ---------------------------------------------------------------------------
# key set / remove
# ---------------------------------------------------------------------------


def _read_key(env_var: str) -> str:
    """A hidden prompt on a terminal; one line of piped stdin otherwise."""
    if sys.stdin.isatty():
        return getpass.getpass(f"{env_var} (hidden): ")
    return sys.stdin.readline().strip()


def _key_set(args: argparse.Namespace) -> None:
    env_var = _key_env()
    value = _read_key(env_var)
    with _audited(args) as (store, did):
        _run_store(store.set(env_var, value, caller_did=did))
    _out(f"Stored {env_var} in {arcagent.default_env_file(_arc_dir(args))}.")


def _key_remove(args: argparse.Namespace) -> None:
    env_var = _key_env()
    with _audited(args) as (store, did):
        removed = _run_store(store.delete(env_var, caller_did=did))
    _out(f"Removed {env_var}." if removed else f"No {env_var} was stored; nothing to remove.")


_KEY_MAP = {"set": _key_set, "remove": _key_remove}


def _key(args: argparse.Namespace) -> None:
    _KEY_MAP[args.key_cmd](args)


_PROMOTION_MAP = {"show": _show, "set": _set, "key": _key}


def _promotion(args: argparse.Namespace) -> None:
    """Entry for ``arc agent promotion <verb>``."""
    _PROMOTION_MAP[args.promotion_cmd](args)


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def _add_world(parser: argparse.ArgumentParser) -> None:
    """Where the key store and audit chain live — the same flags as ``arc keys``."""
    parser.add_argument(
        "--arc-dir", default=None, help="Arc config dir (default: ${ARC_CONFIG_DIR:-~/.arc})."
    )
    parser.add_argument("--data-dir", default=None, help="Operational data dir for audit.")


def add_promotion_parser(subs: Any) -> None:
    """Register ``promotion`` on the ``arc agent`` subparsers."""
    parser = subs.add_parser("promotion", help="Memory promotion settings and classifier key.")
    verbs = parser.add_subparsers(dest="promotion_cmd", metavar="<verb>", required=True)

    p = verbs.add_parser("show", help="Show promotion settings, tier and key presence.")
    p.add_argument("path", help="Agent directory.")
    p.add_argument("--json", action="store_true", help="Emit JSON.")
    _add_world(p)

    # No key flag here: a credential on argv is shell history (D-555).
    p = verbs.add_parser("set", help="Change promotion settings (validated, audited).")
    p.add_argument("path", help="Agent directory.")
    toggle = p.add_mutually_exclusive_group()
    toggle.add_argument("--enabled", dest="enabled", action="store_const", const=True)
    toggle.add_argument("--disabled", dest="enabled", action="store_const", const=False)
    p.add_argument("--threshold", type=float, default=None, help="Confidence threshold.")
    p.add_argument("--model", default=None, help="Pinned classifier model version.")
    _add_world(p)

    p = verbs.add_parser("key", help="Store or forget the fleet-wide classifier key.")
    key_verbs = p.add_subparsers(dest="key_cmd", metavar="<verb>", required=True)
    # No --value: the key is read from piped stdin or a hidden prompt only.
    _add_world(key_verbs.add_parser("set", help="Store the key (stdin or hidden prompt)."))
    _add_world(key_verbs.add_parser("remove", help="Forget the stored key."))
